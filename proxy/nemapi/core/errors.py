"""OpenAI-style error envelope: {"error": {"message": ..., "type": ...}}."""
from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException


class APIError(Exception):
    def __init__(self, status_code: int, message: str, type_: str = "invalid_request"):
        super().__init__(message)
        self.status_code = status_code
        self.message = message
        self.type = type_


def error_body(message: str, type_: str) -> dict:
    return {"error": {"message": message, "type": type_}}


def _describe_validation(exc: RequestValidationError) -> str:
    errors = exc.errors()
    if any(e.get("type") == "json_invalid" for e in errors):
        return "Invalid JSON body"
    parts = []
    for e in errors[:3]:
        loc = ".".join(str(x) for x in e.get("loc", ()) if x != "body")
        parts.append(f"{loc or 'body'}: {e.get('msg')}")
    return "Invalid request: " + "; ".join(parts)


def install_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(APIError)
    async def _api_error(_req: Request, exc: APIError):
        return JSONResponse(error_body(exc.message, exc.type), status_code=exc.status_code)

    @app.exception_handler(RequestValidationError)
    async def _validation(_req: Request, exc: RequestValidationError):
        return JSONResponse(error_body(_describe_validation(exc), "invalid_request"), status_code=400)

    @app.exception_handler(StarletteHTTPException)
    async def _http(_req: Request, exc: StarletteHTTPException):
        type_ = "not_found" if exc.status_code == 404 else "http_error"
        message = "not found" if exc.status_code == 404 else str(exc.detail)
        return JSONResponse(error_body(message, type_), status_code=exc.status_code)
