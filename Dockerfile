# One build provides the same package and assets to each eventual entrypoint.
FROM node:24.21.0-bookworm-slim AS assets
WORKDIR /build
COPY package.json package-lock.json ./
RUN npm ci --ignore-scripts
COPY frontend ./frontend
COPY src ./src
RUN npm run build

FROM ghcr.io/astral-sh/uv:0.12.21 AS uv-bin

FROM python:3.14-slim-bookworm AS package
WORKDIR /app
COPY --from=uv-bin /uv /usr/local/bin/uv
COPY pyproject.toml uv.lock README.md ./
COPY src ./src
COPY migrations ./migrations
COPY alembic.ini ./
COPY --from=assets /build/src/primary_signal/web/common/static/htmx.min.js ./src/primary_signal/web/common/static/htmx.min.js
COPY --from=assets /build/src/primary_signal/web/public/static/public.css ./src/primary_signal/web/public/static/public.css
COPY --from=assets /build/src/primary_signal/web/admin/static/admin.css ./src/primary_signal/web/admin/static/admin.css
RUN uv sync --locked --no-dev --no-editable

FROM python:3.14-slim-bookworm AS runtime
ENV PATH="/app/.venv/bin:${PATH}" \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1
WORKDIR /app
COPY --from=package /app/.venv /app/.venv
USER 10001:10001
EXPOSE 8000
CMD ["primary-signal-web", "--surface", "public", "--host", "0.0.0.0", "--port", "8000"]
