"""JSON authentication endpoints for the private admin application."""

import json
from typing import cast

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel

from primary_signal.web.admin.auth import (
    COOKIE_NAME,
    AdminAuthService,
    LoginRateLimited,
    StoredSession,
)

router = APIRouter(prefix="/admin/auth", tags=["admin-auth"])


class LoginResponse(BaseModel):
    csrf_token: str


def service_for(request: Request) -> AdminAuthService:
    return cast(AdminAuthService, request.app.state.admin_auth)


def validate_origin(request: Request, service: AdminAuthService) -> None:
    if request.headers.get("origin") != service.config.origin:
        raise HTTPException(status_code=403, detail="Request origin denied")


def require_session(request: Request) -> StoredSession:
    service = service_for(request)
    session = service.authenticate(request.cookies.get(COOKIE_NAME))
    if session is None:
        raise HTTPException(status_code=401, detail="Authentication required")
    return session


def require_mutation(request: Request) -> StoredSession:
    service = service_for(request)
    validate_origin(request, service)
    session = require_session(request)
    if not service.valid_csrf(session, request.headers.get("x-csrf-token")):
        raise HTTPException(status_code=403, detail="Request verification failed")
    return session


async def read_login_password(request: Request) -> str:
    """Bound and parse login data without reflecting the password in errors."""

    if request.headers.get("content-type", "").split(";", 1)[0].strip() != "application/json":
        raise HTTPException(status_code=400, detail="Invalid login request")
    body = bytearray()
    async for chunk in request.stream():
        if len(body) + len(chunk) > 8192:
            raise HTTPException(status_code=400, detail="Invalid login request")
        body.extend(chunk)
    try:
        payload: object = json.loads(body)
    except (ValueError, UnicodeDecodeError) as error:
        raise HTTPException(status_code=400, detail="Invalid login request") from error
    if not isinstance(payload, dict) or set(cast(dict[object, object], payload)) != {"password"}:
        raise HTTPException(status_code=400, detail="Invalid login request")
    password = cast(dict[str, object], payload)["password"]
    if not isinstance(password, str) or not 1 <= len(password) <= 4096:
        raise HTTPException(status_code=400, detail="Invalid login request")
    return password


@router.post("/login", response_model=LoginResponse)
async def login(request: Request, response: Response) -> LoginResponse:
    service = service_for(request)
    validate_origin(request, service)
    if request.client is None:
        raise HTTPException(status_code=403, detail="Request source denied")
    password = await read_login_password(request)
    try:
        result = service.login(password, request.client.host)
    except LoginRateLimited as error:
        raise HTTPException(status_code=429, detail="Login temporarily limited") from error
    if result is None:
        raise HTTPException(status_code=401, detail="Invalid credentials")
    previous = request.cookies.get(COOKIE_NAME)
    if previous:
        service.logout(previous)
    response.set_cookie(
        COOKIE_NAME,
        result.identifier,
        max_age=service.config.absolute_seconds,
        secure=True,
        httponly=True,
        samesite="strict",
        path="/",
    )
    response.headers["Cache-Control"] = "no-store"
    return LoginResponse(csrf_token=result.csrf_token)


@router.post("/logout", status_code=204)
async def logout(request: Request, response: Response) -> None:
    require_mutation(request)
    service_for(request).logout(request.cookies[COOKIE_NAME])
    response.delete_cookie(COOKIE_NAME, secure=True, httponly=True, samesite="strict", path="/")
    response.headers["Cache-Control"] = "no-store"


@router.get("/session")
async def session_probe(request: Request) -> dict[str, bool]:
    require_session(request)
    return {"authenticated": True}
