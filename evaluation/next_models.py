"""Pinned research comparison; no production model or policy changes."""

import argparse
import hashlib
import importlib
import json
import math
import platform
import re
import resource
import secrets
import time
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient
from pydantic import SecretStr

from evaluation.assess import assessment_metrics
from evaluation.compare_text import digest_file
from evaluation.external_text import write
from modera.config import Settings
from modera.main import create_app
from modera.model_config import PinnedModelSettings, TextModelSettings
from modera.results import ModerationResult

CATEGORIES = {
    "Violent",
    "Non-violent Illegal Acts",
    "Sexual Content or Sexual Acts",
    "PII",
    "Suicide & Self-Harm",
    "Unethical Acts",
    "Politically Sensitive Topics",
    "Copyright Violation",
    "Jailbreak",
}


def parse_guard(text: str) -> tuple[str, list[str]]:
    match = re.fullmatch(
        r"Safety: (Safe|Unsafe|Controversial)\nCategories: ([^\n]+)\n?", text
    )
    if match is None:
        raise ValueError("invalid_guard_output")
    label, raw = match.groups()
    categories = [] if raw == "None" else raw.split(", ")
    if (
        len(set(categories)) != len(categories)
        or not set(categories) <= CATEGORIES
        or (label == "Safe") != (not categories)
    ):
        raise ValueError("invalid_guard_categories")
    return label, categories


def matched_sample(cases: list[dict[str, Any]], per_class: int = 50) -> list[str]:
    if per_class < 1 or len({row["id"] for row in cases}) != len(cases):
        raise ValueError("unique cases and positive sample size required")
    groups: dict[tuple[str, bool], list[dict[str, Any]]] = defaultdict(list)
    for row in cases:
        groups[(row["source"], row["positive"])].append(row)
    if len(groups) != 4:
        raise ValueError("both classes in both source datasets required")
    ids: list[str] = []
    for _, rows in sorted(groups.items()):
        if len(rows) < per_class:
            raise ValueError("not enough cases for matched sample")
        selected = sorted(
            rows,
            key=lambda row: hashlib.sha256(
                ("next-models-v1:" + row["id"]).encode()
            ).hexdigest(),
        )[:per_class]
        ids.extend(row["id"] for row in selected)
    return ids


def freeze(directory: Path, output: Path) -> None:
    protocol = json.loads((directory / "protocol.json").read_bytes())
    for name, digest in protocol["hashes"].items():
        if digest_file(directory / name) != digest:
            raise ValueError("original cohort changed")
    cases = json.loads((directory / "sealed-test.json").read_bytes())
    specs = {
        path.stem: json.loads(path.read_bytes())
        for path in Path(__file__).with_name("research").glob("*.json")
    }
    if set(specs) != {"xlmr-large-v2", "qwen3guard-0.6b"}:
        raise ValueError("exact pinned research candidates required")
    write(
        output,
        {
            "version": "next-models-v1",
            "original_protocol_sha256": digest_file(directory / "protocol.json"),
            "cases_sha256": digest_file(directory / "sealed-test.json"),
            "models": specs,
            "matched_ids": matched_sample(cases),
            "xlmr_scope": "all reserved test cases",
            "qwen_scope": "50 cases per source/class on the preselected matched subset",
            "qwen_input_mode": (
                "official single user-prompt chat template, no extra instructions"
            ),
            "qwen_rules": {
                "strict": "Unsafe escalates",
                "broad": "Unsafe or Controversial escalates",
            },
            "max_new_tokens": 128,
            "max_input_tokens": 2048,
            "budget_seconds": 5.0,
            "threads": 2,
            "dtype": "float32",
            "sampling": "greedy",
            "limitations": (
                "Previously inspected data, diagnostic only; no threshold "
                "fitting or acceptance claim. Qwen prompt safety differs "
                "from community-"
                "comment toxicity. Balanced subset is not traffic prevalence. "
                "Generation "
                "max_time is a token-boundary budget, not a hard interrupt; "
                "late results are failures."
            ),
        },
    )


