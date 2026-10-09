"""Administration web application composition."""

from collections.abc import Awaitable, Callable

from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse

from primary_signal.config import RuntimeEnvironment, Settings
from primary_signal.web.admin.auth import COOKIE_NAME, AdminAuthService
from primary_signal.web.admin.health import router as admin_health_router
from primary_signal.web.admin.routes import router as auth_router
from primary_signal.web.common import Surface, create_base_app
from primary_signal.web.common.health import router as health_router
from primary_signal.web.common.templates import TemplateSurface
from primary_signal.web.common.ui import mount_ui_assets


def create_admin_app(
    settings: Settings | None = None, *, auth_service: AdminAuthService | None = None
) -> FastAPI:
    """Create the administration application and its explicit route set."""

    resolved_settings = settings or Settings()
    if resolved_settings.environment is RuntimeEnvironment.PRODUCTION and auth_service is None:
        raise RuntimeError("production administration requires configured authentication")
    app = create_base_app(settings=resolved_settings, surface=Surface.ADMIN)
    app.state.admin_auth = auth_service

    @app.middleware("http")
    async def admin_boundary(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        if auth_service is None:
            if request.url.path not in {"/health/live", "/admin/health/live"}:
                return JSONResponse({"detail": "Admin authentication unavailable"}, status_code=503)
            return await call_next(request)
        if request.headers.get("host") != auth_service.config.host:
            return JSONResponse({"detail": "Request host denied"}, status_code=400)
        if any(
            header in request.headers
            for header in ("forwarded", "x-forwarded-host", "x-forwarded-for", "x-forwarded-proto")
        ):
            return JSONResponse({"detail": "Forwarded headers denied"}, status_code=400)
        public_paths = {"/health/live", "/admin/health/live", "/admin/auth/login"}
        if request.url.path not in public_paths:
            session = auth_service.authenticate(request.cookies.get(COOKIE_NAME))
            if session is None:
                return JSONResponse({"detail": "Authentication required"}, status_code=401)
            if request.method not in {"GET", "HEAD", "OPTIONS"}:
                if request.headers.get("origin") != auth_service.config.origin:
                    return JSONResponse({"detail": "Request origin denied"}, status_code=403)
                if not auth_service.valid_csrf(session, request.headers.get("x-csrf-token")):
                    return JSONResponse({"detail": "Request verification failed"}, status_code=403)
        return await call_next(request)

    mount_ui_assets(app, TemplateSurface.ADMIN)
    app.include_router(health_router)
    app.include_router(admin_health_router)
    if auth_service is not None:
        app.include_router(auth_router)
    return app
