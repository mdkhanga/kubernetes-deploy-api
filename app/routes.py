"""REST endpoints. Thin: parse/validate input, call the service, return models."""

from fastapi import APIRouter, Depends, Query, Request, status

from app import validation
from app.errors import InvalidInputError
from app.models import (
    CreateDeploymentRequest,
    DeleteResponse,
    DeploymentDetail,
    DeploymentList,
    ErrorDetail,
    ErrorResponse,
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


@router.delete("/{deployment_id}", status_code=status.HTTP_202_ACCEPTED, response_model=DeleteResponse, responses=ERRORS)
def delete_deployment(deployment_id: str, service: DeploymentService = Depends(get_service)):
    return service.delete(deployment_id)
