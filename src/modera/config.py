from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="MODERA_", frozen=True, extra="forbid")

    docs_enabled: bool = False
    policy_file: Path | None = None
