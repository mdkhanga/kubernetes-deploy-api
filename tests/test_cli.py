"""CLI tests: commands run against a mocked HTTP API (httpx.MockTransport), no server needed."""

import json

import httpx
import pytest
from typer.testing import CliRunner

from cli import deploy

WEB_ID = "11111111-2222-3333-4444-555555555555"
BROKEN_ID = "99999999-8888-7777-6666-555555555555"


def summary(id_, name, health, reason, ready, desired=3, namespace="dev", image="nginx:latest"):
    return {
        "id": id_, "name": name, "namespace": namespace, "image": image, "labels": {},
        "replicas": {"desired": desired, "current": desired, "updated": desired, "ready": ready,
                     "available": ready, "unavailable": desired - ready},
        "health": health, "health_reason": reason, "conditions": [],
        "created_at": "2026-10-29T16:43:48Z", "last_updated_at": "2026-10-29T16:43:59Z", "restarted_at": None,
    }


WEB = summary(WEB_ID, "web", "HEALTHY", "All 3 replicas ready", ready=3)
BROKEN = summary(BROKEN_ID, "broken", "FAILING", "ImagePullBackOff in 2 pod(s): broken-a, broken-b (0/2 ready)",
                 ready=0, desired=2, image="nginx:nonexistent")
BROKEN_DETAIL = {
    **BROKEN,
    "conditions": [{"type": "Available", "status": "False", "reason": "MinimumReplicasUnavailable",
                    "message": "Deployment does not have minimum availability.",
                    "last_update_time": "2026-10-29T16:44:00Z", "last_transition_time": "2026-10-29T16:44:00Z"}],
    "pods": [{"name": "broken-a", "phase": "Pending", "node": "k3d-learn-agent-0", "ready": False, "restart_count": 0,
              "reason": None, "message": None, "created_at": "2026-10-29T16:44:00Z",
              "containers": [{"name": "broken", "image": "nginx:nonexistent", "ready": False, "restart_count": 0,
                              "state": "waiting", "reason": "ImagePullBackOff", "message": "Back-off", "exit_code": None}]}],
}


class FakeApi:
    """Records requests and answers from a (method, path) -> (status, body) table."""

    def __init__(self):
        self.requests: list[httpx.Request] = []
        self.routes = {
            ("GET", "/api/deployments"): (200, {"items": [BROKEN, WEB], "count": 2}),
            ("GET", f"/api/deployments/{BROKEN_ID}"): (200, BROKEN_DETAIL),
            ("POST", "/api/deployments"): (201, {**WEB, "health": "PROGRESSING", "health_reason": "Rollout in progress"}),
            ("PATCH", f"/api/deployments/{WEB_ID}"): (200, WEB),
            ("POST", f"/api/deployments/{WEB_ID}/restart"): (202, {**WEB, "restarted_at": "2026-10-29T17:00:00+00:00"}),
            ("DELETE", f"/api/deployments/{WEB_ID}"): (202, {"id": WEB_ID, "name": "web", "namespace": "dev",
                                                             "status": "DELETING", "propagation_policy": "Foreground"}),
        }

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        status, body = self.routes.get(
            (request.method, request.url.path),
            (404, {"error": {"code": "NOT_FOUND", "message": "Deployment not found", "details": []}}),
        )
        if isinstance(body, str):
            return httpx.Response(status, text=body)
        return httpx.Response(status, json=body)

    def last(self, method: str) -> httpx.Request:
        return [r for r in self.requests if r.method == method][-1]


@pytest.fixture
def fake(monkeypatch):
    fake = FakeApi()
    monkeypatch.setattr(deploy, "_transport", httpx.MockTransport(fake.handler))
    return fake


def run(*args, input=None):
    return CliRunner().invoke(deploy.app, list(args), input=input, env={"COLUMNS": "220"})


# ---------- list / status ----------

def test_list_shows_health_per_deployment(fake):
    result = run("list")
    assert result.exit_code == 0, result.output
    assert "web" in result.output and "HEALTHY" in result.output
    assert "broken" in result.output and "FAILING" in result.output
    assert "0/2" in result.output and "3/3" in result.output


def test_list_namespace_filter_is_sent_as_query(fake):
    run("list", "--namespace", "dev")
    assert fake.last("GET").url.params["namespace"] == "dev"


def test_list_wide_shows_reason(fake):
    assert "ImagePullBackOff in 2 pod(s)" in run("list", "--wide").output


def test_status_shows_pods_and_conditions(fake):
    result = run("status", "broken")  # by name
    assert result.exit_code == 0, result.output
    for text in ("FAILING", "broken-a", "Pending", "k3d-learn-agent-0", "ImagePullBackOff", "MinimumReplicasUnavailable"):
        assert text in result.output


