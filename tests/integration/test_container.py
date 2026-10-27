import json
import os
import subprocess
import time
from pathlib import Path
from typing import Any
from uuid import uuid4

import httpx2 as httpx
import pytest

pytestmark = [
    pytest.mark.inference,
    pytest.mark.skipif(
        not os.environ.get("MODERA_TEST_CONTAINER_IMAGE"),
        reason="requires an explicitly selected inference-enabled Docker image",
    ),
]


def docker(*arguments: str) -> str:
    result = subprocess.run(
        ["docker", *arguments], capture_output=True, text=True, timeout=60
    )
    assert result.returncode == 0, result.stderr
    if arguments[0] == "logs":
        return result.stdout + result.stderr
    return result.stdout.strip()


def test_inference_container_with_read_only_cache_and_restart(
    tmp_path: Path,
    image_deployment: tuple[Path, Path],
    auth_headers: dict[str, str],
    image_bytes: bytes,
) -> None:
    model_file, policy_file = image_deployment
    tmp_path.chmod(0o755)
    name = f"modera-test-{uuid4().hex}"
    environment = dict(os.environ)
    environment["MODERA_API_KEY"] = auth_headers["Authorization"][7:]
    try:
        subprocess.run(
            [
                "docker",
                "run",
                "--detach",
                "--name",
                name,
                "--read-only",
                "--cap-drop=ALL",
                "--security-opt=no-new-privileges",
                "--tmpfs",
                "/tmp",
                "--publish",
                "127.0.0.1::8000",
                "--volume",
                f"{tmp_path}:/etc/modera:ro",
                "--env",
                "MODERA_API_KEY",
                "--env",
                f"MODERA_MODEL_FILE=/etc/modera/{model_file.name}",
                "--env",
                f"MODERA_POLICY_FILE=/etc/modera/{policy_file.name}",
                "--env",
                "HF_HUB_OFFLINE=1",
                "--env",
                "WEB_CONCURRENCY=4",
                os.environ["MODERA_TEST_CONTAINER_IMAGE"],
            ],
            env=environment,
            capture_output=True,
            text=True,
            check=True,
            timeout=60,
        )
        details = json.loads(docker("inspect", name))[0]
        assert details["Config"]["User"] == "10001:10001"
        assert details["HostConfig"]["ReadonlyRootfs"]
        assert "ALL" in details["HostConfig"]["CapDrop"]
        assert any(
            mount["Destination"] == "/etc/modera" and not mount["RW"]
            for mount in details["Mounts"]
        )
        results: list[dict[str, Any]] = []
        for attempt in range(2):
            if attempt:
                docker("start", name)
            details = json.loads(docker("inspect", name))[0]
            port = details["NetworkSettings"]["Ports"]["8000/tcp"][0]["HostPort"]
            with httpx.Client(
                base_url=f"http://127.0.0.1:{port}", trust_env=False, timeout=5
            ) as client:
                deadline = time.monotonic() + 60
                while time.monotonic() < deadline:
                    try:
                        if client.get("/health/ready").status_code == 200:
                            break
                    except httpx.HTTPError:
                        if (
                            docker("inspect", "--format", "{{.State.Running}}", name)
                            != "true"
                        ):
                            pytest.fail("container exited: " + docker("logs", name))
                    time.sleep(0.1)
                else:
                    pytest.fail(
                        "container did not become ready: " + docker("logs", name)
                    )
                response = client.post(
                    "/v1/moderations",
                    headers=auth_headers,
                    files={"file": ("photo.png", image_bytes, "image/png")},
                )
                assert response.status_code == 200, response.text
                result = response.json()
                assert result["models"] == [
                    {"id": "example/image", "revision": "a" * 40}
                ]
                assert result["coverage"]["evaluated"] == ["sexual"]
                assert not result["fallback"]
                assert client.get("/metrics", headers=auth_headers).status_code == 200
                results.append(result)
            docker("stop", "--time", "45", name)
            details = json.loads(docker("inspect", name))[0]
            assert details["State"]["ExitCode"] in (0, 143)
            assert not details["State"]["OOMKilled"]
            logs = docker("logs", name)
            assert logs.count("Application shutdown complete.") == attempt + 1
            assert logs.count("Application startup complete.") == attempt + 1
        assert results[0]["request_id"] != results[1]["request_id"]
        for field in ("findings", "decision", "policy", "preprocessing_version"):
            assert results[0][field] == results[1][field]
    finally:
        subprocess.run(
            ["docker", "rm", "--force", name],
            capture_output=True,
            timeout=60,
        )
