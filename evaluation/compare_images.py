"""Compare frozen image policies on exactly the same labelled cases."""

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any

from evaluation.assess import AssessmentObservation
from evaluation.report import Action, Case, summarize

PROFILES = ("classifier", "detector", "combined")


def compare(
    cases: list[Case], runs: dict[str, list[AssessmentObservation]]
) -> dict[str, Any]:
    if not cases or len({case.id for case in cases}) != len(cases):
        raise ValueError("cases must be nonempty with unique IDs")
    if any(case.input_type != "image" or case.split != "test" for case in cases):
        raise ValueError("only held-out image cases are accepted")
    if {case.expected for case in cases} != {"allow", "block"}:
        raise ValueError("include both allow and block labels; review is an outcome")
    if set(runs) != set(PROFILES):
        raise ValueError("provide classifier, detector and combined observations")
    expected_ids = {case.id for case in cases}
    predictions: dict[str, dict[str, Action | None]] = {}
    reports: dict[str, Any] = {}
    request_ids: set[str] = set()
    for name in PROFILES:
        observations = runs[name]
        if (
            len(observations) != len(cases)
            or {o.id for o in observations} != expected_ids
        ):
            raise ValueError(f"{name}: observations must match case IDs exactly")
        predictions[name] = {}
        provenance: str | None = None
        for observation in observations:
            result = observation.result
            action: Action | None = None
            if result is not None:
                if result.input_type != "image":
                    raise ValueError("image results required")
                if result.request_id in request_ids:
                    raise ValueError("request IDs must be distinct across runs")
                request_ids.add(result.request_id)
                signature = json.dumps(
                    {
                        "models": sorted(
                            (m.model_dump() for m in result.models),
                            key=lambda m: m["id"],
                        ),
                        "policy": result.policy.model_dump(),
                        "preprocessing": result.preprocessing_version,
                    },
                    sort_keys=True,
                )
                if provenance is not None and signature != provenance:
                    raise ValueError(f"{name}: provenance changed during run")
                provenance = signature
                if (
                    not result.fallback
                    and not result.coverage.unsupported
                    and all(f.source == "model" for f in result.findings)
                ):
                    action = result.decision.action
            predictions[name][observation.id] = action
        rows = [(case, predictions[name][case.id]) for case in cases]
        reports[name] = {
            "provenance": json.loads(provenance) if provenance else None,
            "overall": summarize(rows),
            "slices": {
                label: summarize([(c, a) for c, a in rows if c.slice == label])
                for label in sorted({c.slice for c in cases})
            },
            "escalation": {
                "positives": sum(c.expected == "block" for c in cases),
                "missed": sum(c.expected == "block" and a == "allow" for c, a in rows),
                "false_alarms": sum(
                    c.expected == "allow" and a in ("review", "block") for c, a in rows
                ),
                "failed_positive": sum(
                    c.expected == "block" and a is None for c, a in rows
                ),
                "failed_negative": sum(
                    c.expected == "allow" and a is None for c, a in rows
                ),
            },
        }
    return {
        "profiles": reports,
        "paired": {
            name: {
                "both_completed": sum(
                    predictions[name][c.id] is not None
                    and predictions["combined"][c.id] is not None
                    for c in cases
                ),
                "decisions_changed": sum(
                    predictions[name][c.id] is not None
                    and predictions["combined"][c.id] is not None
                    and predictions[name][c.id] != predictions["combined"][c.id]
                    for c in cases
                ),
            }
            for name in ("classifier", "detector")
        },
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cases", type=Path, required=True)
    for name in PROFILES:
        parser.add_argument(f"--{name}", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    paths = {"cases": args.cases, **{n: getattr(args, n) for n in PROFILES}}
    # Hash the exact bytes parsed, even if a source file changes concurrently.
    raw = {name: path.read_bytes() for name, path in paths.items()}
    cases = [Case.model_validate(c) for c in json.loads(raw["cases"])]
    runs = {
        name: [
            AssessmentObservation.model_validate_json(line)
            for line in raw[name].splitlines()
        ]
        for name in PROFILES
    }
    report = compare(cases, runs)
    report["inputs_sha256"] = {
        name: hashlib.sha256(data).hexdigest() for name, data in raw.items()
    }
    report["comparator_sha256"] = hashlib.sha256(
        Path(__file__).read_bytes()
    ).hexdigest()
    with args.output.open("x") as output:
        output.write(json.dumps(report, indent=2, allow_nan=False) + "\n")


if __name__ == "__main__":
    main()
