"""Submit labelled photos to a running Modera API and report where it errs."""

import argparse
import hashlib
import json
import os
import time
import urllib.error
import urllib.request
from collections import Counter
from pathlib import Path
from typing import Annotated, Any, Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field

Action = Literal["allow", "review", "block"]
ACTIONS = ("allow", "review", "block")
Name = Annotated[str, Field(min_length=1, max_length=200)]
FLAGS = ("review", "block")


class Case(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    id: Name
    file: Name
    expected: Action
    slice: Name = "all"


class Outcome(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action: Action | None = None
    error: str | None = None
    provenance: str | None = None
    seconds: float = 0.0


def load_cases(path: Path) -> list[tuple[Case, Path]]:
    root = path.resolve().parent
    cases = [Case.model_validate(item) for item in json.loads(path.read_bytes())]
    if not cases or len({case.id for case in cases}) != len(cases):
        raise ValueError("cases must be nonempty with unique IDs")
    located = []
    for case in cases:
        photo = (root / case.file).resolve()
        if not photo.is_relative_to(root) or not photo.is_file():
            raise ValueError(f"{case.id}: photo must be a file beside the cases file")
        located.append((case, photo))
    return located


def check_endpoint(url: str) -> str:
    parts = urlsplit(url)
    local = parts.hostname in {"127.0.0.1", "localhost", "::1"}
    if parts.scheme not in {"http", "https"} or (parts.scheme == "http" and not local):
        raise ValueError("use https, or http only for loopback")
    return url.rstrip("/") + "/v1/moderations"


def submit(
    endpoint: str, key: str, policy: str, data: bytes, timeout: float
) -> Outcome:
    boundary = hashlib.sha256(data).hexdigest()[:32]
    body = b"".join(
        [
            f"--{boundary}\r\n".encode(),
            b'Content-Disposition: form-data; name="policy"\r\n\r\n',
            policy.encode(),
            f"\r\n--{boundary}\r\n".encode(),
            b'Content-Disposition: form-data; name="file"; filename="photo"\r\n',
            b"Content-Type: application/octet-stream\r\n\r\n",
            data,
            f"\r\n--{boundary}--\r\n".encode(),
        ]
    )
    request = urllib.request.Request(
        endpoint,
        data=body,
        headers={
            "Authorization": f"Bearer {key}",
            "Content-Type": f"multipart/form-data; boundary={boundary}",
        },
    )
    started = time.monotonic()
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload: dict[str, Any] = json.load(response)
    except urllib.error.HTTPError as exc:
        try:
            code = json.load(exc)["error"]["code"]
        except (ValueError, KeyError, TypeError):
            code = f"http_{exc.code}"
        return Outcome(error=str(code), seconds=time.monotonic() - started)
    except (urllib.error.URLError, TimeoutError, OSError):
        return Outcome(error="network", seconds=time.monotonic() - started)
    except ValueError:
        return Outcome(error="invalid_response", seconds=time.monotonic() - started)
    provenance = json.dumps(
        {
            "policy": payload["policy"],
            "models": sorted(payload["models"], key=lambda m: m["id"]),
            "preprocessing": payload["preprocessing_version"],
        },
        sort_keys=True,
    )
    return Outcome(
        action=payload["decision"]["action"],
        provenance=provenance,
        seconds=time.monotonic() - started,
    )


def summarize(cases: list[Case], outcomes: dict[str, Outcome]) -> dict[str, Any]:
    def tally(rows: list[tuple[Case, Outcome]]) -> dict[str, Any]:
        confusion = {e: dict.fromkeys(ACTIONS, 0) for e in ACTIONS}
        failed: Counter[str] = Counter()
        for case, outcome in rows:
            if outcome.action is None:
                failed[case.expected] += 1
            else:
                confusion[case.expected][outcome.action] += 1
        return {
            "cases": len(rows),
            "completed": len(rows) - sum(failed.values()),
            "failed_checks": dict(failed),
            "confusion": confusion,
            "missed": [
                c.id for c, o in rows if c.expected in FLAGS and o.action == "allow"
            ],
            "false_flags": [
                c.id for c, o in rows if c.expected == "allow" and o.action in FLAGS
            ],
            "wrong_level": [
                c.id
                for c, o in rows
                if c.expected in FLAGS and o.action in FLAGS and o.action != c.expected
            ],
        }

    rows = [(case, outcomes[case.id]) for case in cases]
    provenance = sorted({o.provenance for _, o in rows if o.provenance})
    return {
        **tally(rows),
        "failure_codes": dict(Counter(o.error for _, o in rows if o.error)),
        "provenance": [json.loads(p) for p in provenance],
        "provenance_consistent": len(provenance) <= 1,
        "slices": {
            name: tally([(c, o) for c, o in rows if c.slice == name])
            for name in sorted({c.slice for c in cases})
        },
        "total_seconds": round(sum(o.seconds for _, o in rows), 3),
        "slowest_seconds": round(max((o.seconds for _, o in rows), default=0), 3),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cases", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--url", default="http://127.0.0.1:8000")
    parser.add_argument("--policy", default="default")
    parser.add_argument("--timeout", type=float, default=30.0)
    args = parser.parse_args()
    key = os.environ.get("MODERA_API_KEY")
    if not key:
        parser.error("set MODERA_API_KEY in the environment")
    endpoint = check_endpoint(args.url)
    located = load_cases(args.cases)
    outcomes = {
        case.id: submit(endpoint, key, args.policy, photo.read_bytes(), args.timeout)
        for case, photo in located
    }
    report = summarize([case for case, _ in located], outcomes)
    with args.output.open("x") as output:
        output.write(json.dumps(report, indent=2, allow_nan=False) + "\n")
    print(
        f"{report['completed']}/{report['cases']} completed; "
        f"missed={len(report['missed'])} false_flags={len(report['false_flags'])} "
        f"failed_checks={sum(report['failed_checks'].values())}"
    )


if __name__ == "__main__":
    main()
