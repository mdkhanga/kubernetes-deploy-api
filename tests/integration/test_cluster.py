"""End-to-end tests: real HTTP requests through the app to a real Kubernetes cluster.

Run with:  pytest -m integration -v
"""

import pytest
from kubernetes import client
from kubernetes.client.exceptions import ApiException

from tests.integration.conftest import wait_for

pytestmark = pytest.mark.integration

IMAGE = "nginx:1.27"


def create(api, namespace, **fields):
    return api.post("/api/deployments", json={"namespace": namespace, "image": IMAGE, **fields})


def detail(api, dep_id):
    return api.get(f"/api/deployments/{dep_id}").json()


def wait_for_health(api, dep_id, health, timeout=120, **expected_replicas):
    def check():
        d = detail(api, dep_id)
        ok = d.get("health") == health and all(d["replicas"][k] == v for k, v in expected_replicas.items())
        return d if ok else None
    return wait_for(check, timeout=timeout, what=f"{health} {expected_replicas or ''}")


def test_deployment_lifecycle(api, namespace):
    # Create: the namespace does not exist yet; the API creates it.
    r = create(api, namespace, name="web", replicas=2, port=80)
    assert r.status_code == 201, r.text
    dep_id = r.json()["id"]
    assert client.CoreV1Api().read_namespace(namespace).metadata.name == namespace

    # Appears in the list, with and without the namespace filter.
    assert dep_id in [d["id"] for d in api.get("/api/deployments", params={"namespace": namespace}).json()["items"]]
    assert dep_id in [d["id"] for d in api.get("/api/deployments").json()["items"]]

    # Becomes healthy, with real pods on real nodes.
    d = wait_for_health(api, dep_id, "HEALTHY", ready=2)
    assert len(d["pods"]) == 2
    assert all(p["phase"] == "Running" and p["node"] and p["ready"] for p in d["pods"])

    # Scale: the replica count changes in the API and in Kubernetes.
    r = api.patch(f"/api/deployments/{dep_id}", json={"replicas": 3})
    assert r.status_code == 200 and r.json()["replicas"]["desired"] == 3
    wait_for_health(api, dep_id, "HEALTHY", desired=3, ready=3, available=3)
    assert client.AppsV1Api().read_namespaced_deployment("web", namespace).spec.replicas == 3

    # Delete: gone from the API, from Kubernetes, and its pods are gone too (cascade).
    assert api.delete(f"/api/deployments/{dep_id}").status_code == 202
    wait_for(lambda: api.get(f"/api/deployments/{dep_id}").status_code == 404, timeout=90, what="404 after delete")
    with pytest.raises(ApiException) as exc:
        client.AppsV1Api().read_namespaced_deployment("web", namespace)
    assert exc.value.status == 404
    assert not client.CoreV1Api().list_namespaced_pod(namespace, label_selector="deploy-api/name=web").items


def test_bad_image_is_reported_as_failing(api, namespace):
    r = create(api, namespace, name="broken", image="nginx:nonexistent", replicas=1)
    assert r.status_code == 201, r.text
    d = wait_for_health(api, r.json()["id"], "FAILING", timeout=90)

    # Deployment level: FAILING with the reason; pod level: container waiting on the pull error.
    assert "ImagePullBackOff" in d["health_reason"] or "ErrImagePull" in d["health_reason"]
    assert d["replicas"]["ready"] == 0
    pod = d["pods"][0]
    assert pod["phase"] == "Pending" and pod["node"]
    container = pod["containers"][0]
    assert container["state"] == "waiting" and container["reason"] in {"ImagePullBackOff", "ErrImagePull"}


def test_duplicate_name_returns_409(api, namespace):
    assert create(api, namespace, name="dup").status_code == 201
    r = create(api, namespace, name="dup")
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "CONFLICT"


def test_service_exposure_routes_to_pods_and_is_garbage_collected(api, namespace):
    r = create(api, namespace, name="exposed", replicas=2, port=80, service={"type": "NodePort"})
    assert r.status_code == 201, r.text
    dep_id, svc = r.json()["id"], r.json()["service"]
    assert svc["type"] == "NodePort" and 30000 <= svc["node_port"] <= 32767

    # The real Service exists, is owned by the Deployment, and routes to both ready pods.
    core = client.CoreV1Api()
    k8s_svc = core.read_namespaced_service("exposed", namespace)
    assert k8s_svc.metadata.owner_references[0].uid == dep_id
    wait_for_health(api, dep_id, "HEALTHY", ready=2)

    def ready_endpoints():
        slices = client.DiscoveryV1Api().list_namespaced_endpoint_slice(
            namespace, label_selector="kubernetes.io/service-name=exposed").items
        return sum(1 for s in slices for e in (s.endpoints or []) if e.conditions and e.conditions.ready) == 2

    wait_for(ready_endpoints, timeout=60, what="2 ready endpoints behind the Service")

    # Deleting the Deployment garbage-collects the Service (ownerReference).
    assert api.delete(f"/api/deployments/{dep_id}").status_code == 202

    def service_gone():
        try:
            core.read_namespaced_service("exposed", namespace)
            return False
        except ApiException as e:
            return e.status == 404

    wait_for(service_gone, timeout=90, what="Service to be garbage-collected")


def test_restart_replaces_pods(api, namespace):
    dep_id = create(api, namespace, name="restartme", replicas=1).json()["id"]
    before = {p["name"] for p in wait_for_health(api, dep_id, "HEALTHY", ready=1)["pods"]}

    r = api.post(f"/api/deployments/{dep_id}/restart")
    assert r.status_code == 202 and r.json()["restarted_at"]

    def replaced():
        d = detail(api, dep_id)
        names = {p["name"] for p in d["pods"]}
        return d if d["health"] == "HEALTHY" and names and not names & before else None

    wait_for(replaced, timeout=120, what="restarted pods to replace the old ones")
