"""Checks on the real gateway's calls, with the Kubernetes API classes mocked out."""

from unittest.mock import MagicMock

import pytest
from kubernetes import client

from app.k8s_gateway import REQUEST_TIMEOUT_SECONDS, STRATEGIC_MERGE_PATCH, KubernetesGateway


def gateway_with_mocks():
    gw = KubernetesGateway()
    gw._apps, gw._core = MagicMock(), MagicMock()  # skip loading real credentials
    return gw


def test_patch_uses_strategic_merge_content_type():
    # The client defaults PATCH to JSON Patch, which would reject our dict bodies.
    gw = gateway_with_mocks()
    gw.patch_deployment("dev", "web", {"spec": {"replicas": 2}})
    _, kwargs = gw._apps.patch_namespaced_deployment.call_args
    assert kwargs["_content_type"] == STRATEGIC_MERGE_PATCH == "application/strategic-merge-patch+json"


def test_delete_cascades_in_foreground():
    gw = gateway_with_mocks()
    gw.delete_deployment("dev", "web")
    _, kwargs = gw._apps.delete_namespaced_deployment.call_args
    assert kwargs["propagation_policy"] == "Foreground"


def test_open_pod_log_streams_raw_response():
    gw = gateway_with_mocks()
    gw.open_pod_log("dev", "web-a", follow=True, tail_lines=50)
    args, kwargs = gw._core.read_namespaced_pod_log.call_args
    assert args == ("web-a", "dev")
    assert kwargs["follow"] is True and kwargs["tail_lines"] == 50
    assert kwargs["_preload_content"] is False      # stream, don't buffer the whole log
    assert "_request_timeout" not in kwargs         # follow: no timeout on a quiet stream

    gw.open_pod_log("dev", "web-a", follow=False, tail_lines=50)
    _, kwargs = gw._core.read_namespaced_pod_log.call_args
    assert kwargs["_request_timeout"] == REQUEST_TIMEOUT_SECONDS


@pytest.mark.parametrize("follow", [True, False])
def test_open_pod_log_arguments_pass_real_client_validation(follow):
    """Mocks don't validate arguments. This runs the REAL client method (which checks its
    arguments with pydantic) and only stubs the network call."""
    gw = KubernetesGateway()
    gw._core = client.CoreV1Api(client.ApiClient(client.Configuration(host="http://unused")))
    gw._apps = MagicMock()
    gw._core.api_client.call_api = MagicMock(return_value=MagicMock(status=200))
    gw.open_pod_log("dev", "web-a", follow=follow, tail_lines=10)
    gw._core.api_client.call_api.assert_called_once()


def test_patch_arguments_pass_real_client_validation():
    gw = KubernetesGateway()
    gw._apps = client.AppsV1Api(client.ApiClient(client.Configuration(host="http://unused")))
    gw._core = MagicMock()
    gw._apps.api_client.call_api = MagicMock()
    gw._apps.api_client.response_deserialize = MagicMock()
    gw.patch_deployment("dev", "web", {"spec": {"replicas": 2}})
    method, url, headers, *_ = gw._apps.api_client.call_api.call_args.args
    assert method == "PATCH" and headers["Content-Type"] == STRATEGIC_MERGE_PATCH


def test_create_service_body_passes_real_client_validation():
    from app.models import CreateDeploymentRequest
    from app.service import build_deployment, build_service

    req = CreateDeploymentRequest(name="web", image="nginx", namespace="dev", port=80,
                                  service={"type": "NodePort", "node_port": 31080})
    owner = build_deployment(req)
    owner.metadata.uid = "11111111-2222-3333-4444-555555555555"
    gw = KubernetesGateway()
    gw._core = client.CoreV1Api(client.ApiClient(client.Configuration(host="http://unused")))
    gw._apps = MagicMock()
    gw._core.api_client.call_api = MagicMock()
    gw._core.api_client.response_deserialize = MagicMock()
    gw.create_service("dev", build_service(req, owner))
    method, url, *_ = gw._core.api_client.call_api.call_args.args
    assert method == "POST" and url.endswith("/api/v1/namespaces/dev/services")


def test_list_all_namespaces_when_no_filter():
    gw = gateway_with_mocks()
    gw.list_deployments(None, "a=b")
    gw._apps.list_deployment_for_all_namespaces.assert_called_once()
    gw.list_deployments("dev", "a=b")
    gw._apps.list_namespaced_deployment.assert_called_once()
