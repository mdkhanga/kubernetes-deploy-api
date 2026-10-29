"""Fixtures for integration tests against a real cluster (current kubeconfig context).

Each run works in a fresh namespace `deploy-api-it-<random>`, created by the API
itself and deleted at the end, so it never touches other workloads.
"""

import time
import uuid

import pytest
from fastapi.testclient import TestClient
from kubernetes import client, config
from kubernetes.client.exceptions import ApiException

from app.k8s_gateway import KubernetesGateway
from app.main import create_app


def _connect() -> bool:
    try:
        try:
            config.load_incluster_config()
        except config.ConfigException:
            config.load_kube_config()
        client.VersionApi().get_code(_request_timeout=5)
        return True
    except Exception:
        return False


@pytest.fixture(scope="session")
def cluster():
    if not _connect():
        pytest.skip("No reachable Kubernetes cluster (check your kubeconfig context)")


@pytest.fixture(scope="session")
def api(cluster):
    """The real app (HTTP -> service -> real KubernetesGateway), in-process: no server to start."""
    return TestClient(create_app(KubernetesGateway()), raise_server_exceptions=False)


@pytest.fixture(scope="session")
def namespace(cluster):
    name = f"deploy-api-it-{uuid.uuid4().hex[:6]}"
    yield name
    try:
        client.CoreV1Api().delete_namespace(name)  # removes everything the tests created
    except ApiException as e:
        if e.status != 404:
            raise


def wait_for(check, timeout=120, interval=2, what="condition"):
    """Poll `check()` until it returns something truthy; Kubernetes is asynchronous."""
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        result = check()
        if result:
            return result
        last = result
        time.sleep(interval)
    raise AssertionError(f"Timed out after {timeout}s waiting for {what} (last: {last!r})")
