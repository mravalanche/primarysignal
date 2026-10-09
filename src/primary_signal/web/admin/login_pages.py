"""Plain HTML sign-in for the private Reading desk."""

from urllib.parse import parse_qs

from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.responses import HTMLResponse, RedirectResponse

from primary_signal.web.admin.auth import COOKIE_NAME, LoginRateLimited
from primary_signal.web.admin.routes import service_for, validate_origin
from primary_signal.web.common.templates import TemplateSurface, create_templates

router = APIRouter(tags=["admin-login-page"])
templates = create_templates(TemplateSurface.ADMIN)


def _login_page(
    request: Request, *, error: str | None = None, status_code: int = 200
) -> HTMLResponse:
    return templates.TemplateResponse(
        request=request,
        name="login.html",
        context={"error": error},
        status_code=status_code,
        headers={"Cache-Control": "no-store"},
    )


@router.get("/admin/login", response_class=HTMLResponse)
async def login_page(request: Request) -> HTMLResponse:
    """Display a single-purpose form; the application boundary checks Host."""

    return _login_page(request)


@router.post("/admin/login")
async def login_form(request: Request) -> Response:
    """Accept one bounded form field and use the same service-wide login limit."""

    service = service_for(request)
    validate_origin(request, service)
    if request.headers.get("content-type", "").split(";", 1)[0].strip() != (
        "application/x-www-form-urlencoded"
    ):
        raise HTTPException(status_code=400, detail="Invalid login request")
    body = bytearray()
    async for chunk in request.stream():
        if len(body) + len(chunk) > 8192:
            raise HTTPException(status_code=400, detail="Invalid login request")
        body.extend(chunk)
    try:
        fields = parse_qs(
            body.decode("utf-8", errors="strict"),
            keep_blank_values=True,
            strict_parsing=True,
            max_num_fields=1,
            errors="strict",
        )
    except (UnicodeDecodeError, ValueError) as error:
        raise HTTPException(status_code=400, detail="Invalid login request") from error
    passwords = fields.get("password")
    if set(fields) != {"password"} or passwords is None or len(passwords) != 1:
        raise HTTPException(status_code=400, detail="Invalid login request")
    password = passwords[0]
    if not 1 <= len(password) <= 4096:
        raise HTTPException(status_code=400, detail="Invalid login request")
    try:
        result = service.login(password)
    except LoginRateLimited:
        return _login_page(
            request, error="Sign-in is temporarily limited. Try again later.", status_code=429
        )
    if result is None:
        return _login_page(request, error="Password not recognised.", status_code=401)
    previous = request.cookies.get(COOKIE_NAME)
    if previous:
        service.logout(previous)
    response = RedirectResponse(url="/admin/desk", status_code=303)
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
    return response


@router.post("/admin/logout")
async def logout_form(request: Request) -> RedirectResponse:
    """Revoke a browser session only with its own form CSRF token."""

    service = service_for(request)
    validate_origin(request, service)
    session = request.state.admin_session
    if request.headers.get("content-type", "").split(";", 1)[0].strip() != (
        "application/x-www-form-urlencoded"
    ):
        raise HTTPException(status_code=400, detail="Invalid logout request")
    body = bytearray()
    async for chunk in request.stream():
        if len(body) + len(chunk) > 256:
            raise HTTPException(status_code=400, detail="Invalid logout request")
        body.extend(chunk)
    try:
        fields = parse_qs(
            body.decode("utf-8", errors="strict"),
            keep_blank_values=True,
            strict_parsing=True,
            max_num_fields=1,
            errors="strict",
        )
    except (UnicodeDecodeError, ValueError) as error:
        raise HTTPException(status_code=400, detail="Invalid logout request") from error
    tokens = fields.get("csrf_token")
    if set(fields) != {"csrf_token"} or tokens is None or len(tokens) != 1:
        raise HTTPException(status_code=400, detail="Invalid logout request")
    if not service.valid_csrf(session, tokens[0]):
        raise HTTPException(status_code=403, detail="Request verification failed")
    service.logout(request.cookies[COOKIE_NAME])
    response = RedirectResponse(url="/admin/login", status_code=303)
    response.delete_cookie(COOKIE_NAME, secure=True, httponly=True, samesite="strict", path="/")
    response.headers["Cache-Control"] = "no-store"
    return response
