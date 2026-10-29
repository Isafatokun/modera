"""Verify the real reference profile in an isolated Compose project with no cache."""

import argparse
import json
import os
import platform
import secrets
import subprocess
import time
from pathlib import Path
from typing import Any
from uuid import uuid4


def verify(output: Path) -> None:
    output.mkdir(parents=True, exist_ok=False)
    root = Path(__file__).resolve().parents[2]
    project = "modera-verify-" + uuid4().hex
    environment = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(("MODERA_", "COMPOSE_"))
    }
    environment.update(
        MODERA_API_KEY=secrets.token_urlsafe(32),
        MODERA_IMAGE="modera:" + project,
        MODERA_PORT="0",
        MODERA_MODELS_FILE=str(root / "deploy/reference/models.json"),
        MODERA_POLICIES_FILE=str(root / "deploy/reference/policies.json"),
        COMPOSE_DISABLE_ENV_FILE="1",
        MODERA_IMAGE_ALLOWED_HOSTS='["httpbingo.org"]',
        MODERA_CHECK_IMAGE_URL="https://httpbingo.org/image/png",
        MODERA_CHECK_UNAVAILABLE_URL="https://httpbingo.org/status/503",
    )
    command = [
        "docker",
        "compose",
        "--project-name",
        project,
        "--file",
        str(root / "compose.yaml"),
    ]
    report: dict[str, Any] = {
        "status": "failed",
        "project": project,
        "platform": platform.platform(),
        "runner_logical_cpus": os.cpu_count(),
        "models": json.loads((root / "deploy/reference/models.json").read_text()),
        "policy": json.loads((root / "deploy/reference/policies.json").read_text()),
        "checks": [],
    }

    def run(args: list[str], *, required: bool = True) -> str:
        result = subprocess.run(
            args, cwd=root, env=environment, text=True, capture_output=True, timeout=900
        )
        with (output / "commands.log").open("a") as log:
            log.write(json.dumps(args) + "\n" + result.stdout + result.stderr + "\n")
        if required and result.returncode:
            raise RuntimeError(f"Command failed with exit {result.returncode}: {args}")
        return result.stdout.strip()

    def compose(*args: str, required: bool = True) -> str:
        return run(command + list(args), required=required)

    try:
        report["commit"] = run(["git", "rev-parse", "HEAD"])
        report["compose_version"] = run(["docker", "compose", "version", "--short"])
        report["docker_host"] = json.loads(
            run(
                [
                    "docker",
                    "info",
                    "--format",
                    '{"cpus":{{.NCPU}},"memory_bytes":{{.MemTotal}}}',
                ]
            )
        )
        started = time.monotonic()
        compose("build", "modera")
        report["build_seconds"] = time.monotonic() - started
        compose(
            "run",
            "--rm",
            "--no-deps",
            "prepare",
            "python",
            "-c",
            "from pathlib import Path; "
            "assert not any(Path('/var/cache/modera').iterdir())",
        )
        report["empty_model_cache_verified"] = True
        started = time.monotonic()
        compose(
            "up", "--no-build", "--force-recreate", "--wait", "--wait-timeout", "600"
        )
        report["prepare_and_ready_seconds"] = time.monotonic() - started
        report["container_memory_samples"] = []
        for attempt in range(2):
            started = time.monotonic()
            if attempt:
                compose("restart", "modera")
            checks = compose("--profile", "checks", "run", "--rm", "--no-deps", "check")
            for expected in (
                "text: passed",
                "photo file: passed",
                "photo URL: passed",
                "unavailable source: retryable error without a decision",
                "health, authentication and metrics: passed",
            ):
                assert expected in checks, f"Missing check result: {expected}"
            report["checks"].append(
                {
                    "phase": "restart" if attempt else "cold_start",
                    "seconds_including_requests": time.monotonic() - started,
                    "output": checks,
                }
            )
            container = compose("ps", "--quiet", "modera")
            report["container_memory_samples"].append(
                json.loads(
                    run(
                        [
                            "docker",
                            "stats",
                            "--no-stream",
                            "--format",
                            "{{json .}}",
                            container,
                        ]
                    )
                )
            )
        compose("stop", "--timeout", "45", "modera")
        container = compose("ps", "--all", "--quiet", "modera")
        state = json.loads(run(["docker", "inspect", container]))[0]["State"]
        assert state["ExitCode"] in (0, 143) and not state["OOMKilled"]
        logs = compose("logs", "--no-color", "modera")
        assert logs.count("Application shutdown complete.") == 2
        report["graceful_shutdown_verified"] = True
        report["status"] = "passed"
    finally:
        try:
            compose("logs", "--no-color", required=False)
        finally:
            try:
                compose("down", "--volumes", "--remove-orphans", required=False)
            finally:
                (output / "result.json").write_text(json.dumps(report, indent=2) + "\n")
                print(json.dumps(report, indent=2), flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    verify(parser.parse_args().output)
