"""Checks on the real gateway's calls, with the Kubernetes API classes mocked out."""

from unittest.mock import MagicMock

from app.k8s_gateway import STRATEGIC_MERGE_PATCH, KubernetesGateway


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


def test_list_all_namespaces_when_no_filter():
    gw = gateway_with_mocks()
    gw.list_deployments(None, "a=b")
    gw._apps.list_deployment_for_all_namespaces.assert_called_once()
    gw.list_deployments("dev", "a=b")
    gw._apps.list_namespaced_deployment.assert_called_once()
