FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

RUN groupadd --system mooo && useradd --system --gid mooo --home-dir /app mooo

COPY pyproject.toml ./
COPY mooo_core ./mooo_core
COPY mooo_api ./mooo_api
COPY mooo_worker ./mooo_worker
COPY alembic.ini ./
COPY migrations ./migrations

RUN pip install .

USER mooo

EXPOSE 8000 8081

CMD ["uvicorn", "mooo_api.app:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000"]
