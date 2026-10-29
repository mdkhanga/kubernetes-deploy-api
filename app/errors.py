"""Application errors and the single JSON error shape every failure uses:

    {"error": {"code": "...", "message": "...", "details": [{"field": "...", "message": "..."}]}}
"""

from fastapi.responses import JSONResponse

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


def error_response(status_code: int, code: str, message: str, details: list[ErrorDetail] | None = None) -> JSONResponse:
    body = ErrorResponse(error=ErrorBody(code=code, message=message, details=details or []))
    return JSONResponse(status_code=status_code, content=body.model_dump())
