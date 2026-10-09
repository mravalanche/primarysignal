"""Administration web application composition."""

import re
from collections.abc import Awaitable, Callable
from urllib.parse import urlsplit

from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse

from primary_signal.config import RuntimeEnvironment, Settings
from primary_signal.publication.editorial_reader import EditorialReader
from primary_signal.web.admin.auth import COOKIE_NAME, AdminAuthService
from primary_signal.web.admin.decisions import router as decision_router
from primary_signal.web.admin.desk import router as desk_router
from primary_signal.web.admin.health import router as admin_health_router
from primary_signal.web.admin.login_pages import router as login_page_router
from primary_signal.web.admin.routes import router as auth_router
from primary_signal.web.common import Surface, create_base_app
from primary_signal.web.common.health import router as health_router
from primary_signal.web.common.templates import TemplateSurface
from primary_signal.web.common.ui import mount_ui_assets


def create_admin_app(
    settings: Settings | None = None,
    *,
    auth_service: AdminAuthService | None = None,
    editorial_reader: EditorialReader | None = None,
    decision_writer: object | None = None,
    decision_actor: str = "site operator",
    public_origin: str | None = None,
) -> FastAPI:
    """Create the administration application and its explicit route set."""

    resolved_settings = settings or Settings()
    if resolved_settings.environment is RuntimeEnvironment.PRODUCTION and auth_service is None:
        raise RuntimeError("production administration requires configured authentication")
    if resolved_settings.environment is RuntimeEnvironment.PRODUCTION and editorial_reader is None:
        raise RuntimeError("production administration requires restricted editorial reader")
    if decision_writer is not None and (
        not decision_actor.strip() or decision_actor == "system" or len(decision_actor) > 160
    ):
        raise ValueError("a named decision actor is required")
    if public_origin is not None:
        parsed = urlsplit(public_origin)
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.path
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("public origin must be an HTTPS origin without a path")
        if (
            auth_service is not None
            and parsed.hostname == urlsplit(auth_service.config.origin).hostname
        ):
            raise ValueError("public and admin origins must use different hostnames")
    app = create_base_app(settings=resolved_settings, surface=Surface.ADMIN)
    app.state.admin_auth = auth_service
    app.state.editorial_reader = editorial_reader
    app.state.decision_writer = decision_writer
    app.state.decision_actor = decision_actor
    app.state.public_origin = public_origin

    def private_headers(response: Response) -> Response:
        response.headers["Cache-Control"] = "no-store"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Content-Security-Policy"] = (
            "default-src 'none'; script-src 'self'; style-src 'self'; "
            "img-src 'self' data:; font-src 'self'; connect-src 'self'; "
            "form-action 'self'; base-uri 'none'; frame-ancestors 'none'"
        )
        return response

    @app.middleware("http")
    async def admin_boundary(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        if auth_service is None:
            if request.url.path not in {"/health/live", "/admin/health/live"}:
                return private_headers(
                    JSONResponse({"detail": "Admin authentication unavailable"}, status_code=503)
                )
            return private_headers(await call_next(request))
        if request.headers.get("host") != auth_service.config.host:
            return private_headers(JSONResponse({"detail": "Request host denied"}, status_code=400))
        if any(
            header in request.headers
            for header in ("forwarded", "x-forwarded-host", "x-forwarded-for", "x-forwarded-proto")
        ):
            return private_headers(
                JSONResponse({"detail": "Forwarded headers denied"}, status_code=400)
            )
        public_paths = {
            "/health/live",
            "/admin/health/live",
            "/admin/auth/login",
            "/admin/login",
            "/assets/admin/admin.css",
            "/assets/common/htmx.min.js",
        }
        if request.url.path not in public_paths:
            session = auth_service.authenticate(request.cookies.get(COOKIE_NAME))
            if session is None:
                return private_headers(
                    JSONResponse({"detail": "Authentication required"}, status_code=401)
                )
            request.state.admin_session = session
            if request.method not in {"GET", "HEAD", "OPTIONS"}:
                if request.headers.get("origin") != auth_service.config.origin:
                    return private_headers(
                        JSONResponse({"detail": "Request origin denied"}, status_code=403)
                    )
                form_decision = request.method == "POST" and re.fullmatch(
                    r"/admin/desk/stories/[a-z0-9][a-z0-9-]{0,159}/(publish|suppress)",
                    request.url.path,
                )
                if (
                    request.url.path != "/admin/logout"
                    and not form_decision
                    and not auth_service.valid_csrf(session, request.headers.get("x-csrf-token"))
                ):
                    return private_headers(
                        JSONResponse({"detail": "Request verification failed"}, status_code=403)
                    )
        return private_headers(await call_next(request))

    mount_ui_assets(app, TemplateSurface.ADMIN)
    app.include_router(health_router)
    app.include_router(admin_health_router)
    if auth_service is not None:
        app.include_router(auth_router)
        app.include_router(login_page_router)
        if editorial_reader is not None:
            app.include_router(desk_router)
            if decision_writer is not None:
                app.include_router(decision_router)
    return app
