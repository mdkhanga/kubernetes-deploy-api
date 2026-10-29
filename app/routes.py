"""REST endpoints. Thin: parse/validate input, call the service, return models."""

from fastapi import APIRouter, Depends, Query, Request, status
from fastapi.responses import StreamingResponse

from app import validation
from app.errors import InvalidInputError
from app.models import (
    CreateDeploymentRequest,
    DeleteResponse,
    DeploymentDetail,
    DeploymentList,
    ErrorDetail,
    ErrorResponse,
    PatchDeploymentRequest,
)
from app.service import DeploymentService

router = APIRouter(prefix="/api/deployments", tags=["deployments"])

ERRORS = {code: {"model": ErrorResponse} for code in (400, 404, 409, 500)}


def get_service(request: Request) -> DeploymentService:
    return request.app.state.service


@router.post("", status_code=status.HTTP_201_CREATED, response_model=DeploymentDetail, responses=ERRORS)
def create_deployment(body: CreateDeploymentRequest, service: DeploymentService = Depends(get_service)):
    return service.create(body)


@router.get("", response_model=DeploymentList, responses=ERRORS)
def list_deployments(
    namespace: str | None = Query(default=None, description="Only deployments in this namespace"),
    service: DeploymentService = Depends(get_service),
):
    if namespace is not None:
        try:
            validation.validate_dns_label(namespace)
        except ValueError as e:
            raise InvalidInputError("Invalid query parameter", [ErrorDetail(field="namespace", message=str(e))])
    items = service.list(namespace)
    return DeploymentList(items=items, count=len(items))


@router.get("/{deployment_id}", response_model=DeploymentDetail, responses=ERRORS)
def get_deployment(deployment_id: str, service: DeploymentService = Depends(get_service)):
    return service.get(deployment_id)


@router.patch("/{deployment_id}", response_model=DeploymentDetail, responses=ERRORS)
def update_deployment(
    deployment_id: str, body: PatchDeploymentRequest, service: DeploymentService = Depends(get_service)
):
    return service.update(deployment_id, body)


@router.post("/{deployment_id}/restart", status_code=status.HTTP_202_ACCEPTED,
             response_model=DeploymentDetail, responses=ERRORS)
def restart_deployment(deployment_id: str, service: DeploymentService = Depends(get_service)):
    return service.restart(deployment_id)


@router.get("/{deployment_id}/logs", response_class=StreamingResponse, responses={
    200: {"content": {"text/plain": {}}, "description": "Log lines; prefixed with the pod name when all pods are read"},
    **ERRORS,
})
def deployment_logs(
    deployment_id: str,
    pod: str | None = Query(default=None, description="One pod; default: all pods of the deployment"),
    follow: bool = Query(default=False, description="Keep streaming new lines (like kubectl logs -f)"),
    tail: int = Query(default=100, ge=1, le=10000, description="Lines of history per pod"),
    service: DeploymentService = Depends(get_service),
):
    lines = service.logs(deployment_id, pod, follow, tail)
    return StreamingResponse(lines, media_type="text/plain; charset=utf-8")


@router.delete("/{deployment_id}", status_code=status.HTTP_202_ACCEPTED, response_model=DeleteResponse, responses=ERRORS)
def delete_deployment(deployment_id: str, service: DeploymentService = Depends(get_service)):
    return service.delete(deployment_id)