@pytest.mark.parametrize("ref", [BROKEN_ID, "99999999", "dev/broken", "broken"])
def test_status_accepts_id_prefix_name_or_namespace_name(fake, ref):
    assert run("status", ref).exit_code == 0


def test_unknown_reference_exits_1(fake):
    result = run("status", "nothing-like-this")
    assert result.exit_code == 1 and "No deployment matches" in result.output


# ---------- create ----------

def test_create_sends_full_body(fake):
    result = run("create", "--name", "web", "--image", "nginx:latest", "-n", "dev", "-r", "3", "--port", "80",
                 "--cpu-request", "100m", "--memory-limit", "128Mi", "-e", "MODE=demo", "-l", "team=platform",
                 "--command", "sh -c", "--arg", "echo hi")
    assert result.exit_code == 0, result.output
    body = json.loads(fake.last("POST").content)
    assert body == {
        "name": "web", "image": "nginx:latest", "namespace": "dev", "replicas": 3, "port": 80,
        "resources": {"requests": {"cpu": "100m"}, "limits": {"memory": "128Mi"}},
        "env": [{"name": "MODE", "value": "demo"}], "labels": {"team": "platform"},
        "command": ["sh", "-c"], "args": ["echo hi"],
    }
    assert WEB_ID in result.output


def test_create_rejects_malformed_env(fake):
    result = run("create", "--name", "web", "--image", "nginx", "--env", "NOEQUALS")
    assert result.exit_code == 2
    assert not fake.requests


def test_api_validation_error_is_printed_with_details(fake):
    fake.routes[("POST", "/api/deployments")] = (400, {"error": {
        "code": "INVALID_INPUT", "message": "Request validation failed",
        "details": [{"field": "replicas", "message": "Input should be greater than or equal to 0"}]}})
    result = run("create", "--name", "web", "--image", "nginx", "-r", "-1")
    assert result.exit_code == 1
    assert "INVALID_INPUT" in result.output and "replicas: Input should be greater than or equal to 0" in result.output


def test_api_unreachable_exits_2(monkeypatch):
    def refuse(request):
        raise httpx.ConnectError("Connection refused")
    monkeypatch.setattr(deploy, "_transport", httpx.MockTransport(refuse))
    result = run("list")
    assert result.exit_code == 2 and "Cannot reach deploy-api" in result.output


# ---------- scale / set-image / restart / delete ----------

def test_scale_sends_patch(fake):
    result = run("scale", "web", "--replicas", "5")
    assert result.exit_code == 0, result.output
    assert json.loads(fake.last("PATCH").content) == {"replicas": 5}


def test_set_image_sends_patch(fake):
    run("set-image", "web", "--image", "nginx:1.27")
    assert json.loads(fake.last("PATCH").content) == {"image": "nginx:1.27"}


def test_restart_calls_restart_endpoint(fake):
    result = run("restart", "web")
    assert result.exit_code == 0
    assert fake.last("POST").url.path == f"/api/deployments/{WEB_ID}/restart"


def test_logs_prints_lines_and_sends_options(fake):
    fake.routes[("GET", f"/api/deployments/{WEB_ID}/logs")] = (200, "web-a | started\nweb-a | GET / 200\n")
    result = run("logs", "web", "--pod", "web-a", "--follow", "--tail", "20")
    assert result.exit_code == 0, result.output
    assert "web-a | started" in result.output and "web-a | GET / 200" in result.output
    params = fake.last("GET").url.params
    assert (params["pod"], params["follow"], params["tail"]) == ("web-a", "true", "20")


def test_logs_api_error_is_printed(fake):
    fake.routes[("GET", f"/api/deployments/{WEB_ID}/logs")] = (400, {"error": {
        "code": "LOGS_UNAVAILABLE", "message": "container is waiting to start", "details": []}})
    result = run("logs", "web")
    assert result.exit_code == 1 and "LOGS_UNAVAILABLE" in result.output


def test_delete_asks_for_confirmation_and_aborts_on_no(fake):
    result = run("delete", "web", input="n\n")
    assert result.exit_code == 1  # aborted
    assert not [r for r in fake.requests if r.method == "DELETE"]


def test_delete_after_confirmation(fake):
    result = run("delete", "web", input="y\n")
    assert result.exit_code == 0, result.output
    assert fake.last("DELETE").url.path == f"/api/deployments/{WEB_ID}"


def test_delete_yes_skips_prompt(fake):
    assert run("delete", "web", "--yes").exit_code == 0
