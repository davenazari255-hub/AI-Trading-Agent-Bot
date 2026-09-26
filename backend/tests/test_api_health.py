from fastapi.testclient import TestClient

from mooo_api.main import create_app
from mooo_core.config import Settings
from tests.conftest import DB_PASSWORD, VALID_MASTER_KEY


def test_health_reports_ok(settings: Settings) -> None:
    client = TestClient(create_app(settings))
    response = client.get("/health")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["service"] == "api"
    assert body["bybit_env"] == "demo"
    assert VALID_MASTER_KEY not in response.text
    assert DB_PASSWORD not in response.text


def test_errors_use_standard_body(settings: Settings) -> None:
    client = TestClient(create_app(settings))
    response = client.get("/api/v1/does-not-exist")
    assert response.status_code == 404
    assert response.json() == {"error_code": "not_found", "message": "Not Found"}

    response = client.post("/health")
    assert response.status_code == 405
    assert response.json()["error_code"] == "method_not_allowed"


def test_create_app_loads_settings_from_environment() -> None:
    client = TestClient(create_app())
    assert client.get("/health").status_code == 200


def test_docs_are_disabled_in_production(settings: Settings) -> None:
    production = settings.model_copy(update={"app_env": "production"})
    client = TestClient(create_app(production))
    assert client.get("/docs").status_code == 404
    assert client.get("/openapi.json").status_code == 404
