"""Fixed-policy diagnostic on a public human-labelled sexual-safety challenge."""

import argparse
import hashlib
import json
import secrets
import urllib.request
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient
from pydantic import SecretStr

from evaluation.assess import assessment_metrics
from evaluation.compare_text import digest_file
from evaluation.external_text import write
from modera.config import Settings
from modera.main import create_app
from modera.model_config import ImageModelSettings
from modera.results import ModerationResult

LIMITATIONS = (
    "Small AI-generated, human-labelled sexual-safety challenge, not a natural-photo "
    "nudity benchmark or application calibration set. Unsafe sexual meaning may depend "
    "on rendered text and context, which this nudity classifier does not model. "
    "Labels describe sexual safety, not verified porn/hentai ground truth. "
    "No thresholds are fitted. Training overlap is unknown. The upstream repository "
    "does not provide a top-level dataset licence; images are not redistributed. "
    "UnsafeBench remains gated, and broad independent photo validation is outstanding."
)


def verified_blob(data: bytes, expected: str) -> bool:
    return (
        hashlib.sha1(b"blob " + str(len(data)).encode() + b"\0" + data).hexdigest()
        == expected
    )


def prepare(directory: Path) -> None:
    directory.mkdir(parents=True, exist_ok=False)
    manifest = json.loads(Path(__file__).with_name("photo-challenge.json").read_bytes())
    base = (
        "https://raw.githubusercontent.com/"
        + manifest["repository"]
        + "/"
        + manifest["revision"]
        + "/"
    )

    def download(entry: dict[str, Any]) -> dict[str, Any]:
        data = urllib.request.urlopen(base + entry["path"], timeout=60).read()
        if len(data) != entry["size"] or not verified_blob(data, entry["git_blob_sha"]):
            raise ValueError("source image differs from pinned Git object")
        digest = hashlib.sha256(data).hexdigest()
        name = digest + ".png"
        # Write after download futures complete, so duplicates cannot race.
        return {**entry, "sha256": digest, "local": name, "data": data}

    with ThreadPoolExecutor(max_workers=6) as pool:
        downloaded = list(pool.map(download, manifest["files"]))
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in downloaded:
        row["positive"] = row["path"].split("/")[1] == "unsafe"
        groups[row["sha256"]].append(row)
    cases = []
    exclusions = []
    for digest, members in sorted(groups.items()):
        if len({row["positive"] for row in members}) != 1:
            exclusions.append(
                {
                    "sha256": digest,
                    "paths": [row["path"] for row in members],
                    "reason": "conflicting_labels",
                }
            )
            continue
        row = min(members, key=lambda row: row["path"])
        (directory / row["local"]).write_bytes(row["data"])
        cases.append({key: value for key, value in row.items() if key != "data"})
    write(directory / "cases.json", cases)
    write(
        directory / "protocol.json",
        {
            "source": manifest,
            "source_images": len(downloaded),
            "eligible_unique_images": len(cases),
            "same_label_duplicates_collapsed": sum(
                len(rows) - 1
                for rows in groups.values()
                if len({row["positive"] for row in rows}) == 1
            ),
            "conflicting_groups_excluded": exclusions,
            "cases_sha256": digest_file(directory / "cases.json"),
            "policy": json.loads(
                (
                    Path(__file__).parents[1] / "deploy/reference/policies.json"
                ).read_bytes()
            ),
            "model": json.loads(
                (
                    Path(__file__).parents[1] / "deploy/reference/models.json"
                ).read_bytes()
            )["image"],
            "limitations": LIMITATIONS,
        },
    )


def run(directory: Path, cache: Path, output: Path) -> None:
    protocol = json.loads((directory / "protocol.json").read_bytes())
    if digest_file(directory / "cases.json") != protocol["cases_sha256"]:
        raise ValueError("frozen cases changed")
    cases = json.loads((directory / "cases.json").read_bytes())
    for row in cases:
        if digest_file(directory / row["local"]) != row["sha256"]:
            raise ValueError("frozen image changed")
    output.mkdir(parents=True, exist_ok=False)
    policy = output / "policy.json"
    write(policy, protocol["policy"])
    model = ImageModelSettings.model_validate(protocol["model"]).model_copy(
        update={"cache_dir": cache.resolve() / "hub"}
    )
    key = secrets.token_urlsafe(32)
    settings = Settings(api_key=SecretStr(key), image_model=model, policy_file=policy)
    rows = []
    errors: Counter[str] = Counter()
    with (
        TestClient(
            create_app(settings), headers={"Authorization": "Bearer " + key}
        ) as client,
        (output / "predictions.jsonl").open("x") as stream,
    ):
        for index, row in enumerate(cases, 1):
            response = client.post(
                "/v1/moderations",
                files={
                    "file": (
                        "challenge.png",
                        (directory / row["local"]).read_bytes(),
                        "image/png",
                    )
                },
            )
            predicted = None
            if response.status_code == 200:
                result = ModerationResult.model_validate(response.json())
                predicted = result.decision.action != "allow"
                observation = {
                    "path": row["path"],
                    "sha256": row["sha256"],
                    "result": result.model_dump(mode="json"),
                }
            else:
                code = response.json()["error"]["code"]
                errors[code] += 1
                observation = {
                    "path": row["path"],
                    "sha256": row["sha256"],
                    "error": code,
                }
            rows.append((row["positive"], predicted))
            stream.write(json.dumps(observation) + "\n")
            stream.flush()
            print(f"Photo challenge: {index}/{len(cases)}", flush=True)
    report = {
        "status": "completed",
        "scope": (
            "Reference-policy escalation against broad sexual-safety labels; "
            "no calibration"
        ),
        "metrics": assessment_metrics(rows),
        "errors": dict(errors),
        "protocol": protocol,
        "collector_sha256": digest_file(Path(__file__)),
        "predictions_sha256": digest_file(output / "predictions.jsonl"),
        "limitations": LIMITATIONS,
    }
    write(output / "report.json", report)
    print(json.dumps(report["metrics"], indent=2), flush=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("prepare", "run"))
    parser.add_argument("--directory", required=True, type=Path)
    parser.add_argument("--cache", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if args.command == "prepare":
        prepare(args.directory)
    else:
        if args.cache is None or args.output is None:
            parser.error("run requires --cache and --output")
        run(args.directory, args.cache, args.output)


if __name__ == "__main__":
    main()
