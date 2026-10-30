"""Prepare and verify pinned classifiers before offline service startup."""

import argparse
import importlib
import json
import os
import sys
from pathlib import Path

from modera.inference import Classifier
from modera.model_config import ModelConfig, load_models

ARTIFACT_PATTERNS = ["*.json", "*.safetensors", "*.txt", "*.model"]


def prepare_models(models: ModelConfig, *, threads: int, offline: bool) -> None:
    os.environ["HF_HUB_DISABLE_TELEMETRY"] = "1"
    hub = importlib.import_module("huggingface_hub")
    for kind, selection in (("text", models.text), ("image", models.image)):
        if selection is None:
            continue
        print(f"Preparing {kind} model", file=sys.stderr, flush=True)
        hub.snapshot_download(
            selection.id,
            revision=selection.revision,
            cache_dir=selection.cache_dir,
            local_files_only=offline,
            allow_patterns=ARTIFACT_PATTERNS,
            max_workers=4,
        )
        Classifier(selection, threads)
        print(
            json.dumps(
                {
                    "type": kind,
                    "id": selection.id,
                    "revision": selection.revision,
                    "ready": True,
                }
            ),
            flush=True,
        )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument(
        "--offline", action="store_true", help="verify cached files only"
    )
    parser.add_argument("--threads", type=int, choices=range(1, 33), default=2)
    args = parser.parse_args()
    try:
        models = load_models(args.config)
    except ValueError as exc:
        parser.error(str(exc))
    try:
        prepare_models(models, threads=args.threads, offline=args.offline)
    except Exception as exc:
        parser.exit(
            1,
            f"Model preparation failed ({type(exc).__name__}). "
            "Check inference dependencies, model access and cached files.\n",
        )


if __name__ == "__main__":
    main()
