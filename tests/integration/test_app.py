import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from modera.config import Settings
from modera.main import create_app


def test_health_tracks_lifecycle() -> None:
    app = create_app(Settings(docs_enabled=False))
    with TestClient(app) as client:
        assert client.get("/health/live").json() == {"status": "ok"}
        response = client.get("/health/ready")
        assert response.status_code == 200
        assert response.json() == {"status": "ok"}

    with TestClient(app) as client:
        assert client.get("/health/ready").status_code == 200

    client = TestClient(app)
    assert client.get("/health/live").status_code == 200
    assert client.get("/health/ready").status_code == 503
    client.close()


@pytest.mark.parametrize("path", ["/docs", "/redoc", "/openapi.json"])
def test_docs_are_disabled_by_default(
    path: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("MODERA_DOCS_ENABLED", raising=False)
    with TestClient(create_app()) as client:
        assert client.get(path).status_code == 404


def test_docs_can_be_enabled() -> None:
    with TestClient(create_app(Settings(docs_enabled=True))) as client:
        assert client.get("/docs").status_code == 200
        schema = client.get("/openapi.json").json()
        assert schema["info"]["version"] == "0.1.0"
        assert "/health/ready" in schema["paths"]


def test_app_settings_do_not_leak_between_instances() -> None:
    with (
        TestClient(create_app(Settings(docs_enabled=True))) as first,
        TestClient(create_app(Settings(docs_enabled=False))) as second,
    ):
        assert first.get("/docs").status_code == 200
        assert second.get("/docs").status_code == 404


def test_bad_settings_prevent_app_creation(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MODERA_DOCS_ENABLED", "invalid")
    with pytest.raises(ValidationError):
        create_app()
