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

import httpx2 as httpx
from PIL import Image


def main() -> None:
    root = Path(__file__).resolve().parents[2]
    environment = {k: v for k, v in os.environ.items() if not k.startswith("MODERA_")}
    environment.update(
        MODERA_API_KEY=secrets.token_urlsafe(32),
        MODERA_POLICY_FILE=str(root / "examples/policies.json"),
        MODERA_TEXT_RULES_FILE=str(root / "examples/text-rules.json"),
    )
    with socket.socket() as listener, tempfile.TemporaryFile(mode="w+") as log:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
        environment["MODERA_CHECK_BASE_URL"] = f"http://127.0.0.1:{port}"
        process = subprocess.Popen(
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
            cwd=root,
            env=environment,
            pass_fds=(listener.fileno(),),
            stdout=log,
            stderr=log,
        )
        try:
            with httpx.Client(trust_env=False, timeout=1) as client:
                for _ in range(60):
                    if process.poll() is not None:
                        raise RuntimeError("reference API exited before readiness")
                    try:
                        if (
                            client.get(
                                environment["MODERA_CHECK_BASE_URL"] + "/health/ready"
                            ).status_code
                            == 200
                        ):
                            break
                    except httpx.TransportError:
                        pass
                    time.sleep(0.1)
                else:
                    raise RuntimeError("reference API did not become ready")
            subprocess.run(
                ["go", "run", "./cmd/check"],
                cwd=root / "clients/go",
                env=environment,
                check=True,
                timeout=60,
            )
            with tempfile.TemporaryDirectory(prefix="modera-example-") as temporary:
                binary = str(Path(temporary) / "moderate")
                subprocess.run(
                    ["go", "build", "-o", binary, "./cmd/moderate"],
                    cwd=root / "clients/go",
                    env=environment,
                    check=True,
                    timeout=60,
                )
                with BytesIO() as buffer, Image.new("RGB", (2, 2), "white") as image:
                    image.save(buffer, format="PNG")
                    photo = buffer.getvalue()
                for args, content, status, disposition in [
                    (["--policy", "rules-only"], b"hello", 0, "publish"),
                    (
                        ["--policy", "rules-only"],
                        b"example prohibited phrase",
                        3,
                        "reject",
                    ),
                    (
                        ["--policy", "rules-review"],
                        b"example prohibited phrase",
                        2,
                        "review",
                    ),
                    (["--input", "image"], photo, 4, "pending"),
                    (
                        ["--input", "image-url"],
                        b"https://unreachable.invalid/photo.png\n",
                        4,
                        "pending",
                    ),
                ]:
                    result = subprocess.run(
                        [
                            binary,
                            "--base-url",
                            environment["MODERA_CHECK_BASE_URL"],
                            *args,
                        ],
                        input=content,
                        capture_output=True,
                        env=environment,
                        timeout=20,
                    )
                    output = json.loads(result.stdout)
                    if (
                        result.returncode != status
                        or output["disposition"] != disposition
                    ):
                        raise RuntimeError(
                            "Go application exit/disposition contract failed"
                        )
                    if disposition == "pending" and "result" in output:
                        raise RuntimeError(
                            "application error included a moderation decision"
                        )
                print(
                    "Go application: publish/review/reject/pending "
                    "and real image upload passed"
                )
        finally:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
            if process.returncode not in (0, -15):
                log.seek(0)
                print(log.read(), file=sys.stderr)


if __name__ == "__main__":
    main()
