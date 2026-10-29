"""In-memory stand-in for KubernetesGateway, so API tests run without a cluster.

It mimics what the API server does that matters to us: UIDs, timestamps,
409 on duplicates, 404 on missing objects, label-selector filtering.
It does NOT run controllers, so tests set status fields themselves.
"""

import copy
import uuid
from datetime import datetime, timezone

from kubernetes.client import V1Deployment, V1DeploymentStatus, V1Pod
from kubernetes.client.exceptions import ApiException


def _matches(labels: dict | None, selector: str) -> bool:
    labels = labels or {}
    for term in filter(None, selector.split(",")):
        key, _, value = term.partition("=")
        if labels.get(key) != value:
            return False
    return True


class FakeGateway:
    def __init__(self):
        self.namespaces: set[str] = {"default"}
        self.deployments: dict[tuple[str, str], V1Deployment] = {}
        self.pods: list[V1Pod] = []
        self.deleted: list[tuple[str, str]] = []
        self.patches: list[tuple[str, str, dict]] = []
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
        del self.deployments[(namespace, name)]
        self.deleted.append((namespace, name))

    def list_pods(self, namespace: str | None, label_selector: str) -> list[V1Pod]:
        self._maybe_fail()
        return [
            copy.deepcopy(p)
            for p in self.pods
            if (namespace is None or p.metadata.namespace == namespace) and _matches(p.metadata.labels, label_selector)
        ]

    # ---- test helpers ----
    def stored(self, namespace: str, name: str) -> V1Deployment:
        return self.deployments[(namespace, name)]
