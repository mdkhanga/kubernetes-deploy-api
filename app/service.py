"""Business logic: build Deployments from requests, resolve IDs, assemble live status.

No state is stored here: every call reads the current state from the cluster.
"""

from collections.abc import Iterator
from datetime import datetime, timezone

from kubernetes.client import (
    V1Container,
    V1ContainerPort,
    V1Deployment,
    V1DeploymentSpec,
    V1EnvVar,
    V1LabelSelector,
    V1ObjectMeta,
    V1PodSpec,
    V1PodTemplateSpec,
    V1ResourceRequirements,
)
from kubernetes.client.exceptions import ApiException

from app.constants import MANAGED_BY_LABEL, MANAGED_BY_VALUE, NAME_LABEL, RESTARTED_AT_ANNOTATION
from app.convert import to_detail, to_summary
from app.errors import ConflictError, LogsUnavailableError, NotFoundError, kubernetes_message
from app.k8s_gateway import Gateway
from app.logs import all_pods, single_pod
from app.models import (
    CreateDeploymentRequest,
    DeleteResponse,
    DeploymentDetail,
    DeploymentSummary,
    PatchDeploymentRequest,
)

MANAGED_SELECTOR = f"{MANAGED_BY_LABEL}={MANAGED_BY_VALUE}"


def build_deployment(req: CreateDeploymentRequest) -> V1Deployment:
    labels = {**req.labels, MANAGED_BY_LABEL: MANAGED_BY_VALUE, NAME_LABEL: req.name}

    resources = None
    if req.resources:
        resources = V1ResourceRequirements(
            requests=req.resources.requests.model_dump(exclude_none=True) if req.resources.requests else None,
            limits=req.resources.limits.model_dump(exclude_none=True) if req.resources.limits else None,
        )

    container = V1Container(
        name=req.name,
        image=req.image,
        ports=[V1ContainerPort(container_port=req.port)] if req.port else None,
        env=[V1EnvVar(name=e.name, value=e.value) for e in req.env] or None,
        resources=resources,
        command=req.command,
        args=req.args,
    )

    return V1Deployment(
        api_version="apps/v1",
        kind="Deployment",
        metadata=V1ObjectMeta(name=req.name, namespace=req.namespace, labels=labels),
        spec=V1DeploymentSpec(
            replicas=req.replicas,
            # Selector is immutable: only our own name label, never user labels.
            selector=V1LabelSelector(match_labels={NAME_LABEL: req.name}),
            template=V1PodTemplateSpec(
                metadata=V1ObjectMeta(labels=labels),
                spec=V1PodSpec(containers=[container]),
            ),
        ),
    )


def pod_selector(d: V1Deployment) -> str:
    """Use the Deployment's own selector to find its pods (old and new during a rollout)."""
    return ",".join(f"{k}={v}" for k, v in sorted(d.spec.selector.match_labels.items()))


class DeploymentService:
    def __init__(self, gateway: Gateway):
        self.gateway = gateway

    def create(self, req: CreateDeploymentRequest) -> DeploymentDetail:
        self.gateway.ensure_namespace(req.namespace)
        try:
            created = self.gateway.create_deployment(req.namespace, build_deployment(req))
        except ApiException as e:
            if e.status == 409:
                raise ConflictError(f"Deployment '{req.name}' already exists in namespace '{req.namespace}'")
            raise
        return to_detail(created, pods=[])

    def list(self, namespace: str | None = None) -> list[DeploymentSummary]:
        deployments = self.gateway.list_deployments(namespace, MANAGED_SELECTOR)
        # One pods call for all deployments (not one per deployment), grouped by owner.
        pods_by_owner: dict[tuple[str, str], list] = {}
        for p in self.gateway.list_pods(namespace, MANAGED_SELECTOR):
            owner = (p.metadata.labels or {}).get(NAME_LABEL)
            pods_by_owner.setdefault((p.metadata.namespace, owner), []).append(p)
        summaries = [
            to_summary(d, pods_by_owner.get((d.metadata.namespace, d.metadata.name), [])) for d in deployments
        ]
        return sorted(summaries, key=lambda s: (s.namespace, s.name))

    def get(self, deployment_id: str) -> DeploymentDetail:
        d = self._find(deployment_id)
        return to_detail(d, self.gateway.list_pods(d.metadata.namespace, pod_selector(d)))

    def update(self, deployment_id: str, req: PatchDeploymentRequest) -> DeploymentDetail:
        """Scale and/or change the image. An image change alters the pod template,
        so the Deployment controller performs a rolling update."""
        d = self._find(deployment_id)
        spec: dict = {}
        if req.replicas is not None:
            spec["replicas"] = req.replicas
        if req.image is not None:
            # Strategic merge matches containers by name, so only the image changes.
            container = d.spec.template.spec.containers[0].name
            spec["template"] = {"spec": {"containers": [{"name": container, "image": req.image}]}}
        return self._patch(d, {"spec": spec})

    def restart(self, deployment_id: str) -> DeploymentDetail:
        """Same as `kubectl rollout restart`: stamp the pod template with the current
        time; the template changed, so the controller rolls out fresh pods."""
        d = self._find(deployment_id)
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        return self._patch(d, {"spec": {"template": {"metadata": {"annotations": {RESTARTED_AT_ANNOTATION: now}}}}})

    def _patch(self, d: V1Deployment, patch: dict) -> DeploymentDetail:
        updated = self.gateway.patch_deployment(d.metadata.namespace, d.metadata.name, patch)
        return to_detail(updated, self.gateway.list_pods(updated.metadata.namespace, pod_selector(updated)))

    def logs(self, deployment_id: str, pod: str | None, follow: bool, tail_lines: int) -> Iterator[str]:
        """Validate and open the log streams eagerly, so errors become normal JSON
        responses; only then return the iterator that the HTTP response streams."""
        d = self._find(deployment_id)
        ns, name = d.metadata.namespace, d.metadata.name
        pod_names = sorted(p.metadata.name for p in self.gateway.list_pods(ns, pod_selector(d)))

        if pod is not None:
            if pod not in pod_names:
                raise NotFoundError(f"Pod '{pod}' is not part of deployment '{name}'. Pods: {', '.join(pod_names) or 'none'}")
            try:
                return single_pod(self.gateway.open_pod_log(ns, pod, follow, tail_lines))
            except ApiException as e:
                if e.status == 400:
                    raise LogsUnavailableError(f"Logs for pod '{pod}' are not available: {kubernetes_message(e)}")
                raise

        if not pod_names:
            raise NotFoundError(f"Deployment '{name}' has no pods")
        streams, unavailable = {}, {}
        for pod_name in pod_names:
            try:
                streams[pod_name] = self.gateway.open_pod_log(ns, pod_name, follow, tail_lines)
            except ApiException as e:
                if e.status != 400:
                    raise
                unavailable[pod_name] = kubernetes_message(e)  # e.g. container waiting to start
        return all_pods(streams, unavailable, follow)

    def delete(self, deployment_id: str) -> DeleteResponse:
        d = self._find(deployment_id)
        self.gateway.delete_deployment(d.metadata.namespace, d.metadata.name)
        return DeleteResponse(id=d.metadata.uid, name=d.metadata.name, namespace=d.metadata.namespace)

    def _find(self, deployment_id: str) -> V1Deployment:
        """IDs are Deployment UIDs: find the managed Deployment with that UID (live lookup)."""
        for d in self.gateway.list_deployments(None, MANAGED_SELECTOR):
            if d.metadata.uid == deployment_id:
                return d
        raise NotFoundError(f"Deployment '{deployment_id}' not found")
