import re
from pathlib import Path

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="MODERA_", frozen=True, extra="forbid", hide_input_in_errors=True
    )

    api_key: SecretStr = Field(repr=False)
    docs_enabled: bool = False
    policy_file: Path | None = None
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
