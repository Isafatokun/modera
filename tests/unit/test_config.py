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


@pytest.mark.parametrize(
    "name,value",
    [
        ("MODERA_IMAGE_QUEUE_SIZE", "-1"),
        ("MODERA_IMAGE_QUEUE_SIZE", "33"),
        ("MODERA_IMAGE_QUEUE_WAIT_SECONDS", "0"),
        ("MODERA_IMAGE_QUEUE_WAIT_SECONDS", "11"),
        ("MODERA_IMAGE_QUEUE_WAIT_SECONDS", "nan"),
    ],
)
def test_photo_queue_limits_are_validated(
    monkeypatch: pytest.MonkeyPatch, name: str, value: str
) -> None:
    monkeypatch.setenv(name, value)
    with pytest.raises(ValidationError):
        Settings()


def test_photo_queue_defaults_and_fail_fast_setting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert Settings().image_queue_size == 5
    assert Settings().image_queue_wait_seconds == 2
    monkeypatch.setenv("MODERA_IMAGE_QUEUE_SIZE", "0")
    assert Settings().image_queue_size == 0