def prepare(cache: Path) -> None:
    hub = importlib.import_module("huggingface_hub")
    for path in sorted(Path(__file__).with_name("research").glob("*.json")):
        spec = json.loads(path.read_bytes())
        hub.snapshot_download(
            spec["id"],
            revision=spec["revision"],
            cache_dir=cache / "hub",
            allow_patterns=["*.safetensors", "*.json", "*.txt", "*.model"],
            max_workers=4,
        )


def percentile(values: list[float], fraction: float) -> float:
    return sorted(values)[max(0, math.ceil(len(values) * fraction) - 1)]


def run(
    kind: str, directory: Path, protocol_path: Path, cache: Path, output: Path
) -> None:
    protocol = json.loads(protocol_path.read_bytes())
    if (
        digest_file(directory / "sealed-test.json") != protocol["cases_sha256"]
        or digest_file(directory / "protocol.json")
        != protocol["original_protocol_sha256"]
    ):
        raise ValueError("frozen input fingerprints changed")
    cases = json.loads((directory / "sealed-test.json").read_bytes())
    matched = set(protocol["matched_ids"])
    if matched != set(matched_sample(cases)):
        raise ValueError("matched sample changed")
    if kind == "qwen3guard-0.6b":
        cases = [row for row in cases if row["id"] in matched]
    spec = protocol["models"][kind]
    output.mkdir(parents=True, exist_ok=False)
    started = time.perf_counter()
    native_label_counts: Counter[str] = Counter()
    errors: Counter[str] = Counter()
    observations = []
    durations = []
    if kind == "xlmr-large-v2":
        selection = TextModelSettings.model_validate(spec).model_copy(
            update={"cache_dir": cache.resolve() / "hub"}
        )
        policy = output / "policy.json"
        write(
            policy,
            {
                "policies": [
                    {
                        "id": "default",
                        "version": "research-fixed-1",
                        "text": [
                            {
                                "category": "toxicity",
                                "review_threshold": 0.5,
                                "block_threshold": 0.85,
                            }
                        ],
                    }
                ]
            },
        )
        key = secrets.token_urlsafe(32)
        client = TestClient(
            create_app(
                Settings(
                    api_key=SecretStr(key), text_model=selection, policy_file=policy
                )
            ),
            headers={"Authorization": "Bearer " + key},
        )
        client.__enter__()
        load_seconds = time.perf_counter() - started
    else:
        selection_guard = PinnedModelSettings.model_validate(spec)
        torch = importlib.import_module("torch")
        transformers = importlib.import_module("transformers")
        torch.set_num_threads(protocol["threads"])
        options = {
            "revision": selection_guard.revision,
            "cache_dir": cache.resolve() / "hub",
            "local_files_only": True,
            "trust_remote_code": False,
        }
        tokenizer = transformers.AutoTokenizer.from_pretrained(
            selection_guard.id, **options
        )
        model = transformers.AutoModelForCausalLM.from_pretrained(
            selection_guard.id, **options, use_safetensors=True, dtype=torch.float32
        )
        model.eval()
        load_seconds = time.perf_counter() - started
    try:
        with (output / "predictions.jsonl").open("x") as stream:
            for index, case in enumerate(cases, 1):
                began = time.perf_counter()
                record: dict[str, Any] = {
                    "id": case["id"],
                    "source": case["source"],
                    "input_sha256": hashlib.sha256(case["text"].encode()).hexdigest(),
                }
                try:
                    if kind == "xlmr-large-v2":
                        response = client.post(
                            "/v1/moderations",
                            json={"input": {"type": "text", "text": case["text"]}},
                        )
                        if response.status_code != 200:
                            raise ValueError(response.json()["error"]["code"])
                        result = ModerationResult.model_validate(response.json())
                        record["result"] = result.model_dump(mode="json")
                        record["strict"] = record["broad"] = (
                            result.decision.action != "allow"
                        )
                    else:
                        rendered = tokenizer.apply_chat_template(
                            [{"role": "user", "content": case["text"]}], tokenize=False
                        )
                        inputs = tokenizer(
                            [rendered], return_tensors="pt", truncation=False
                        )
                        if inputs["input_ids"].shape[1] > protocol["max_input_tokens"]:
                            raise ValueError("input_too_long")
                        with torch.inference_mode():
                            generated = model.generate(
                                **inputs,
                                max_new_tokens=protocol["max_new_tokens"],
                                do_sample=False,
                                max_time=protocol["budget_seconds"],
                                use_cache=True,
                            )
                        suffix = generated[0][inputs["input_ids"].shape[1] :]
                        generated_text = tokenizer.decode(
                            suffix, skip_special_tokens=True
                        )
                        record["generated"] = generated_text
                        record["generated_tokens"] = len(suffix)
                        if time.perf_counter() - began > protocol["budget_seconds"]:
                            raise ValueError("deadline_exceeded")
                        label, categories = parse_guard(generated_text)
                        record["label"], record["categories"] = label, categories
                        record["strict"], record["broad"] = (
                            label == "Unsafe",
                            label != "Safe",
                        )
                        native_label_counts[label] += 1
                except Exception as exc:
                    # Failed checks stay in the denominator; parse failure is not allow.
                    record["error"] = (
                        str(exc) if isinstance(exc, ValueError) else type(exc).__name__
                    )
                    record.pop("strict", None)
                    record.pop("broad", None)
                    errors[record["error"]] += 1
                record["seconds"] = time.perf_counter() - began
                durations.append(record["seconds"])
                observations.append(record)
                stream.write(json.dumps(record, allow_nan=False) + "\n")
                stream.flush()
                if index % 25 == 0:
                    print(
                        kind, index, "/", len(cases), "errors", dict(errors), flush=True
                    )
    finally:
        if kind == "xlmr-large-v2":
            client.__exit__(None, None, None)
    labels = {row["id"]: row["positive"] for row in cases}
    scores = {}
    for scope in ("all", "matched"):
        subset = [row for row in observations if scope == "all" or row["id"] in matched]
        scores[scope] = {
            source: {
                rule: assessment_metrics(
                    [
                        (labels[row["id"]], row.get(rule))
                        for row in subset
                        if row["source"] == source
                    ]
                )
                for rule in ("strict", "broad")
            }
            for source in ("toxifrench", "toxigen")
        }
    write(
        output / "report.json",
        {
            "model": spec,
            "protocol_sha256": digest_file(protocol_path),
            "collector_sha256": digest_file(Path(__file__)),
            "metrics": scores,
            "errors": dict(errors),
            "native_label_counts": dict(native_label_counts),
            "loading_seconds": load_seconds,
            "latency_seconds": {
                "p50": percentile(durations, 0.5),
                "p95": percentile(durations, 0.95),
                "max": max(durations),
            },
            "process_peak_rss_kib": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
            "platform": platform.platform(),
            "cpu_quota": Path("/sys/fs/cgroup/cpu.max").read_text().strip(),
            "execution": "actual Modera ASGI HTTP/serving engine"
            if kind == "xlmr-large-v2"
            else "separate generative research path; no serving adapter",
            "predictions_sha256": digest_file(output / "predictions.jsonl"),
            "limitations": protocol["limitations"],
        },
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("prepare", "freeze", "run"))
    parser.add_argument(
        "--directory", type=Path, default=Path("evaluation/data/external-v1")
    )
    parser.add_argument("--cache", type=Path, default=Path("model-cache"))
    parser.add_argument("--protocol", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--kind", choices=("xlmr-large-v2", "qwen3guard-0.6b"))
    args = parser.parse_args()
    if args.command == "prepare":
        prepare(args.cache)
    elif args.command == "freeze":
        if args.output is None:
            parser.error("freeze requires --output")
        freeze(args.directory, args.output)
    else:
        if args.kind is None or args.output is None or args.protocol is None:
            parser.error("run requires --kind, --protocol and --output")
        run(args.kind, args.directory, args.protocol, args.cache, args.output)


if __name__ == "__main__":
    main()
