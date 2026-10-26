from pathlib import Path
from typing import Annotated, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

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
