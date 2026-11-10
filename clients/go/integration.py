"""Run the Go client's contract checks against the actual local rules-only API."""

import json
import os
import secrets
import socket
import subprocess
import sys
import tempfile
import time
from io import BytesIO
from pathlib import Path
from typing import IO

import httpx2 as httpx
from PIL import Image

ROOT = Path(__file__).resolve().parents[2]
GO_DIRECTORY = ROOT / "clients/go"


def service_environment() -> dict[str, str]:
    environment = {k: v for k, v in os.environ.items() if not k.startswith("MODERA_")}
    environment.update(
        MODERA_API_KEY=secrets.token_urlsafe(32),
        MODERA_POLICY_FILE=str(ROOT / "examples/policies.json"),
        MODERA_TEXT_RULES_FILE=str(ROOT / "examples/text-rules.json"),
    )
    return environment


def start_api(
    listener: socket.socket, environment: dict[str, str], log: IO[str]
) -> subprocess.Popen[bytes]:
    return subprocess.Popen(
        [
            sys.executable,
            "-m",
            "uvicorn",
            "modera.main:create_app",
            "--factory",
            "--fd",
            str(listener.fileno()),
            "--no-access-log",
            "--no-proxy-headers",
        ],
        cwd=ROOT,
        env=environment,
        pass_fds=(listener.fileno(),),
        stdout=log,
        stderr=log,
    )


def wait_until_ready(process: subprocess.Popen[bytes], base_url: str) -> None:
    with httpx.Client(trust_env=False, timeout=1) as client:
        for _ in range(60):
            if process.poll() is not None:
                raise RuntimeError("reference API exited before readiness")
            try:
                if client.get(base_url + "/health/ready").status_code == 200:
                    return
            except httpx.TransportError:
                pass
            time.sleep(0.1)
    raise RuntimeError("reference API did not become ready")


def stop_api(process: subprocess.Popen[bytes], log: IO[str]) -> None:
    process.terminate()
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)
    if process.returncode not in (0, -15):
        log.seek(0)
        print(log.read(), file=sys.stderr)


def go(args: list[str], environment: dict[str, str]) -> None:
    subprocess.run(
        ["go", *args], cwd=GO_DIRECTORY, env=environment, check=True, timeout=60
    )


def white_png() -> bytes:
    with BytesIO() as buffer, Image.new("RGB", (2, 2), "white") as image:
        image.save(buffer, format="PNG")
        return buffer.getvalue()


def check_application(binary: str, environment: dict[str, str], base_url: str) -> None:
    unreachable = b"https://unreachable.invalid/photo.png\n"
    phrase = b"example prohibited phrase"
    cases = [
        (["--policy", "rules-only"], b"hello", 0, "publish"),
        (["--policy", "rules-only"], phrase, 3, "reject"),
        (["--policy", "rules-review"], phrase, 2, "review"),
        (["--input", "image"], white_png(), 4, "pending"),
        (["--input", "image-url"], unreachable, 4, "pending"),
    ]
    for args, content, status, disposition in cases:
        result = subprocess.run(
            [binary, "--base-url", base_url, *args],
            input=content,
            capture_output=True,
            env=environment,
            timeout=20,
        )
        output = json.loads(result.stdout)
        if result.returncode != status or output["disposition"] != disposition:
            raise RuntimeError("Go application exit/disposition contract failed")
        if disposition == "pending" and "result" in output:
            raise RuntimeError("application error included a moderation decision")
    print("Go application: publish/review/reject/pending and real image upload passed")


def main() -> None:
    environment = service_environment()
    with socket.socket() as listener, tempfile.TemporaryFile(mode="w+") as log:
        listener.bind(("127.0.0.1", 0))
        base_url = f"http://127.0.0.1:{listener.getsockname()[1]}"
        environment["MODERA_CHECK_BASE_URL"] = base_url
        process = start_api(listener, environment, log)
        try:
            wait_until_ready(process, base_url)
            go(["run", "./cmd/check"], environment)
            with tempfile.TemporaryDirectory(prefix="modera-example-") as temporary:
                binary = str(Path(temporary) / "moderate")
                go(["build", "-o", binary, "./cmd/moderate"], environment)
                check_application(binary, environment, base_url)
        finally:
            stop_api(process, log)


if __name__ == "__main__":
    main()
