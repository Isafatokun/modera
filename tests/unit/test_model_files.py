import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from modera.config import Settings
from modera.model_config import load_models


def config_file(tmp_path: Path) -> Path:
    path = tmp_path / "models.json"
    path.write_text(
        json.dumps(
            {
                "image": {
                    "id": "example/image",
                    "revision": "a" * 40,
                    "label_map": {"nsfw": "sexual"},
                    "cache_dir": "cache",
                }
            }
        )
    )
    return path


def test_file_selection_and_relative_cache_are_independent_of_working_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = config_file(tmp_path)
    monkeypatch.setenv("MODERA_MODEL_FILE", str(path))
    monkeypatch.chdir(tmp_path.parent)
    settings = Settings()
    assert settings.text_model is None
    assert settings.image_model is not None
    assert settings.image_model.cache_dir == tmp_path / "cache"
    assert settings.image_model.revision == "a" * 40
    path.write_text("{}")
    assert Settings.model_validate(settings) is settings
    assert settings.image_model.id == "example/image"


@pytest.mark.parametrize("modality", ["TEXT", "IMAGE"])
def test_file_and_direct_model_environment_cannot_be_combined(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, modality: str
) -> None:
    monkeypatch.setenv("MODERA_MODEL_FILE", str(config_file(tmp_path)))
    monkeypatch.setenv(f"MODERA_{modality}_MODEL__ID", "example/model")
    monkeypatch.setenv(f"MODERA_{modality}_MODEL__REVISION", "b" * 40)
    with pytest.raises(ValidationError, match="not both"):
        Settings()


@pytest.mark.parametrize(
    "content",
    [
        "{}",
        "not json",
        '{"image": {"id": "example/image", "revision": "main"}}',
        '{"image": {"id": "example/image", "revision": "'
        + "a" * 40
        + '", "label_map": {}}}',
        '{"image": {"id": "example/image", "revision": "'
        + "a" * 40
        + '", "label_map": {"x": "rules.bad"}}}',
        '{"secret-field": "private-value"}',
    ],
)
def test_invalid_files_fail_without_echoing_contents(
    tmp_path: Path, content: str
) -> None:
    path = tmp_path / "models.json"
    path.write_text(content)
    with pytest.raises(ValueError) as error:
        load_models(path)
    assert "private-value" not in str(error.value)


def test_missing_and_oversized_model_files_are_rejected(tmp_path: Path) -> None:
    path = tmp_path / "models.json"
    with pytest.raises(ValueError):
        load_models(path)
    path.write_bytes(b" " * (1024 * 1024 + 1))
    with pytest.raises(ValueError, match="1 MiB"):
        load_models(path)
