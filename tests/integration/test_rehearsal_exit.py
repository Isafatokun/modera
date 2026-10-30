"""Distinguish normal Uvicorn TERM exit from injected uncooperative native work."""

import os
import signal
import socket
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import httpx2 as httpx
import pytest

from deploy.rehearsal.rehearse import until


@pytest.mark.inference
@pytest.mark.skipif(sys.platform != "linux", reason="Linux process rehearsal")
@pytest.mark.parametrize("stubborn", [False, True])
def test_rehearsal_exit_fault_is_explicit(
    tmp_path: Path,
    image_deployment: tuple[Path, Path],
    image_bytes: bytes,
    auth_headers: dict[str, str],
    stubborn: bool,
) -> None:
    root = Path(__file__).resolve().parents[2]
    model, policy = image_deployment
    environment = {k: v for k, v in os.environ.items() if not k.startswith("MODERA_")}
    environment.update(
        MODERA_API_KEY=auth_headers["Authorization"][7:],
        MODERA_MODEL_FILE=str(model),
        MODERA_POLICY_FILE=str(policy),
        MODERA_INFERENCE_TIMEOUT_SECONDS="60",
        HF_HUB_OFFLINE="1",
    )
    with socket.socket() as listener, (tmp_path / "server.log").open("w") as log:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
        args = [
            sys.executable,
            str(root / "deploy/rehearsal/bootstrap.py"),
            "--fd",
            str(listener.fileno()),
            "--control",
            str(tmp_path),
        ]
        if stubborn:
            args.append("--stubborn-exit")
        process = subprocess.Popen(
            args,
            cwd=root,
            env=environment,
            pass_fds=(listener.fileno(),),
            stdout=log,
            stderr=log,
        )
        listener.close()
        try:
            with httpx.Client(
                base_url=f"http://127.0.0.1:{port}", trust_env=False, timeout=10
            ) as client:

                def ready() -> bool:
                    assert process.poll() is None, "server exited before readiness"
                    try:
                        return client.get("/health/ready").status_code == 200
                    except httpx.HTTPError:
                        return False

                until(ready, "server readiness timeout", 30)
                (tmp_path / "armed").touch()

                def request() -> None:
                    try:
                        response = client.post(
                            "/v1/moderations",
                            headers=auth_headers,
                            files={"file": ("photo.png", image_bytes, "image/png")},
                        )
                        assert response.status_code == 500
                    except httpx.TransportError:
                        pass

                with ThreadPoolExecutor(max_workers=1) as pool:
                    pending = pool.submit(request)
                    try:
                        until(
                            lambda: (tmp_path / "entered").exists(),
                            "native gate not entered",
                        )
                        process.terminate()
                        if stubborn:
                            with pytest.raises(subprocess.TimeoutExpired):
                                process.wait(timeout=3)
                            process.kill()
                            process.wait(timeout=5)
                            assert process.returncode == -signal.SIGKILL
                        else:
                            process.wait(timeout=5)
                            assert process.returncode in (0, -signal.SIGTERM)
                    finally:
                        if process.poll() is None:
                            process.kill()
                            process.wait(timeout=5)
                    pending.result(timeout=5)
        finally:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=5)
    assert not (tmp_path / "released").exists()
    assert "Application shutdown complete." in (tmp_path / "server.log").read_text()
