import pytest
from fastapi.testclient import TestClient
from kubernetes.client import (
    V1Container,
    V1ContainerState,
    V1ContainerStateWaiting,
    V1ContainerStatus,
    V1Deployment,
    V1DeploymentSpec,
    V1LabelSelector,
    V1ObjectMeta,
    V1Pod,
    V1PodSpec,
    V1PodStatus,
    V1PodTemplateSpec,
)
from kubernetes.client.exceptions import ApiException
from urllib3.exceptions import MaxRetryError

from app.constants import MANAGED_BY_LABEL, NAME_LABEL
from app.main import create_app
from tests.fakes import FakeGateway


@pytest.fixture
def gateway():
    return FakeGateway()


@pytest.fixture
def api(gateway):
    return TestClient(create_app(gateway), raise_server_exceptions=False)


def create(api, **overrides):
    body = {"name": "web", "image": "nginx:latest", "namespace": "dev", "replicas": 3}
    body.update(overrides)
    return api.post("/api/deployments", json=body)


def add_pod(gateway, namespace, deployment, name, reason="ImagePullBackOff"):
    gateway.pods.append(V1Pod(
        metadata=V1ObjectMeta(name=name, namespace=namespace,
                              labels={MANAGED_BY_LABEL: "deploy-api", NAME_LABEL: deployment}),
        spec=V1PodSpec(node_name="k3d-learn-agent-0", containers=[V1Container(name=deployment, image="nginx:bad")]),
        status=V1PodStatus(phase="Pending", container_statuses=[V1ContainerStatus(
            name=deployment, image="nginx:bad", image_id="", ready=False, restart_count=0,
            state=V1ContainerState(waiting=V1ContainerStateWaiting(reason=reason, message="Back-off pulling image")),
        )]),
    ))


# ---------- POST /api/deployments ----------

def test_create_returns_201_with_uid_and_status(api):
    r = create(api)
    assert r.status_code == 201
    body = r.json()
    assert body["id"] and body["name"] == "web" and body["namespace"] == "dev"
    assert body["image"] == "nginx:latest"
    assert body["replicas"]["desired"] == 3
    assert body["labels"][MANAGED_BY_LABEL] == "deploy-api"
    assert body["pods"] == []


def test_create_builds_the_full_deployment_spec(api, gateway):
    create(api, port=8080, labels={"team": "platform"},
           env=[{"name": "MODE", "value": "demo"}],
           resources={"requests": {"cpu": "100m", "memory": "64Mi"}, "limits": {"cpu": "500m", "memory": "128Mi"}},
           command=["sh", "-c"], args=["echo hi"])
    d = gateway.stored("dev", "web")
    c = d.spec.template.spec.containers[0]
    assert c.image == "nginx:latest" and c.ports[0].container_port == 8080
    assert c.env[0].name == "MODE" and c.env[0].value == "demo"
    assert c.resources.requests == {"cpu": "100m", "memory": "64Mi"}
    assert c.resources.limits == {"cpu": "500m", "memory": "128Mi"}
    assert c.command == ["sh", "-c"] and c.args == ["echo hi"]
    assert d.spec.selector.match_labels == {NAME_LABEL: "web"}
    assert d.spec.template.metadata.labels["team"] == "platform"
    assert d.metadata.labels["team"] == "platform"


def test_create_creates_missing_namespace(api, gateway):
    create(api, namespace="brand-new")
    assert "brand-new" in gateway.namespaces


def test_create_duplicate_returns_409(api):
    assert create(api).status_code == 201
    r = create(api)
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "CONFLICT"
    assert "already exists" in r.json()["error"]["message"]


def test_same_name_in_other_namespace_is_allowed(api):
    assert create(api).status_code == 201
    assert create(api, namespace="other").status_code == 201


def test_create_invalid_returns_400_with_field_details(api):
    r = create(api, name="Bad_Name", replicas=-1, image="not valid")
    assert r.status_code == 400
    err = r.json()["error"]
    assert err["code"] == "INVALID_INPUT"
    assert {"name", "replicas", "image"} <= {d["field"] for d in err["details"]}
    assert all(not d["message"].startswith("Value error") for d in err["details"])


def test_create_malformed_json_returns_400(api):
    r = api.post("/api/deployments", content="{not json", headers={"Content-Type": "application/json"})
    assert r.status_code == 400


# ---------- GET /api/deployments ----------

