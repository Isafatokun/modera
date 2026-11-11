"""Download pinned benchmark checkpoints before starting the offline service."""

import argparse
import importlib
import os
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("cache", type=Path)
    args = parser.parse_args()
    os.environ["HF_HUB_DISABLE_TELEMETRY"] = "1"
    os.environ["ORT_DISABLE_TELEMETRY"] = "1"
    hub = importlib.import_module("huggingface_hub")

    for model, revision in [
        (
            "Horizon-Labs/multilingual-toxicity-small",
            "01adcc628a33848569816d4851a38544c321b92c",
        ),
        ("AdamCodd/vit-base-nsfw-detector", "8587de998f441aac03fdd57a85d2e4cb808c7d64"),
    ]:
        print(
            hub.snapshot_download(
                model,
                revision=revision,
                cache_dir=args.cache,
                allow_patterns=["*.json", "*.safetensors", "*.txt", "*.model"],
            )
        )


if __name__ == "__main__":
    main()
