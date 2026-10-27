from pathlib import Path
from typing import Annotated, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from modera.results import Category

ModelRevision = Annotated[str, Field(pattern=r"^[0-9a-f]{40}$")]
ModelId = Annotated[
    str,
    Field(
        max_length=200,
        pattern=r"^[a-zA-Z0-9][a-zA-Z0-9._-]*/[a-zA-Z0-9][a-zA-Z0-9._-]*$",
    ),
]


class PinnedModelSettings(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    id: ModelId
    revision: ModelRevision
    label_map: dict[str, Category] = Field(default_factory=dict)
    cache_dir: Annotated[Path | None, Field(strict=False)] = None

    @model_validator(mode="after")
    def unique_categories(self) -> Self:
        if len(self.label_map) != len(set(self.label_map.values())):
            raise ValueError("model categories must be unique")
        return self


class TextModelSettings(PinnedModelSettings):
    activation: Literal["sigmoid", "softmax"] = "sigmoid"
    max_tokens: Annotated[int, Field(ge=1, le=8192)] = 256
    backend: Literal["transformers_sequence_classification"] = (
        "transformers_sequence_classification"
    )


class ImageModelSettings(PinnedModelSettings):
    activation: Literal["sigmoid", "softmax"] = "softmax"
    backend: Literal["transformers_image_classification"] = (
        "transformers_image_classification"
    )


class ModelConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    text: TextModelSettings | None = None
    image: ImageModelSettings | None = None

    @model_validator(mode="after")
    def validate_selections(self) -> Self:
        selections = [model for model in (self.text, self.image) if model is not None]
        if not selections:
            raise ValueError("configure at least one model")
        if any(
            not model.label_map
            or any(c.startswith("rules.") for c in model.label_map.values())
            for model in selections
        ):
            raise ValueError("models require explicit non-rules label mappings")
        return self


def load_models(path: Path) -> ModelConfig:
    try:
        with path.open("rb") as source:
            content = source.read(1024 * 1024 + 1)
        if len(content) > 1024 * 1024:
            raise ValueError("model configuration exceeds 1 MiB")
        models = ModelConfig.model_validate_json(content)
    except (OSError, ValidationError):
        raise ValueError(
            "model configuration could not be loaded or is invalid"
        ) from None
    updates = {}
    for kind, model in (("text", models.text), ("image", models.image)):
        if model is not None and model.cache_dir is not None:
            updates[kind] = model.model_copy(
                update={
                    "cache_dir": (path.resolve().parent / model.cache_dir).resolve()
                }
            )
    return models.model_copy(update=updates)