def test_list_returns_managed_deployments_only(api, gateway):
    create(api, name="a")
    create(api, name="b", namespace="other")
    # A deployment we did not create (no managed-by label) must never appear.
    gateway.deployments[("dev", "foreign")] = V1Deployment(
        metadata=V1ObjectMeta(name="foreign", namespace="dev", uid="x", labels={"app": "foreign"}),
        spec=V1DeploymentSpec(selector=V1LabelSelector(match_labels={"app": "foreign"}), template=V1PodTemplateSpec(
            spec=V1PodSpec(containers=[V1Container(name="c", image="nginx")]))),
    )
    r = api.get("/api/deployments")
    assert r.status_code == 200
    assert r.json()["count"] == 2
    assert [(d["namespace"], d["name"]) for d in r.json()["items"]] == [("dev", "a"), ("other", "b")]


def test_list_filters_by_namespace(api):
    create(api, name="a")
    create(api, name="b", namespace="other")
    r = api.get("/api/deployments", params={"namespace": "other"})
    assert [d["name"] for d in r.json()["items"]] == ["b"]


def test_list_rejects_invalid_namespace_filter(api):
    r = api.get("/api/deployments", params={"namespace": "Not_Valid"})
    assert r.status_code == 400
    assert r.json()["error"]["details"][0]["field"] == "namespace"


# ---------- GET /api/deployments/{id} ----------

def test_get_returns_detail_with_pods(api, gateway):
    dep_id = create(api).json()["id"]
    add_pod(gateway, "dev", "web", "web-abc")
    add_pod(gateway, "dev", "other-app", "other-xyz")  # belongs to a different deployment
    r = api.get(f"/api/deployments/{dep_id}")
    assert r.status_code == 200
    pods = r.json()["pods"]
    assert [p["name"] for p in pods] == ["web-abc"]
    container = pods[0]["containers"][0]
    assert container["state"] == "waiting" and container["reason"] == "ImagePullBackOff"
    assert pods[0]["node"] == "k3d-learn-agent-0" and pods[0]["phase"] == "Pending"
    # The same failure is reported at the deployment level too.
    assert r.json()["health"] == "FAILING"
    assert "ImagePullBackOff" in r.json()["health_reason"]


def test_list_reports_health_per_deployment(api, gateway):
    create(api, name="good")
    create(api, name="bad")
    add_pod(gateway, "dev", "bad", "bad-1")
    health = {d["name"]: d["health"] for d in api.get("/api/deployments").json()["items"]}
    assert health["bad"] == "FAILING"
    assert health["good"] == "PROGRESSING"  # fake has no controller: never observed


def test_get_unknown_id_returns_404(api):
    r = api.get("/api/deployments/does-not-exist")
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "NOT_FOUND"


# ---------- DELETE /api/deployments/{id} ----------

def test_delete_returns_202_and_removes_deployment(api, gateway):
    dep_id = create(api).json()["id"]
    r = api.delete(f"/api/deployments/{dep_id}")
    assert r.status_code == 202
    assert r.json() == {"id": dep_id, "name": "web", "namespace": "dev",
                        "status": "DELETING", "propagation_policy": "Foreground"}
    assert gateway.deleted == [("dev", "web")]
    assert api.get(f"/api/deployments/{dep_id}").status_code == 404


def test_delete_unknown_id_returns_404(api):
    assert api.delete("/api/deployments/nope").status_code == 404


# ---------- Kubernetes failures ----------

def test_kubernetes_api_error_returns_500_with_message(api, gateway):
    gateway.fail_with = ApiException(status=500, reason="Internal Server Error",
                                     body='{"message": "etcdserver: request timed out"}')
    r = api.get("/api/deployments")
    assert r.status_code == 500
    assert r.json()["error"]["code"] == "KUBERNETES_ERROR"
    assert "etcdserver: request timed out" in r.json()["error"]["message"]


def test_kubernetes_forbidden_returns_500_with_message(api, gateway):
    gateway.fail_with = ApiException(status=403, reason="Forbidden",
                                     body='{"message": "deployments.apps is forbidden"}')
    r = create(api)
    assert r.status_code == 500
    assert "forbidden" in r.json()["error"]["message"]


def test_api_server_unreachable_returns_500(api, gateway):
    gateway.fail_with = MaxRetryError(pool=None, url="/apis/apps/v1/deployments", reason="Connection refused")
    r = api.get("/api/deployments")
    assert r.status_code == 500
    assert r.json()["error"]["code"] == "KUBERNETES_UNREACHABLE"
