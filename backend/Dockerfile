FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

RUN groupadd --system --gid 10001 mooo \
    && useradd --system --uid 10001 --gid mooo --home-dir /app --shell /usr/sbin/nologin mooo

COPY pyproject.toml ./
COPY mooo_core ./mooo_core
COPY mooo_api ./mooo_api
COPY mooo_worker ./mooo_worker
RUN pip install .

COPY alembic.ini ./
COPY migrations ./migrations

USER mooo

EXPOSE 8000 8081

CMD ["uvicorn", "mooo_api.main:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000"]
