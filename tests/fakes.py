"""In-memory stand-in for KubernetesGateway, so API tests run without a cluster.

It mimics what the API server does that matters to us: UIDs, timestamps,
409 on duplicates, 404 on missing objects, label-selector filtering.
It does NOT run controllers, so tests set status fields themselves.
"""

import copy
import uuid
from datetime import datetime, timezone

from kubernetes.client import V1Deployment, V1DeploymentStatus, V1Pod, V1Service
from kubernetes.client.exceptions import ApiException


def _matches(labels: dict | None, selector: str) -> bool:
    labels = labels or {}
    for term in filter(None, selector.split(",")):
        key, _, value = term.partition("=")
        if labels.get(key) != value:
            return False
    return True


class FakeLogStream:
    def __init__(self, lines: list[str]):
        self._lines = lines
        self.closed = False

    def lines(self):
        yield from self._lines

    def close(self) -> None:
        self.closed = True


class FakeGateway:
    def __init__(self):
        self.namespaces: set[str] = {"default"}
        self.deployments: dict[tuple[str, str], V1Deployment] = {}
        self.pods: list[V1Pod] = []
        self.deleted: list[tuple[str, str]] = []
        self.patches: list[tuple[str, str, dict]] = []
        self.logs: dict[str, list[str]] = {}               # pod name -> log lines
        self.log_errors: dict[str, Exception] = {}         # pod name -> error when opening its log
        self.log_calls: list[tuple[str, str, bool, int]] = []
        self.opened_logs: list["FakeLogStream"] = []
        self.services: dict[tuple[str, str], V1Service] = {}
        self.service_error: Exception | None = None  # set to make create_service fail
        self.fail_with: Exception | None = None  # set to simulate API failures

    def _maybe_fail(self):
        if self.fail_with:
            raise self.fail_with

    def ensure_namespace(self, name: str) -> None:
        self._maybe_fail()
        self.namespaces.add(name)

    def create_deployment(self, namespace: str, body: V1Deployment) -> V1Deployment:
        self._maybe_fail()
        key = (namespace, body.metadata.name)
        if key in self.deployments:
            raise ApiException(status=409, reason="Conflict")
        d = copy.deepcopy(body)
        d.metadata.namespace = namespace
        d.metadata.uid = str(uuid.uuid4())
        d.metadata.creation_timestamp = datetime.now(timezone.utc)
        d.metadata.generation = 1
        d.status = V1DeploymentStatus()
        self.deployments[key] = d
        return copy.deepcopy(d)

    def list_deployments(self, namespace: str | None, label_selector: str) -> list[V1Deployment]:
        self._maybe_fail()
        return [
            copy.deepcopy(d)
            for (ns, _), d in self.deployments.items()
            if (namespace is None or ns == namespace) and _matches(d.metadata.labels, label_selector)
        ]

    def patch_deployment(self, namespace: str, name: str, patch: dict) -> V1Deployment:
        """Applies the subset of strategic merge patch the service sends:
        replicas, container image (matched by name), pod template annotations."""
        self._maybe_fail()
        if (namespace, name) not in self.deployments:
            raise ApiException(status=404, reason="Not Found")
        self.patches.append((namespace, name, copy.deepcopy(patch)))
        d = self.deployments[(namespace, name)]
        spec = patch.get("spec", {})
        if "replicas" in spec:
            d.spec.replicas = spec["replicas"]
        template = spec.get("template", {})
        for change in template.get("spec", {}).get("containers", []):
            for c in d.spec.template.spec.containers:
                if c.name == change["name"]:
                    c.image = change["image"]
        annotations = template.get("metadata", {}).get("annotations")
        if annotations:
            d.spec.template.metadata.annotations = {**(d.spec.template.metadata.annotations or {}), **annotations}
        d.metadata.generation += 1
        return copy.deepcopy(d)

    def delete_deployment(self, namespace: str, name: str) -> None:
        self._maybe_fail()
        if (namespace, name) not in self.deployments:
            raise ApiException(status=404, reason="Not Found")
        uid = self.deployments.pop((namespace, name)).metadata.uid
        self.deleted.append((namespace, name))
        # Garbage collection: objects owned by the deleted Deployment go too.
        for key, svc in list(self.services.items()):
            if any(ref.uid == uid for ref in (svc.metadata.owner_references or [])):
                del self.services[key]

    def create_service(self, namespace: str, body: V1Service) -> V1Service:
        self._maybe_fail()
        if self.service_error:
            raise self.service_error
        key = (namespace, body.metadata.name)
        if key in self.services:
            raise ApiException(status=409, reason="Conflict")
        svc = copy.deepcopy(body)
        svc.metadata.namespace = namespace
        svc.metadata.uid = str(uuid.uuid4())
        svc.spec.cluster_ip = f"10.43.0.{len(self.services) + 10}"
        port = svc.spec.ports[0]
        if svc.spec.type == "NodePort" and port.node_port is None:
            port.node_port = 30000 + len(self.services)
        self.services[key] = svc
        return copy.deepcopy(svc)

    def list_services(self, namespace: str | None, label_selector: str) -> list[V1Service]:
        self._maybe_fail()
        return [
            copy.deepcopy(s)
            for (ns, _), s in self.services.items()
            if (namespace is None or ns == namespace) and _matches(s.metadata.labels, label_selector)
        ]

    def list_pods(self, namespace: str | None, label_selector: str) -> list[V1Pod]:
        self._maybe_fail()
        return [
            copy.deepcopy(p)
            for p in self.pods
            if (namespace is None or p.metadata.namespace == namespace) and _matches(p.metadata.labels, label_selector)
        ]

    def open_pod_log(self, namespace: str, pod: str, follow: bool, tail_lines: int) -> "FakeLogStream":
        self._maybe_fail()
        self.log_calls.append((namespace, pod, follow, tail_lines))
        if pod in self.log_errors:
            raise self.log_errors[pod]
        stream = FakeLogStream(self.logs.get(pod, []))
        self.opened_logs.append(stream)
        return stream

    # ---- test helpers ----
    def stored(self, namespace: str, name: str) -> V1Deployment:
        return self.deployments[(namespace, name)]
