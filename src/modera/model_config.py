from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

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


class TextModelSettings(PinnedModelSettings):
    backend: Literal["transformers_sequence_classification"] = (
        "transformers_sequence_classification"
    )


class ImageModelSettings(PinnedModelSettings):
    backend: Literal["transformers_image_classification"] = (
        "transformers_image_classification"
    )
