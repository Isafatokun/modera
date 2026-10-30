"""Local setup helpers. Diagnostics never print configuration values or secrets."""

import argparse
import json
import os
import secrets
from collections.abc import Sequence
from pathlib import Path

from pydantic import ValidationError

from modera.config import Settings
from modera.engine import ModerationEngine
from modera.policies import load_policies
from modera.rules import load_rules


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
    print("The key is in .env. Keep it private. See docs/start-here.md for commands.")


def diagnose(env_file: Path | None) -> int:
    """Validate local configuration without downloading or loading model weights."""
    if env_file is not None and not env_file.is_file():
        print("ERROR: --env-file does not name a readable file.")
        return 1
    try:
        settings = Settings(_env_file=env_file)
    except ValidationError as exc:
        for error in exc.errors(include_input=False, include_context=False):
            field = str(error["loc"][0]) if error["loc"] else "configuration"
            # Do not echo unknown field names: they may themselves contain secrets.
            known = field.split(".")[0] in Settings.model_fields
            print(
                f"ERROR: {field if known else 'configuration'} is missing or invalid."
            )
        return 1
    except (OSError, ValueError):
        print("ERROR: model_file or environment configuration could not be loaded.")
        return 1
    try:
        policies = load_policies(settings.policy_file)
    except (OSError, ValueError):
        print(
            "ERROR: policy_file could not be loaded; check its path and policy fields."
        )
        return 1
    try:
        rules = load_rules(settings.text_rules_file)
    except (OSError, ValueError):
        print("ERROR: text_rules_file could not be loaded; check its path and fields.")
        return 1
    if not policies:
        print("ERROR: policy_file must select at least one policy for this setup.")
        return 1
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
    for policy in policies.values():
        for kind in ("text", "image"):
            # Policies for unconfigured modalities may be retained for later use.
            if capabilities[kind] and any(
                category not in capabilities[kind]
                for category in policy.categories_for(kind, required_only=True)
            ):
                print("ERROR: policy_file requires a category absent from the checks.")
                return 1
    if not any(capabilities.values()):
        print("ERROR: configure a text model, image model or text_rules_file.")
        return 1
    try:
        engine = ModerationEngine(settings, policies)
    except ValueError:
        print("ERROR: check text_fallback, text_fallback_policy and text_rules_file.")
        return 1
    engine.close()
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
