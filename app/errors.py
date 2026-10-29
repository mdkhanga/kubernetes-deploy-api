"""Application errors and the single JSON error shape every failure uses:

    {"error": {"code": "...", "message": "...", "details": [{"field": "...", "message": "..."}]}}
"""

import json

from fastapi.responses import JSONResponse
from kubernetes.client.exceptions import ApiException

from app.models import ErrorBody, ErrorDetail, ErrorResponse


class AppError(Exception):
    status_code = 500
    code = "INTERNAL_ERROR"

    def __init__(self, message: str, details: list[ErrorDetail] | None = None):
        super().__init__(message)
        self.message = message
        self.details = details or []


class InvalidInputError(AppError):
    status_code = 400
    code = "INVALID_INPUT"


class NotFoundError(AppError):
    status_code = 404
    code = "NOT_FOUND"


class ConflictError(AppError):
    status_code = 409
    code = "CONFLICT"


class LogsUnavailableError(AppError):
    """Kubernetes cannot serve logs yet, e.g. the container never started (ImagePullBackOff)."""
    status_code = 400
    code = "LOGS_UNAVAILABLE"


def kubernetes_message(exc: ApiException) -> str:
    """The API server puts a human-readable reason in the JSON body's 'message'."""
    try:
        return json.loads(exc.body).get("message") or exc.reason
    except (TypeError, ValueError, AttributeError):
        return exc.reason or str(exc)


def error_response(status_code: int, code: str, message: str, details: list[ErrorDetail] | None = None) -> JSONResponse:
    body = ErrorResponse(error=ErrorBody(code=code, message=message, details=details or []))
    return JSONResponse(status_code=status_code, content=body.model_dump())
