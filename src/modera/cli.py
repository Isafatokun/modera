"""Local setup helpers. Diagnostics never print configuration values or secrets."""

import argparse
import json
import os
import secrets
from collections.abc import Mapping, Sequence
from pathlib import Path

from pydantic import ValidationError

from modera.config import Settings
from modera.engine import ModerationEngine
from modera.policies import Policy, load_policies
from modera.rules import TextRules, load_rules


def initialize(directory: Path) -> None:
    """Create a new rules demo directory; never replace existing files."""
    directory.mkdir(mode=0o700, parents=True, exist_ok=False)
    policy = {
        "policies": [
            {
                "id": "rules-example",
                "version": "1",
                "text": [{"category": "rules.example", "block_threshold": 1.0}],
            }
        ]
    }
    rules = {
        "version": "1",
        "rules": [
            {
                "category": "rules.example",
                "phrases": ["example prohibited phrase"],
            }
        ],
    }
    for name, data in (("policies.json", policy), ("text-rules.json", rules)):
        (directory / name).write_text(json.dumps(data, indent=2) + "\n")
    # Generated values contain no shell metacharacters. Paths are relative to the
    # generated directory so it remains movable and can be used with --env-file.
    descriptor = os.open(
        directory / ".env", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600
    )
    with os.fdopen(descriptor, "w") as target:
        target.write(
            f"MODERA_API_KEY={secrets.token_urlsafe(32)}\n"
            "MODERA_POLICY_FILE=policies.json\n"
            "MODERA_TEXT_RULES_FILE=text-rules.json\n"
        )
    (directory / ".gitignore").write_text(".env\n")
    print("Created a text rules demo. No model is installed; photos are unsupported.")
    print("The key is in .env. Keep it private. Next steps: docs/configuration.md.")


class SetupProblem(Exception):
    def __init__(self, *messages: str) -> None:
        super().__init__(*messages)
        self.messages = messages


def load_settings(env_file: Path | None) -> Settings:
    if env_file is not None and not env_file.is_file():
        raise SetupProblem("--env-file does not name a readable file.")
    try:
        return Settings(_env_file=env_file)
    except ValidationError as exc:
        messages = []
        for error in exc.errors(include_input=False, include_context=False):
            field = str(error["loc"][0]) if error["loc"] else "configuration"
            # Do not echo unknown field names: they may themselves contain secrets.
            known = field.split(".")[0] in Settings.model_fields
            messages.append(
                f"{field if known else 'configuration'} is missing or invalid."
            )
        raise SetupProblem(*messages) from None
    except (OSError, ValueError):
        raise SetupProblem(
            "model_file or environment configuration could not be loaded."
        ) from None


def load_policies_and_rules(
    settings: Settings,
) -> tuple[Mapping[str, Policy], TextRules | None]:
    try:
        policies = load_policies(settings.policy_file)
    except (OSError, ValueError):
        raise SetupProblem(
            "policy_file could not be loaded; check its path and policy fields."
        ) from None
    try:
        rules = load_rules(settings.text_rules_file)
    except (OSError, ValueError):
        raise SetupProblem(
            "text_rules_file could not be loaded; check its path and fields."
        ) from None
    if not policies:
        raise SetupProblem(
            "policy_file must select at least one policy for this setup."
        )
    return policies, rules


def configured_categories(
    settings: Settings, rules: TextRules | None
) -> dict[str, set[str]]:
    capabilities = {
        "text": set(settings.text_model.label_map.values())
        if settings.text_model
        else set(),
        "image": {
            category
            for model in settings.configured_image_models
            for category in model.label_map.values()
        },
    }
    if rules is not None:
        capabilities["text"].update(rule.category for rule in rules.rules)
    return capabilities


def check_policy_categories(
    policies: Mapping[str, Policy], capabilities: dict[str, set[str]]
) -> None:
    for policy in policies.values():
        for kind in ("text", "image"):
            # Policies for unconfigured modalities may be retained for later use.
            if capabilities[kind] and any(
                category not in capabilities[kind]
                for category in policy.categories_for(kind, required_only=True)
            ):
                raise SetupProblem(
                    "policy_file requires a category absent from the checks."
                )
    if not any(capabilities.values()):
        raise SetupProblem("configure a text model, image model or text_rules_file.")


def check_engine(settings: Settings, policies: Mapping[str, Policy]) -> None:
    try:
        engine = ModerationEngine(settings, policies)
    except ValueError:
        raise SetupProblem(
            "check text_fallback, text_fallback_policy and text_rules_file."
        ) from None
    engine.close()


def diagnose(env_file: Path | None) -> int:
    """Validate local configuration without downloading or loading model weights."""
    try:
        settings = load_settings(env_file)
        policies, rules = load_policies_and_rules(settings)
        check_policy_categories(policies, configured_categories(settings, rules))
        check_engine(settings, policies)
    except SetupProblem as problem:
        for message in problem.messages:
            print(f"ERROR: {message}")
        return 1
    print("OK: configuration and policy categories checked.")
    print(
        "Not checked: weights, quality, memory, container permissions or running API."
    )
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Modera local setup helpers")
    commands = parser.add_subparsers(dest="command", required=True)
    init = commands.add_parser(
        "init", help="create a text rules demo in a new directory"
    )
    init.add_argument("directory", type=Path)
    doctor = commands.add_parser(
        "doctor", help="validate local configuration without inference"
    )
    doctor.add_argument("--env-file", type=Path)
    args = parser.parse_args(argv)
    try:
        if args.command == "init":
            initialize(args.directory)
            return 0
        return diagnose(args.env_file)
    except OSError:
        print("ERROR: check file permissions; init requires a new directory.")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
