"""Shared input vocabulary; wire values remain compatible with /v1."""

from typing import Final, Literal

InputType = Literal["text", "image"]
TEXT: Final[Literal["text"]] = "text"
IMAGE: Final[Literal["image"]] = "image"
