import pytest
from pydantic import ValidationError

from modera.config import Settings


def test_environment_configures_docs(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MODERA_DOCS_ENABLED", "true")
    assert Settings().docs_enabled is True


def test_invalid_environment_fails_validation(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MODERA_DOCS_ENABLED", "sometimes")
    with pytest.raises(ValidationError):
        Settings()


def test_unknown_constructor_settings_are_rejected() -> None:
    with pytest.raises(ValidationError):
        Settings.model_validate({"docs_enable": True})
