import json
import os
import signal
import socket
import subprocess
import sys
import time
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path

import httpx2 as httpx
import pytest


@contextmanager
def service(
    environment: dict[str, str],
    log_path: Path,
    port: int,
    bootstrap: Path | None = None,
) -> Iterator[tuple[subprocess.Popen[str], httpx.Client]]:
    root = Path(__file__).resolve().parents[2]
    with log_path.open("w+") as log:
        command = (
            [sys.executable, str(bootstrap), str(port)]
            if bootstrap
            else [
                sys.executable,
                "-m",
                "uvicorn",
                "modera.main:create_app",
                "--factory",
                "--host",
                "127.0.0.1",
                "--port",
                str(port),
                "--workers",
                "1",
                "--no-access-log",
                "--no-server-header",
                "--no-proxy-headers",
                "--timeout-graceful-shutdown",
                "30",
            ]
        )
        process = subprocess.Popen(
            command,
            env=environment,
            cwd=root,
            stdout=log,
            stderr=log,
            text=True,
        )
        try:
            with httpx.Client(
                base_url=f"http://127.0.0.1:{port}", trust_env=False, timeout=5
            ) as client:
                yield process, client
        finally:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=5)


def wait_ready(process: subprocess.Popen[str], client: httpx.Client) -> None:
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        assert process.poll() is None, "service exited before readiness"
        try:
            if client.get("/health/ready").status_code == 200:
                return
        except httpx.HTTPError:
            pass
        time.sleep(0.05)
    raise AssertionError("service did not become ready")


def stop(process: subprocess.Popen[str], log_path: Path) -> None:
    process.send_signal(signal.SIGTERM)
    assert_shutdown(process, log_path)


def assert_shutdown(process: subprocess.Popen[str], log_path: Path) -> None:
    process.wait(timeout=10)
    assert process.returncode in (0, -signal.SIGTERM)
    assert "Application shutdown complete." in log_path.read_text()


def unused_port() -> int:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def test_rules_example_over_http_and_graceful_shutdown(
    tmp_path: Path, auth_headers: dict[str, str]
) -> None:
    root = Path(__file__).resolve().parents[2]
    environment = {
        key: value for key, value in os.environ.items() if not key.startswith("MODERA_")
    }
    environment.update(
        MODERA_API_KEY=auth_headers["Authorization"][7:],
        MODERA_POLICY_FILE=str(root / "examples/policies.json"),
        MODERA_TEXT_RULES_FILE=str(root / "examples/text-rules.json"),
    )
    log_path = tmp_path / "server.log"
    with service(environment, log_path, unused_port()) as (process, client):
        wait_ready(process, client)
        payload = {
            "input": {"type": "text", "text": "hello"},
            "policy": "rules-only",
        }
        assert client.post("/v1/moderations", json=payload).status_code == 401
        for text, action in [
            ("hello", "allow"),
            ("example prohibited phrase", "block"),
        ]:
            response = client.post(
                "/v1/moderations",
                headers=auth_headers,
                json={
                    "input": {"type": "text", "text": text},
                    "policy": "rules-only",
                },
            )
            assert response.status_code == 200
            assert response.json()["decision"]["action"] == action
            assert response.json()["models"] == []
            assert not response.json()["fallback"]
        assert client.get("/metrics", headers=auth_headers).status_code == 200
        stop(process, log_path)


