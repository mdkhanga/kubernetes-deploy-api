"""FastAPI app: wiring + translation of every failure into the structured error shape.

Run:  uvicorn app.main:app --port 8090 --reload      (Swagger UI at /docs)
"""

import logging

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from kubernetes.client.exceptions import ApiException
from kubernetes.config import ConfigException
from urllib3.exceptions import HTTPError as Urllib3HTTPError

from app.errors import AppError, error_response, kubernetes_message
from app.k8s_gateway import Gateway, KubernetesGateway
from app.models import ErrorDetail
from app.routes import router
from app.service import DeploymentService

log = logging.getLogger("deploy-api")


def create_app(gateway: Gateway | None = None) -> FastAPI:
    app = FastAPI(
        title="deploy-api",
        description="Create, manage and monitor Kubernetes Deployments.",
        version="1.0.0",
    )
    app.state.service = DeploymentService(gateway or KubernetesGateway())
    app.include_router(router)

    @app.get("/healthz", tags=["meta"])
    def healthz():
        return {"status": "ok"}

    @app.exception_handler(RequestValidationError)
    async def on_validation_error(request: Request, exc: RequestValidationError):
        # FastAPI's default is 422; the spec asks for 400 with field-level details.
        details = []
        for err in exc.errors():
            loc = [str(p) for p in err["loc"] if p not in ("body", "query", "path")]
            details.append(
                ErrorDetail(field=".".join(loc) or None, message=err["msg"].removeprefix("Value error, "))
            )
        return error_response(400, "INVALID_INPUT", "Request validation failed", details)

    @app.exception_handler(AppError)
    async def on_app_error(request: Request, exc: AppError):
        return error_response(exc.status_code, exc.code, exc.message, exc.details)

    @app.exception_handler(ApiException)
    async def on_kubernetes_error(request: Request, exc: ApiException):
        message = kubernetes_message(exc)
        if exc.status == 404:
            return error_response(404, "NOT_FOUND", message)
        if exc.status == 409:
            return error_response(409, "CONFLICT", message)
        if exc.status in (400, 422):
            return error_response(400, "INVALID_INPUT", f"Kubernetes rejected the request: {message}")
        log.error("Kubernetes API error %s: %s", exc.status, message)
        return error_response(500, "KUBERNETES_ERROR", f"Kubernetes API error ({exc.status} {exc.reason}): {message}")

    @app.exception_handler(Urllib3HTTPError)
    async def on_unreachable(request: Request, exc: Urllib3HTTPError):
        log.error("Kubernetes API unreachable: %s", exc)
        return error_response(500, "KUBERNETES_UNREACHABLE", f"Cannot reach the Kubernetes API server: {exc}")

    @app.exception_handler(ConfigException)
    async def on_config_error(request: Request, exc: ConfigException):
        return error_response(500, "KUBERNETES_CONFIG", f"No usable Kubernetes configuration: {exc}")

    @app.exception_handler(Exception)
    async def on_unexpected(request: Request, exc: Exception):
        log.exception("Unexpected error")
        return error_response(500, "INTERNAL_ERROR", f"Unexpected error: {exc}")

    return app


app = create_app()
