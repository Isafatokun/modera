"""Burst experiment: many uploads at once through the upload gate to a real Modera.

Starts a Modera process whose photo model is a stub (``time.sleep``), submits a
burst of uploads through ``examples.upload_gate``, and reports what the server did
with every request. It measures queueing, deadlines and retries. It says nothing
about real-model speed or photo accuracy.

    python -m evaluation.burst --uploads 32 --reps 5

The stall, when enabled, applies to the first model call of each fresh server.
Client and server share one machine, so the numbers describe that machine only.
"""

import argparse
import json
import os
import socket
import statistics
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path

from PIL import Image

from examples.upload_gate import UploadGate, http_moderator

API_KEY = "burst-experiment-key-not-a-deployment-secret"

SERVER = """
import json, sys, time
import uvicorn
from pathlib import Path
from modera import engine
from modera.config import Settings
from modera.main import create_app
from modera.model_config import ImageModelSettings
from modera.results import Finding

policy, log_path = Path(sys.argv[2]), sys.argv[3]
model_seconds, stall_seconds = float(sys.argv[4]), float(sys.argv[5])
deadline = float(sys.argv[6])
log = open(log_path, "a", buffering=1)
calls = []


class Stub:
    def __init__(self, settings, threads):
        pass

    def predict(self, content):
        calls.append(1)
        log.write(f"start {time.monotonic()}\\n")
        time.sleep(stall_seconds if len(calls) == 1 else model_seconds)
        log.write(f"end {time.monotonic()}\\n")
        return [Finding(category="sexual", score=0.1)]


class Tap:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope["path"] != "/v1/moderations":
            return await self.app(scope, receive, send)
        status, body = 0, b""

        async def wrapped(message):
            nonlocal status, body
            if message["type"] == "http.response.start":
                status = message["status"]
            elif message["type"] == "http.response.body":
                body += message.get("body", b"")
            await send(message)

        await self.app(scope, receive, wrapped)
        code = "ok"
        if status != 200:
            try:
                code = json.loads(body)["error"]["code"]
            except Exception:
                code = "unparsed"
        log.write(f"response {time.monotonic()} {status} {code}\\n")


engine.create_adapter = Stub
app = create_app(Settings(
    policy_file=policy,
    image_model=ImageModelSettings(
        id="example/image", revision="a" * 40, label_map={"nsfw": "sexual"}
    ),
    inference_timeout_seconds=deadline,
))
uvicorn.run(Tap(app), host="127.0.0.1", port=int(sys.argv[1]), access_log=False,
            log_level="warning")
"""

POLICY = (
    '{"policies":[{"id":"default","version":"1",'
    '"image":[{"category":"sexual","block_threshold":0.8}]}]}'
)


@dataclass(frozen=True)
class Condition:
    name: str
    deadline: float
    stall: float


CONDITIONS = [
    Condition("A: deadline 5 s, no stall", 5.0, 0.0),
    Condition("B: deadline 0.5 s, no stall", 0.5, 0.0),
    Condition("C: deadline 5 s, 3 s stall", 5.0, 3.0),
    Condition("D: deadline 0.5 s, 3 s stall", 0.5, 3.0),
]


def photo() -> bytes:
    with BytesIO() as buffer, Image.new("RGB", (64, 64), "white") as image:
        image.save(buffer, format="PNG")
        return buffer.getvalue()


def unused_port() -> int:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def wait_ready(base: str, process: subprocess.Popen[bytes]) -> None:
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError("server exited before it was ready")
        try:
            with opener.open(base + "/health/ready", timeout=2):
                return
        except (urllib.error.URLError, OSError):
            time.sleep(0.1)
    raise RuntimeError("server did not become ready")


def summarize_log(path: Path) -> dict[str, object]:
    responses: Counter[str] = Counter()
    events: list[tuple[float, int]] = []
    starts = 0
    for line in path.read_text().splitlines():
        parts = line.split()
        if parts[0] == "response":
            responses[parts[3]] += 1
        else:
            starts += parts[0] == "start"
            events.append((float(parts[1]), 1 if parts[0] == "start" else -1))
    depth = peak = 0
    for _, change in sorted(events):
        depth += change
        peak = max(peak, depth)
    return {
        "responses": dict(responses),
        "model_calls_started": starts,
        "peak_concurrent_model_calls": peak,
    }


def run_once(
    condition: Condition, uploads: int, model_seconds: float
) -> dict[str, object]:
    with tempfile.TemporaryDirectory() as directory:
        tmp = Path(directory)
        (tmp / "policy.json").write_text(POLICY)
        (tmp / "server.py").write_text(SERVER)
        port = unused_port()
        environment = {
            key: value
            for key, value in os.environ.items()
            if not key.startswith("MODERA_")
        }
        environment["MODERA_API_KEY"] = API_KEY
        process = subprocess.Popen(
            [
                sys.executable,
                str(tmp / "server.py"),
                str(port),
                str(tmp / "policy.json"),
                str(tmp / "calls.log"),
                str(model_seconds),
                str(condition.stall),
                str(condition.deadline),
            ],
            env=environment,
        )
        base = f"http://127.0.0.1:{port}"
        try:
            wait_ready(base, process)
            (tmp / "calls.log").touch()
            image = photo()
            attempts = [0] * uploads
            latency = [0.0] * uploads
            states = [""] * uploads

            def one(index: int) -> None:
                inner = http_moderator(base, API_KEY, timeout=30)

                def counted(content: bytes):  # type: ignore[no-untyped-def]
                    attempts[index] += 1
                    return inner(content)

                gate = UploadGate(tmp / "gate.db", counted)
                upload_id = f"u{index}"
                gate.submit(upload_id, image)
                started = time.monotonic()
                states[index] = gate.decide(
                    upload_id, deadline_seconds=60, max_attempts=12
                )
                latency[index] = time.monotonic() - started

            begin = time.monotonic()
            with ThreadPoolExecutor(uploads) as pool:
                list(pool.map(one, range(uploads)))
            drain = time.monotonic() - begin
        finally:
            process.kill()
            process.wait(5)
        server = summarize_log(tmp / "calls.log")
    ordered = sorted(latency)
    return {
        "undecided": states.count("pending"),
        "attempts": sum(attempts),
        "max_attempts_per_upload": max(attempts),
        "median_decision_s": round(statistics.median(ordered), 2),
        "last_decision_s": round(drain, 2),
        **server,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--uploads", type=int, default=32)
    parser.add_argument("--reps", type=int, default=5)
    parser.add_argument("--model-seconds", type=float, default=0.2)
    args = parser.parse_args()
    for condition in CONDITIONS:
        runs = []
        for _ in range(args.reps):
            run = run_once(condition, args.uploads, args.model_seconds)
            runs.append(run)
            print(json.dumps({"condition": condition.name, **run}), flush=True)
        last = sorted(float(str(run["last_decision_s"])) for run in runs)
        attempts = sorted(int(str(run["attempts"])) for run in runs)
        print(
            f"SUMMARY {condition.name}: uploads={args.uploads} reps={args.reps} "
            f"last_decision_s median={statistics.median(last)} "
            f"min={last[0]} max={last[-1]}; attempts median="
            f"{statistics.median(attempts)} min={attempts[0]} max={attempts[-1]}",
            flush=True,
        )


if __name__ == "__main__":
    main()