@pytest.mark.inference
def test_loaded_model_shutdown_restart_and_missing_weights(
    tmp_path: Path, auth_headers: dict[str, str], image_bytes: bytes
) -> None:
    pytest.importorskip("torch")
    transformers = pytest.importorskip("transformers")
    revision = "a" * 40
    snapshot = tmp_path / "models--example--image" / "snapshots" / revision
    snapshot.mkdir(parents=True)
    config = transformers.ViTConfig(
        image_size=16,
        patch_size=8,
        hidden_size=16,
        num_hidden_layers=1,
        num_attention_heads=2,
        intermediate_size=32,
        num_labels=2,
        id2label={0: "sfw", 1: "nsfw"},
        label2id={"sfw": 0, "nsfw": 1},
    )
    transformers.ViTForImageClassification(config).save_pretrained(
        snapshot, safe_serialization=True
    )
    transformers.ViTImageProcessor(size={"height": 16, "width": 16}).save_pretrained(
        snapshot
    )
    selection = {
        "image": {
            "id": "example/image",
            "revision": revision,
            "cache_dir": str(tmp_path),
            "label_map": {"nsfw": "sexual"},
        }
    }
    model_file = tmp_path / "models.json"
    model_file.write_text(json.dumps(selection))
    policy_file = tmp_path / "policy.json"
    policy_file.write_text(
        '{"policies":[{"id":"default","version":"1",'
        '"image":[{"category":"sexual","block_threshold":0.8}]}]}'
    )
    environment = {
        key: value for key, value in os.environ.items() if not key.startswith("MODERA_")
    }
    environment.update(
        MODERA_API_KEY=auth_headers["Authorization"][7:],
        MODERA_MODEL_FILE=str(model_file),
        MODERA_POLICY_FILE=str(policy_file),
        HF_HUB_OFFLINE="1",
    )
    port = unused_port()
    bootstrap = tmp_path / "drain_server.py"
    bootstrap.write_text(
        "import sys, time\n"
        "from pathlib import Path\n"
        "import uvicorn\n"
        "from modera.inference import Classifier, PreparedImage\n"
        "root = Path(__file__).parent\n"
        "predict = Classifier.predict\n"
        "def gated(self, content):\n"
        "    if isinstance(content, PreparedImage) and (root / 'armed').exists():\n"
        "        (root / 'entered').touch()\n"
        "        deadline = time.monotonic() + 10\n"
        "        while not (root / 'released').exists():\n"
        "            if time.monotonic() > deadline:\n"
        "                raise TimeoutError('test gate not released')\n"
        "            time.sleep(0.01)\n"
        "    return predict(self, content)\n"
        "Classifier.predict = gated\n"
        "uvicorn.run('modera.main:create_app', factory=True, host='127.0.0.1',\n"
        "    port=int(sys.argv[1]), access_log=False, server_header=False,\n"
        "    proxy_headers=False, workers=1, timeout_graceful_shutdown=30)\n"
    )
    results = []
    for attempt in range(2):
        log_path = tmp_path / f"model-{attempt}.log"
        with service(environment, log_path, port, bootstrap) as (process, client):
            wait_ready(process, client)
            assert client.get("/health/live").status_code == 200
            response = client.post(
                "/v1/moderations",
                headers=auth_headers,
                files={"file": ("photo.png", image_bytes, "image/png")},
            )
            assert response.status_code == 200, response.text
            result = response.json()
            assert result["models"] == [{"id": "example/image", "revision": revision}]
            assert result["coverage"]["evaluated"] == ["sexual"]
            assert not result["fallback"]
            assert client.get("/metrics", headers=auth_headers).status_code == 200
            results.append(result)
            if attempt == 0:
                (tmp_path / "armed").touch()
                with ThreadPoolExecutor(max_workers=1) as requests:
                    pending = requests.submit(
                        client.post,
                        "/v1/moderations",
                        headers=auth_headers,
                        files={"file": ("photo.png", image_bytes, "image/png")},
                    )
                    try:
                        deadline = time.monotonic() + 3
                        while not (tmp_path / "entered").exists():
                            assert time.monotonic() < deadline, "request did not enter"
                            time.sleep(0.01)
                        process.send_signal(signal.SIGTERM)
                        deadline = time.monotonic() + 3
                        while (
                            "Waiting for connections to close"
                            not in log_path.read_text()
                        ):
                            assert process.poll() is None
                            assert time.monotonic() < deadline, "server did not drain"
                            time.sleep(0.01)
                        assert not pending.done()
                    finally:
                        (tmp_path / "released").touch()
                    drained = pending.result(timeout=5)
                    assert drained.status_code == 200, drained.text
                    assert drained.json()["findings"] == result["findings"]
                assert_shutdown(process, log_path)
                (tmp_path / "armed").unlink()
            else:
                stop(process, log_path)
    assert results[0]["request_id"] != results[1]["request_id"]
    for key in ("decision", "findings", "policy", "preprocessing_version"):
        assert results[0][key] == results[1][key]

    selection["image"]["revision"] = "b" * 40
    model_file.write_text(json.dumps(selection))
    log_path = tmp_path / "missing-weights.log"
    with service(environment, log_path, port) as (process, client):
        process.wait(timeout=30)
        assert process.returncode not in (None, 0)
        assert (
            "configured classifier could not be loaded or warmed"
            in log_path.read_text()
        )
        assert "Application startup complete." not in log_path.read_text()
        with pytest.raises(httpx.HTTPError):
            client.get("/health/ready")
