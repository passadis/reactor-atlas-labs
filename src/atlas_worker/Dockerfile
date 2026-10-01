# ATLAS remediation worker. Build context is the REPOSITORY ROOT (azure.yaml docker.context): the
# worker is a uv workspace member that depends on atlas-common.
#
# Same two-stage shape as the web app and orchestrator. No ingress and no HTTP port: the worker
# only pulls from Service Bus, and Container Apps scales it on queue depth (compute-aca.bicep).

FROM python:3.13-slim AS build
COPY --from=ghcr.io/astral-sh/uv:0.11.7 /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never     UV_PROJECT_ENVIRONMENT=/app/.venv
WORKDIR /src
COPY pyproject.toml uv.lock ./
COPY src/ src/
RUN uv sync --frozen --no-dev --no-editable --package atlas-worker     --python /usr/local/bin/python3.13

FROM python:3.13-slim
RUN groupadd --system atlas && useradd --system --gid atlas --home /app atlas
WORKDIR /app
COPY --from=build --chown=atlas:atlas /app/.venv /app/.venv
ENV PATH="/app/.venv/bin:${PATH}" PYTHONUNBUFFERED=1
USER atlas
CMD ["python", "-m", "atlas_worker"]
