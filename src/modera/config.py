import re
from pathlib import Path
from typing import Annotated, Literal

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from modera.model_config import ImageModelSettings, TextModelSettings
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
    docs_enabled: bool = False
    text_model: TextModelSettings | None = None
    image_model: ImageModelSettings | None = None
    policy_file: Path | None = None
    text_rules_file: Path | None = None
    text_fallback: Literal["error", "rules"] = "error"
    text_fallback_policy: PolicyName | None = None
    max_in_flight_requests: Annotated[int, Field(ge=1, le=256)] = 8
    inference_threads: Annotated[int, Field(ge=1, le=32)] = 2
    inference_timeout_seconds: Annotated[float, Field(gt=0, le=60)] = 5.0
    image_allowed_hosts: tuple[str, ...] = ()

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

    @field_validator("api_key")
    @classmethod
    def validate_api_key(cls, value: SecretStr) -> SecretStr:
        key = value.get_secret_value()
        if not 32 <= len(key) <= 512 or any(not 33 <= ord(char) <= 126 for char in key):
            raise ValueError(
                "API key must contain 32-512 printable ASCII characters without spaces"
            )
        return value
