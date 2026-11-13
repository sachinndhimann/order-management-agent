# One image, five roles (api / orders-mcp / logistics-mcp / mock-partners / migrate): only the command differs.
# WHY one image: identical dependencies everywhere => "works in the API container" == "works in the MCP container".
FROM python:3.12-slim AS base
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
WORKDIR /srv
COPY --from=ghcr.io/astral-sh/uv:0.13.1 /uv /usr/local/bin/uv
# Dependencies first: this layer is cached until pyproject.toml changes (fast rebuilds).
COPY pyproject.toml ./
RUN uv pip install --system -r pyproject.toml
COPY alembic.ini ./
COPY alembic ./alembic
COPY app ./app
COPY mock_services ./mock_services
COPY external_mcp ./external_mcp
# Least privilege: never run as root in a container.
RUN useradd --system --uid 10001 appuser && chown -R appuser /srv
USER appuser
EXPOSE 8000
CMD ["uvicorn", "app.main:get_app", "--factory", "--host", "0.0.0.0", "--port", "8000"]
