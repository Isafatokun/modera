"""The upload gate against a real Modera process over HTTP."""

import os
import socket
import subprocess
import sys
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import httpx2 as httpx
import pytest

from examples.upload_gate import (
    PermanentError,
    RetryableError,
    UploadGate,
    http_moderator,
)

BOOTSTRAP = """
import sys, time
import uvicorn
from pathlib import Path
from modera import engine
from modera.config import Settings
from modera.main import create_app
from modera.model_config import ImageModelSettings
from modera.results import Finding

calls = []

class Stalls:
    def __init__(self, settings, threads):
        pass

    def predict(self, content):
        calls.append(1)
        if len(calls) == 1:
            time.sleep(1.2)  # outlives the 0.3 s inference deadline
        return [Finding(category="sexual", score=0.9)]

engine.create_adapter = Stalls
app = create_app(Settings(
    policy_file=Path(sys.argv[2]),
    image_model=ImageModelSettings(
        id="example/image", revision="a" * 40, label_map={"nsfw": "sexual"}
    ),
    inference_timeout_seconds=0.3,
    image_queue_wait_seconds=0.2,
))
uvicorn.run(app, host="127.0.0.1", port=int(sys.argv[1]), access_log=False)
"""


def unused_port() -> int:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


@contextmanager
def modera(tmp_path: Path, api_key: str) -> Iterator[str]:
    root = Path(__file__).resolve().parents[2]
    policy = tmp_path / "policy.json"
    policy.write_text(
        '{"policies":[{"id":"default","version":"1",'
        '"image":[{"category":"sexual","block_threshold":0.8}]}]}'
    )
    script = tmp_path / "server.py"
    script.write_text(BOOTSTRAP)
    port = unused_port()
    environment = {
        key: value for key, value in os.environ.items() if not key.startswith("MODERA_")
    }
    environment["MODERA_API_KEY"] = api_key
    with (tmp_path / "server.log").open("w") as log:
        process = subprocess.Popen(
            [sys.executable, str(script), str(port), str(policy)],
            env=environment,
            cwd=root,
            stdout=log,
            stderr=log,
        )
        try:
            base = f"http://127.0.0.1:{port}"
            deadline = time.monotonic() + 30
            while True:
                assert process.poll() is None, (tmp_path / "server.log").read_text()
                try:
                    if httpx.get(f"{base}/health/ready", trust_env=False).is_success:
                        break
                except httpx.HTTPError:
                    pass
                assert time.monotonic() < deadline, "service did not become ready"
                time.sleep(0.05)
            yield base
        finally:
            process.kill()
            process.wait(timeout=5)


def test_stalled_model_keeps_the_photo_pending_until_modera_answers(
    tmp_path: Path, auth_headers: dict[str, str], image_bytes: bytes
) -> None:
    key = auth_headers["Authorization"][7:]
    with modera(tmp_path, key) as base:
        gate = UploadGate(tmp_path / "gate.db", http_moderator(base, key))
        gate.submit("profile-1", image_bytes)

        # The first call stalls past the inference deadline: a retryable error and
        # no decision. The slot stays busy, so an immediate retry is also refused.
        with pytest.raises(RetryableError) as first:
            gate.attempt("profile-1")
        assert first.value.retry_after >= 1
        with pytest.raises(RetryableError):
            gate.attempt("profile-1")
        assert gate.state("profile-1") == "pending"
        assert not gate.is_visible("profile-1")

        # Retrying with the production policy (wait >= Retry-After, plus jitter)
        # reaches the decision once the stalled call has finished.
        waits: list[float] = []

        def sleep_briefly(seconds: float) -> None:
            waits.append(seconds)  # what production would wait
            time.sleep(0.4)  # but keep the test short

        state = gate.decide(
            "profile-1", deadline_seconds=30, base_delay=0.05, sleep=sleep_briefly
        )
        assert state == "rejected"
        assert not gate.is_visible("profile-1")
        assert all(wait >= 1 for wait in waits)
        assert gate.review_cases() == []


def test_wrong_key_is_permanent_and_never_publishes(
    tmp_path: Path, auth_headers: dict[str, str], image_bytes: bytes
) -> None:
    key = auth_headers["Authorization"][7:]
    with modera(tmp_path, key) as base:
        gate = UploadGate(tmp_path / "gate.db", http_moderator(base, "wrong-key"))
        gate.submit("u1", image_bytes)
        with pytest.raises(PermanentError):
            gate.decide("u1", sleep=lambda _: None)
        assert gate.state("u1") == "pending"


def test_unreachable_modera_leaves_the_upload_pending(
    tmp_path: Path, image_bytes: bytes
) -> None:
    gate = UploadGate(
        tmp_path / "gate.db",
        http_moderator(f"http://127.0.0.1:{unused_port()}", "key", timeout=1),
    )
    gate.submit("u1", image_bytes)
    waits: list[float] = []
    state = gate.decide("u1", max_attempts=3, sleep=waits.append, base_delay=0.01)
    assert state == "pending"
    assert len(waits) == 2
    assert not gate.is_visible("u1")
