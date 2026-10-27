import os
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx2 as httpx


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
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    with (tmp_path / "server.log").open("w+") as log:
        process = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "uvicorn",
                "modera.main:create_app",
                "--factory",
                "--host",
                "127.0.0.1",
                "--port",
                str(port),
                "--no-access-log",
                "--no-server-header",
                "--no-proxy-headers",
            ],
            env=environment,
            cwd=root,
            stdout=log,
            stderr=log,
        )
        try:
            with httpx.Client(
                base_url=f"http://127.0.0.1:{port}", trust_env=False, timeout=2
            ) as client:
                for _ in range(200):
                    assert process.poll() is None, "service exited before readiness"
                    try:
                        if client.get("/health/ready").status_code == 200:
                            break
                    except httpx.HTTPError:
                        pass
                    time.sleep(0.05)
                else:
                    raise AssertionError("service did not become ready")
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
            process.send_signal(signal.SIGTERM)
            process.wait(timeout=5)
            assert process.returncode in (0, -signal.SIGTERM)
            log.seek(0)
            assert "Application shutdown complete." in log.read()
        finally:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=5)
