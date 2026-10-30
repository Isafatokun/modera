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


class DetectorModelSettings(PinnedModelSettings):
    """Pinned Transformers object detector; category scores use box maxima."""

    backend: Literal["transformers_object_detection"] = "transformers_object_detection"


class OnnxDetectorSettings(PinnedModelSettings):
    """Explicit batch-one YOLOv8 proposal layout; no arbitrary ONNX auto-detection."""

    backend: Literal["onnx_yolo_v8"] = "onnx_yolo_v8"
    filename: Annotated[
        str, Field(max_length=128, pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]*\.onnx$")
    ]
    sha256: Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]
    labels: Annotated[
        tuple[Annotated[str, Field(min_length=1, max_length=100)], ...],
        Field(min_length=1, max_length=256),
    ]
    input_size: Annotated[int, Field(ge=32, le=1280)]
    channel_order: Literal["rgb", "bgr"]

    @model_validator(mode="after")
    def validate_labels(self) -> Self:
        if len(set(self.labels)) != len(self.labels) or not set(self.label_map) <= set(
            self.labels
        ):
            raise ValueError("ONNX mappings require unique ordered native labels")
        return self


ImageSettings = ImageModelSettings | DetectorModelSettings | OnnxDetectorSettings


def validate_image_models(
    image: ImageSettings | None, images: tuple[ImageSettings, ...]
) -> None:
    if image is not None and images:
        raise ValueError("use image or image_models, not both")
    ids: set[str] = set()
    categories: set[str] = set()
    for model in images:
        if model.id in ids or categories.intersection(model.label_map.values()):
            raise ValueError("image models require unique IDs and disjoint categories")
        ids.add(model.id)
        categories.update(model.label_map.values())


class ModelConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    text: TextModelSettings | None = None
    image: ImageSettings | None = None
    image_models: Annotated[tuple[ImageSettings, ...], Field(max_length=4)] = ()

    @property
    def configured_images(self) -> tuple[ImageSettings, ...]:
        return (self.image,) if self.image is not None else self.image_models

    @model_validator(mode="after")
    def validate_selections(self) -> Self:
        validate_image_models(self.image, self.image_models)
        selections = ([self.text] if self.text is not None else []) + list(
            self.configured_images
        )
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

    def resolve(
        model: TextModelSettings | ImageSettings,
    ) -> TextModelSettings | ImageSettings:
        if model.cache_dir is None:
            return model
        return model.model_copy(
            update={"cache_dir": (path.resolve().parent / model.cache_dir).resolve()}
        )

    return models.model_copy(
        update={
            "text": resolve(models.text) if models.text is not None else None,
            "image": resolve(models.image) if models.image is not None else None,
            "image_models": tuple(resolve(model) for model in models.image_models),
        }
    )
