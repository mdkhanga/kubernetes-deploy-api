import pytest
from pydantic import ValidationError

from app import validation
from app.models import CreateDeploymentRequest, PatchDeploymentRequest


# ---------- Rule functions ----------

@pytest.mark.parametrize("name", ["web", "my-app", "a", "app1", "1app", "a" * 63])
def test_dns_label_valid(name):
    assert validation.validate_dns_label(name) == name


@pytest.mark.parametrize("name", ["", "My-App", "my_app", "-web", "web-", "web.app", "a" * 64, "web app"])
def test_dns_label_invalid(name):
    with pytest.raises(ValueError):
        validation.validate_dns_label(name)


@pytest.mark.parametrize("image", [
    "nginx",
    "nginx:latest",
    "nginx:1.27",
    "hashicorp/http-echo:latest",
    "registry.example.com/team/app:v1",
    "localhost:5000/app:dev",
    "ghcr.io/org/my_app:1.0.0-rc.1",
    "nginx@sha256:" + "a" * 64,
])
def test_image_valid(image):
    assert validation.validate_image(image) == image


@pytest.mark.parametrize("image", ["", "Nginx", "nginx:", "nginx latest", ":latest", "nginx:-bad", "nginx@sha256:xyz", "/nginx"])
def test_image_invalid(image):
    with pytest.raises(ValueError):
        validation.validate_image(image)


@pytest.mark.parametrize("value", ["100m", "0.5", "1", "128Mi", "1Gi", "500M"])
def test_quantity_valid(value):
    assert validation.parse_positive_quantity(value) > 0


@pytest.mark.parametrize("value", ["abc", "10x", "-1", "0", "", "1.2.3"])
def test_quantity_invalid(value):
    with pytest.raises(ValueError):
        validation.parse_positive_quantity(value)


def test_requests_within_limits():
    validation.validate_requests_within_limits({"cpu": "100m", "memory": "64Mi"}, {"cpu": "1", "memory": "128Mi"})
    with pytest.raises(ValueError, match="cpu request"):
        validation.validate_requests_within_limits({"cpu": "2"}, {"cpu": "500m"})
    with pytest.raises(ValueError, match="memory request"):
        validation.validate_requests_within_limits({"memory": "1Gi"}, {"memory": "512Mi"})


@pytest.mark.parametrize("key", ["team", "app.kubernetes.io/part-of", "example.com/tier", "a_b.c-d"])
def test_label_key_valid(key):
    assert validation.validate_label_key(key) == key


@pytest.mark.parametrize("key", ["", "-team", "team-", "Bad_Prefix.com/x", "a/" , "app.kubernetes.io/managed-by", "deploy-api/name"])
def test_label_key_invalid(key):
    with pytest.raises(ValueError):
        validation.validate_label_key(key)


@pytest.mark.parametrize("value", ["", "frontend", "v1.2", "a_b"])
def test_label_value_valid(value):
    assert validation.validate_label_value(value) == value


@pytest.mark.parametrize("value", ["has space", "-x", "x" * 64])
def test_label_value_invalid(value):
    with pytest.raises(ValueError):
        validation.validate_label_value(value)


# ---------- Request models ----------

def error_fields(exc: ValidationError) -> set[str]:
    return {".".join(str(p) for p in err["loc"]) for err in exc.errors()}


def test_create_minimal_applies_defaults():
    req = CreateDeploymentRequest(name="web", image="nginx:latest")
    assert req.namespace == "default"
    assert req.replicas == 1
    assert req.env == [] and req.labels == {}
    assert req.port is None and req.resources is None


def test_create_full_request_is_valid():
    req = CreateDeploymentRequest(
        name="echo",
        namespace="demo",
        image="hashicorp/http-echo:latest",
        replicas=3,
        port=5678,
        resources={"requests": {"cpu": "50m", "memory": "32Mi"}, "limits": {"cpu": "200m", "memory": "64Mi"}},
        env=[{"name": "MODE", "value": "test"}],
        labels={"team": "platform"},
        command=["/http-echo"],
        args=["-text=hello"],
    )
    assert req.resources.limits.cpu == "200m"


@pytest.mark.parametrize("body, field", [
    ({"name": "Bad_Name", "image": "nginx"}, "name"),
    ({"name": "web", "image": "nginx", "namespace": "Dev"}, "namespace"),
    ({"name": "web", "image": "not a valid image"}, "image"),
    ({"name": "web", "image": "nginx", "replicas": -1}, "replicas"),
    ({"name": "web", "image": "nginx", "replicas": 1000}, "replicas"),
    ({"name": "web", "image": "nginx", "port": 0}, "port"),
    ({"name": "web", "image": "nginx", "port": 70000}, "port"),
    ({"name": "web", "image": "nginx", "resources": {"requests": {"cpu": "lots"}}}, "resources.requests.cpu"),
    ({"name": "web", "image": "nginx", "resources": {"requests": {"cpu": "2"}, "limits": {"cpu": "1"}}}, "resources"),
    ({"name": "web", "image": "nginx", "env": [{"name": "1BAD"}]}, "env.0.name"),
    ({"name": "web", "image": "nginx", "env": [{"name": "A"}, {"name": "A"}]}, "env"),
    ({"name": "web", "image": "nginx", "labels": {"app.kubernetes.io/managed-by": "me"}}, "labels"),
    ({"name": "web", "image": "nginx", "command": []}, "command"),
    ({"name": "web", "image": "nginx", "unknown_field": 1}, "unknown_field"),
    ({"image": "nginx"}, "name"),
    ({"name": "web"}, "image"),
])
def test_create_invalid(body, field):
    with pytest.raises(ValidationError) as exc:
        CreateDeploymentRequest(**body)
    assert field in error_fields(exc.value)


def test_patch_requires_a_change():
    with pytest.raises(ValidationError, match="at least one"):
        PatchDeploymentRequest()


def test_patch_valid_and_invalid():
    assert PatchDeploymentRequest(replicas=0).replicas == 0
    assert PatchDeploymentRequest(image="nginx:1.27").image == "nginx:1.27"
    with pytest.raises(ValidationError):
        PatchDeploymentRequest(replicas=-2)
    with pytest.raises(ValidationError):
        PatchDeploymentRequest(image="BAD IMAGE")
