"""The only module that talks to the Kubernetes API.

Everything else goes through this small interface, so tests can swap in an
in-memory fake (tests/fakes.py) and run without a cluster.
"""

from typing import Protocol

from kubernetes import client, config
from kubernetes.client import V1Deployment, V1Namespace, V1ObjectMeta, V1Pod
from kubernetes.client.exceptions import ApiException

# Fail fast with a clear 500 instead of hanging when the API server is unreachable.
REQUEST_TIMEOUT_SECONDS = 10

# This client version defaults PATCH to JSON Patch (a list of operations); we send
# dicts, so ask for a strategic merge patch explicitly (what kubectl uses).
STRATEGIC_MERGE_PATCH = "application/strategic-merge-patch+json"


class Gateway(Protocol):
    def ensure_namespace(self, name: str) -> None: ...
    def create_deployment(self, namespace: str, body: V1Deployment) -> V1Deployment: ...
    def list_deployments(self, namespace: str | None, label_selector: str) -> list[V1Deployment]: ...
    def patch_deployment(self, namespace: str, name: str, patch: dict) -> V1Deployment: ...
    def delete_deployment(self, namespace: str, name: str) -> None: ...
    def list_pods(self, namespace: str | None, label_selector: str) -> list[V1Pod]: ...


class KubernetesGateway:
    """Real gateway. Loads credentials lazily on first use: in-cluster
    ServiceAccount if running in a pod, otherwise ~/.kube/config."""

    def __init__(self) -> None:
        self._apps: client.AppsV1Api | None = None
        self._core: client.CoreV1Api | None = None

    def _connect(self) -> None:
        if self._apps is None:
            try:
                config.load_incluster_config()
            except config.ConfigException:
                config.load_kube_config()
            self._apps = client.AppsV1Api()
            self._core = client.CoreV1Api()

    @property
    def apps(self) -> client.AppsV1Api:
        self._connect()
        return self._apps

    @property
    def core(self) -> client.CoreV1Api:
        self._connect()
        return self._core

    def ensure_namespace(self, name: str) -> None:
        try:
            self.core.read_namespace(name, _request_timeout=REQUEST_TIMEOUT_SECONDS)
            return
        except ApiException as e:
            if e.status != 404:
                raise
        try:
            self.core.create_namespace(
                V1Namespace(metadata=V1ObjectMeta(name=name)), _request_timeout=REQUEST_TIMEOUT_SECONDS
            )
        except ApiException as e:
            if e.status != 409:  # someone created it in the meantime: fine
                raise

    def create_deployment(self, namespace: str, body: V1Deployment) -> V1Deployment:
        return self.apps.create_namespaced_deployment(namespace, body, _request_timeout=REQUEST_TIMEOUT_SECONDS)

    def list_deployments(self, namespace: str | None, label_selector: str) -> list[V1Deployment]:
        if namespace:
            result = self.apps.list_namespaced_deployment(
                namespace, label_selector=label_selector, _request_timeout=REQUEST_TIMEOUT_SECONDS
            )
        else:
            result = self.apps.list_deployment_for_all_namespaces(
                label_selector=label_selector, _request_timeout=REQUEST_TIMEOUT_SECONDS
            )
        return result.items

    def patch_deployment(self, namespace: str, name: str, patch: dict) -> V1Deployment:
        # Applied atomically by the API server: no read-modify-write, no conflict retries.
        return self.apps.patch_namespaced_deployment(
            name, namespace, patch, _content_type=STRATEGIC_MERGE_PATCH, _request_timeout=REQUEST_TIMEOUT_SECONDS
        )

    def delete_deployment(self, namespace: str, name: str) -> None:
        # Foreground: Kubernetes deletes the owned ReplicaSets and Pods first
        # (via ownerReferences), then the Deployment itself.
        self.apps.delete_namespaced_deployment(
            name, namespace, propagation_policy="Foreground", _request_timeout=REQUEST_TIMEOUT_SECONDS
        )

    def list_pods(self, namespace: str | None, label_selector: str) -> list[V1Pod]:
        if namespace:
            result = self.core.list_namespaced_pod(
                namespace, label_selector=label_selector, _request_timeout=REQUEST_TIMEOUT_SECONDS
            )
        else:
            result = self.core.list_pod_for_all_namespaces(
                label_selector=label_selector, _request_timeout=REQUEST_TIMEOUT_SECONDS
            )
        return result.items
