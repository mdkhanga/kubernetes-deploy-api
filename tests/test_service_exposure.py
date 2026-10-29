"""Optional Service created alongside the Deployment (stretch goal: service exposure)."""

import pytest
from fastapi.testclient import TestClient
from kubernetes.client.exceptions import ApiException

from app.constants import NAME_LABEL
from app.main import create_app
from tests.fakes import FakeGateway


@pytest.fixture
def gateway():
    return FakeGateway()


@pytest.fixture
def api(gateway):
    return TestClient(create_app(gateway), raise_server_exceptions=False)


def create(api, **overrides):
    body = {"name": "web", "image": "nginx:1.27", "namespace": "dev", "replicas": 2, "port": 80}
    body.update(overrides)
    return api.post("/api/deployments", json=body)


def test_clusterip_service_created_and_owned_by_deployment(api, gateway):
    r = create(api, service={"type": "ClusterIP"})
    assert r.status_code == 201, r.text
    body = r.json()
    svc = gateway.services[("dev", "web")]
    # Selects exactly this deployment's pods and targets the container port.
    assert svc.spec.selector == {NAME_LABEL: "web"}
    assert svc.spec.ports[0].port == 80 and svc.spec.ports[0].target_port == 80  # port defaults to container port
    # Owned by the Deployment, so deleting the Deployment garbage-collects it.
    (owner,) = svc.metadata.owner_references
    assert (owner.kind, owner.name, owner.uid) == ("Deployment", "web", body["id"])
    # Access info in the response.
    assert body["service"]["type"] == "ClusterIP" and body["service"]["cluster_ip"]
    assert body["service"]["urls"] == ["http://web.dev.svc.cluster.local:80"]


def test_nodeport_service_with_explicit_ports(api, gateway):
    r = create(api, port=8080, service={"type": "NodePort", "port": 80, "node_port": 31080})
    assert r.status_code == 201, r.text
    svc = r.json()["service"]
    assert (svc["type"], svc["port"], svc["target_port"], svc["node_port"]) == ("NodePort", 80, 8080, 31080)
    assert "http://<node-ip>:31080" in svc["urls"]


def test_service_shown_in_get_list_and_patch(api):
    dep_id = create(api, service={"type": "NodePort"}).json()["id"]
    assert api.get(f"/api/deployments/{dep_id}").json()["service"]["type"] == "NodePort"
    assert api.get("/api/deployments").json()["items"][0]["service"]["node_port"]
    assert api.patch(f"/api/deployments/{dep_id}", json={"replicas": 3}).json()["service"]["name"] == "web"


def test_no_service_by_default(api, gateway):
    assert create(api).json()["service"] is None
    assert gateway.services == {}


def test_deleting_the_deployment_removes_its_service(api, gateway):
    dep_id = create(api, service={"type": "ClusterIP"}).json()["id"]
    assert api.delete(f"/api/deployments/{dep_id}").status_code == 202
    assert gateway.services == {}


@pytest.mark.parametrize("overrides", [
    {"port": None, "service": {"type": "ClusterIP"}},                    # nothing to target
    {"service": {"type": "LoadBalancer"}},                               # unsupported type
    {"service": {"type": "ClusterIP", "node_port": 31000}},              # node_port needs NodePort
    {"service": {"type": "NodePort", "node_port": 80}},                  # outside 30000-32767
    {"service": {"type": "ClusterIP", "port": 0}},
    {"service": {"type": "ClusterIP", "extra": True}},
])
def test_invalid_service_requests_return_400(api, gateway, overrides):
    body = {"name": "web", "image": "nginx:1.27", "namespace": "dev", "port": 80, **overrides}
    body = {k: v for k, v in body.items() if v is not None}
    r = api.post("/api/deployments", json=body)
    assert r.status_code == 400, r.text
    assert gateway.deployments == {}


def test_service_failure_rolls_back_the_deployment(api, gateway):
    gateway.service_error = ApiException(status=422, reason="Unprocessable Entity",
                                         body='{"message": "provided port is already allocated"}')
    r = create(api, service={"type": "NodePort", "node_port": 31080})
    assert r.status_code == 400
    assert "already allocated" in r.json()["error"]["message"]
    assert gateway.deployments == {} and gateway.deleted == [("dev", "web")]


def test_existing_service_name_returns_409_and_rolls_back(api, gateway):
    gateway.service_error = ApiException(status=409, reason="Conflict")
    r = create(api, service={"type": "ClusterIP"})
    assert r.status_code == 409
    assert "Service 'web' already exists" in r.json()["error"]["message"]
    assert gateway.deployments == {}
