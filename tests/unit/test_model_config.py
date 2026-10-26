import pytest
from pydantic import ValidationError

from modera.config import Settings
from modera.main import create_app
from modera.model_config import ImageModelSettings, TextModelSettings

REVISION = "01adcc628a33848569816d4851a38544c321b92c"


def test_models_are_unset_by_default() -> None:
    settings = Settings()
    assert settings.text_model is None
    assert settings.image_model is None


@pytest.mark.parametrize(
    "model_id",
    ["Horizon-Labs/multilingual-toxicity-small", "unitary/toxic-bert"],
)
def test_text_model_can_be_selected_from_environment(
    monkeypatch: pytest.MonkeyPatch, model_id: str
) -> None:
    monkeypatch.setenv("MODERA_TEXT_MODEL__ID", model_id)
    monkeypatch.setenv("MODERA_TEXT_MODEL__REVISION", REVISION)
    settings = Settings()
    assert settings.text_model is not None
    assert settings.text_model.id == model_id
    assert settings.text_model.revision == REVISION
    assert settings.image_model is None


def test_photo_model_can_be_configured_independently(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MODERA_IMAGE_MODEL__ID", "AdamCodd/vit-base-nsfw-detector")
    monkeypatch.setenv("MODERA_IMAGE_MODEL__REVISION", REVISION)
    settings = Settings()
    assert settings.image_model is not None
    assert settings.text_model is None


@pytest.mark.parametrize("modality", ["TEXT", "IMAGE"])
@pytest.mark.parametrize("field", ["ID", "REVISION"])
def test_incomplete_selections_fail(
    monkeypatch: pytest.MonkeyPatch, modality: str, field: str
) -> None:
    monkeypatch.setenv(
        f"MODERA_{modality}_MODEL__{field}",
        REVISION if field == "REVISION" else "unitary/toxic-bert",
    )
    with pytest.raises(ValidationError):
        Settings()


@pytest.mark.parametrize("revision", ["main", "v1", "", "a" * 39, "a" * 41, "G" * 40])
def test_revision_must_be_a_full_commit_sha(revision: str) -> None:
    with pytest.raises(ValidationError):
        TextModelSettings.model_validate(
            {"id": "unitary/toxic-bert", "revision": revision}
        )


@pytest.mark.parametrize(
    "model_id", ["example/new-text-model", "other-vendor/classifier-v2"]
)
def test_replacement_model_repositories_are_not_allowlisted(model_id: str) -> None:
    selection = TextModelSettings(id=model_id, revision=REVISION)
    assert selection.id == model_id


def test_replacement_photo_model_can_be_selected() -> None:
    selection = ImageModelSettings(id="example/new-image-model", revision=REVISION)
    assert selection.id == "example/new-image-model"


@pytest.mark.parametrize(
    "model_id",
    [
        "",
        "../model",
        "/tmp/model",
        "https://host/model",
        "owner/model/extra",
        "owner/model?token=secret",
    ],
)
def test_invalid_repository_identifiers_are_rejected(model_id: str) -> None:
    with pytest.raises(ValidationError):
        TextModelSettings.model_validate({"id": model_id, "revision": REVISION})


@pytest.mark.parametrize(
    "backend",
    ["arbitrary.module:function", "unknown", "transformers_image_classification"],
)
def test_unknown_or_wrong_modality_backends_are_rejected(backend: str) -> None:
    with pytest.raises(ValidationError):
        TextModelSettings.model_validate(
            {"id": "example/model", "revision": REVISION, "backend": backend}
        )


def test_unknown_model_options_are_rejected() -> None:
    with pytest.raises(ValidationError):
        TextModelSettings.model_validate(
            {
                "id": "unitary/toxic-bert",
                "revision": REVISION,
                "trust_remote_code": True,
            }
        )


def test_model_settings_are_frozen() -> None:
    selection = TextModelSettings(id="unitary/toxic-bert", revision=REVISION)
    with pytest.raises(ValidationError):
        selection.revision = "b" * 40  # type: ignore[misc]


def test_explicit_null_leaves_a_modality_unselected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MODERA_TEXT_MODEL", "null")
    assert Settings().text_model is None


def test_application_keeps_its_model_selection_snapshot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MODERA_TEXT_MODEL__ID", "unitary/toxic-bert")
    monkeypatch.setenv("MODERA_TEXT_MODEL__REVISION", REVISION)
    first = create_app()
    monkeypatch.setenv(
        "MODERA_TEXT_MODEL__ID", "Horizon-Labs/multilingual-toxicity-small"
    )
    second = create_app()
    assert first.state.text_model.id == "unitary/toxic-bert"
    assert second.state.text_model.id == "Horizon-Labs/multilingual-toxicity-small"
