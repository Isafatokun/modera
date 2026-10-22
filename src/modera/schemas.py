from typing import Annotated, Literal

from fastapi import UploadFile
from pydantic import BaseModel, ConfigDict, Field, HttpUrl, UrlConstraints, field_validator

MAX_TEXT_CHARACTERS = 4096
MAX_IMAGE_BYTES = 8 * 1024 * 1024
PolicyName = Annotated[str, Field(min_length=1, max_length=64, pattern=r"^[a-zA-Z0-9_-]+$")]
ImageUrl = Annotated[HttpUrl, UrlConstraints(allowed_schemes=["https"], max_length=2048)]


class RequestModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class TextInput(RequestModel):
    type: Literal["text"]
    text: str = Field(min_length=1, max_length=MAX_TEXT_CHARACTERS)

    @field_validator("text")
    @classmethod
    def reject_blank_text(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("text must not be blank")
        return value


class ImageUrlSource(RequestModel):
    type: Literal["url"]
    url: ImageUrl

    @field_validator("url")
    @classmethod
    def reject_credentials_and_fragments(cls, value: HttpUrl) -> HttpUrl:
        if value.username is not None or value.password is not None:
            raise ValueError("image URLs must not contain credentials")
        if value.fragment is not None:
            raise ValueError("image URLs must not contain fragments")
        return value


class ImageInput(RequestModel):
    type: Literal["image"]
    source: ImageUrlSource


class ModerationRequest(RequestModel):
    input: Annotated[TextInput | ImageInput, Field(discriminator="type")]
    policy: PolicyName = "default"


class ImageUploadRequest(RequestModel):
    file: UploadFile = Field(
        description="Non-empty image file, at most 8 MiB.",
        json_schema_extra={"format": "binary"},
    )
    policy: PolicyName = "default"
