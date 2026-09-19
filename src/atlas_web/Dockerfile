# ATLAS web. Build context is the REPOSITORY ROOT (azure.yaml docker.context), because the app is a
# uv workspace member that depends on atlas-common and atlas-agent.
#
# Two stages: uv resolves the locked workspace into a virtualenv in the build stage; the runtime
# stage copies only that virtualenv. Non-root, no secrets, Python 3.13 (CLAUDE.md section 7.2).

FROM python:3.13-slim AS build
COPY --from=ghcr.io/astral-sh/uv:0.11.7 /uv /usr/local/bin/uv
# The venv is built at its FINAL path: console-script shebangs are absolute, so a venv built at
# /src/.venv and copied to /app/.venv fails with 'exec ... no such file or directory'.
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/app/.venv
WORKDIR /src
COPY pyproject.toml uv.lock ./
COPY src/ src/
# --frozen: never re-resolve during a build (exact pins, CLAUDE.md section 1).
# --no-editable: install the workspace packages for real, so /src is not needed at runtime.
RUN uv sync --frozen --no-dev --no-editable --package atlas-web --python /usr/local/bin/python3.13

FROM python:3.13-slim
RUN groupadd --system atlas && useradd --system --gid atlas --home /app atlas
WORKDIR /app
COPY --from=build --chown=atlas:atlas /app/.venv /app/.venv
ENV PATH="/app/.venv/bin:${PATH}" PYTHONUNBUFFERED=1 PORT=8000
USER atlas
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/api/live', timeout=4)"]
# One worker per replica; scale is replicas (min 2). --proxy-headers: behind ACA ingress.
CMD ["python", "-m", "uvicorn", "atlas_web.app:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers", "--forwarded-allow-ips", "*", "--timeout-graceful-shutdown", "20"]
