import re
from pathlib import Path
from typing import Annotated, Any, Literal, Self

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from modera.model_config import (
    ImageSettings,
    TextModelSettings,
    load_models,
    validate_image_models,
)
from modera.schemas import PolicyName


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="MODERA_",
        env_nested_delimiter="__",
        frozen=True,
        extra="forbid",
        hide_input_in_errors=True,
    )

    api_key: SecretStr = Field(repr=False)
    previous_api_key: SecretStr | None = Field(default=None, repr=False)
    docs_enabled: bool = False
    text_model: TextModelSettings | None = None
    image_model: ImageSettings | None = None
    image_models: Annotated[tuple[ImageSettings, ...], Field(max_length=4)] = ()
    model_file: Path | None = None
    policy_file: Path | None = None
    text_rules_file: Path | None = None
    text_fallback: Literal["error", "rules"] = "error"
    text_fallback_policy: PolicyName | None = None
    max_in_flight_requests: Annotated[int, Field(ge=1, le=256)] = 8
    inference_threads: Annotated[int, Field(ge=1, le=32)] = 2
    inference_timeout_seconds: Annotated[float, Field(gt=0, le=60)] = 5.0
    image_queue_size: Annotated[int, Field(ge=0, le=32)] = 5
    image_queue_wait_seconds: Annotated[float, Field(gt=0, le=10)] = 2.0
    image_allowed_hosts: tuple[str, ...] = ()

    @model_validator(mode="before")
    @classmethod
    def load_model_file(cls, data: Any) -> Any:
        if isinstance(data, dict) and isinstance(data.get("model_file"), (str, Path)):
            if (
                data.get("text_model") is not None
                or data.get("image_model") is not None
                or data.get("image_models")
            ):
                raise ValueError("use a model file or direct model settings, not both")
            models = load_models(Path(data["model_file"]))
            return {
                **data,
                "text_model": models.text,
                "image_model": models.image,
                "image_models": models.image_models,
            }
        return data

    @property
    def configured_image_models(self) -> tuple[ImageSettings, ...]:
        return (
            (self.image_model,) if self.image_model is not None else self.image_models
        )

    @model_validator(mode="after")
    def validate_images(self) -> Self:
        validate_image_models(self.image_model, self.image_models)
        if any(
            not model.label_map
            or any(c.startswith("rules.") for c in model.label_map.values())
            for model in self.image_models
        ):
            raise ValueError("image models require explicit non-rules categories")
        return self

    @field_validator("image_allowed_hosts")
    @classmethod
    def validate_image_hosts(cls, hosts: tuple[str, ...]) -> tuple[str, ...]:
        for host in hosts:
            if len(host) > 253 or not re.fullmatch(
                r"(?=.+[a-zA-Z])(?:[a-zA-Z0-9](?:[a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?\.)+"
                r"[a-zA-Z](?:[a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?",
                host,
            ):
                raise ValueError(
                    "image hosts must be DNS names without wildcards, ports, or paths"
                )
        return tuple(dict.fromkeys(host.lower() for host in hosts))

    @field_validator("previous_api_key")
    @classmethod
    def validate_previous_api_key(cls, value: SecretStr | None) -> SecretStr | None:
        return cls.validate_api_key(value) if value is not None else None

    @model_validator(mode="after")
    def validate_key_rotation(self) -> Self:
        if self.previous_api_key is not None and (
            self.previous_api_key.get_secret_value() == self.api_key.get_secret_value()
        ):
            raise ValueError("current and previous API keys must differ")
        return self

    @field_validator("api_key")
    @classmethod
    def validate_api_key(cls, value: SecretStr) -> SecretStr:
        key = value.get_secret_value()
        if not 32 <= len(key) <= 512 or any(not 33 <= ord(char) <= 126 for char in key):
            raise ValueError(
                "API key must contain 32-512 printable ASCII characters without spaces"
            )
        return value
