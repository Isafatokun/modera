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

ROOT = Path(__file__).resolve().parents[2]
EXPECTED_CHECKS = (
    "text: passed",
    "photo file: passed",
    "photo URL: passed",
    "unavailable source: retryable error without a decision",
    "health, authentication and metrics: passed",
)


def isolated_environment(project: str) -> dict[str, str]:
    environment = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(("MODERA_", "COMPOSE_"))
    }
    environment.update(
        MODERA_API_KEY=secrets.token_urlsafe(32),
        MODERA_IMAGE="modera:" + project,
        MODERA_PORT="0",
        MODERA_MODELS_FILE=str(ROOT / "deploy/reference/models.json"),
        MODERA_POLICIES_FILE=str(ROOT / "deploy/reference/policies.json"),
        COMPOSE_DISABLE_ENV_FILE="1",
        MODERA_IMAGE_ALLOWED_HOSTS='["httpbingo.org"]',
        MODERA_CHECK_IMAGE_URL="https://httpbingo.org/image/png",
        MODERA_CHECK_UNAVAILABLE_URL="https://httpbingo.org/status/503",
    )
    return environment


class Verification:
    def __init__(self, output: Path) -> None:
        output.mkdir(parents=True, exist_ok=False)
        self.output = output
        self.project = "modera-verify-" + uuid4().hex
        self.environment = isolated_environment(self.project)
        self.command = [
            "docker",
            "compose",
            "--project-name",
            self.project,
            "--file",
            str(ROOT / "compose.yaml"),
        ]
        self.report: dict[str, Any] = {
            "status": "failed",
            "project": self.project,
            "platform": platform.platform(),
            "runner_logical_cpus": os.cpu_count(),
            "models": json.loads((ROOT / "deploy/reference/models.json").read_text()),
            "policy": json.loads((ROOT / "deploy/reference/policies.json").read_text()),
            "checks": [],
        }

    def run(self, args: list[str], *, required: bool = True) -> str:
        result = subprocess.run(
            args,
            cwd=ROOT,
            env=self.environment,
            text=True,
            capture_output=True,
            timeout=900,
        )
        with (self.output / "commands.log").open("a") as log:
            log.write(json.dumps(args) + "\n" + result.stdout + result.stderr + "\n")
        if required and result.returncode:
            raise RuntimeError(f"Command failed with exit {result.returncode}: {args}")
        return result.stdout.strip()

    def compose(self, *args: str, required: bool = True) -> str:
        return self.run(self.command + list(args), required=required)

    def record_host(self) -> None:
        self.report["commit"] = self.run(["git", "rev-parse", "HEAD"])
        self.report["compose_version"] = self.run(
            ["docker", "compose", "version", "--short"]
        )
        info_format = '{"cpus":{{.NCPU}},"memory_bytes":{{.MemTotal}}}'
        self.report["docker_host"] = json.loads(
            self.run(["docker", "info", "--format", info_format])
        )

    def build_and_start(self) -> None:
        started = time.monotonic()
        self.compose("build", "modera")
        self.report["build_seconds"] = time.monotonic() - started
        self.compose(
            "run",
            "--rm",
            "--no-deps",
            "prepare",
            "python",
            "-c",
            "from pathlib import Path; "
            "assert not any(Path('/var/cache/modera').iterdir())",
        )
        self.report["empty_model_cache_verified"] = True
        started = time.monotonic()
        self.compose(
            "up", "--no-build", "--force-recreate", "--wait", "--wait-timeout", "600"
        )
        self.report["prepare_and_ready_seconds"] = time.monotonic() - started

    def run_check(self, restart: bool) -> None:
        started = time.monotonic()
        if restart:
            self.compose("restart", "modera")
        checks = self.compose(
            "--profile", "checks", "run", "--rm", "--no-deps", "check"
        )
        for expected in EXPECTED_CHECKS:
            assert expected in checks, f"Missing check result: {expected}"
        self.report["checks"].append(
            {
                "phase": "restart" if restart else "cold_start",
                "seconds_including_requests": time.monotonic() - started,
                "output": checks,
            }
        )
        container = self.compose("ps", "--quiet", "modera")
        stats = self.run(
            ["docker", "stats", "--no-stream", "--format", "{{json .}}", container]
        )
        self.report["container_memory_samples"].append(json.loads(stats))

    def verify_shutdown(self) -> None:
        self.compose("stop", "--timeout", "45", "modera")
        container = self.compose("ps", "--all", "--quiet", "modera")
        state = json.loads(self.run(["docker", "inspect", container]))[0]["State"]
        assert state["ExitCode"] in (0, 143) and not state["OOMKilled"]
        logs = self.compose("logs", "--no-color", "modera")
        assert logs.count("Application shutdown complete.") == 2
        self.report["graceful_shutdown_verified"] = True

    def finish(self) -> None:
        try:
            self.compose("logs", "--no-color", required=False)
        finally:
            try:
                self.compose("down", "--volumes", "--remove-orphans", required=False)
            finally:
                result = json.dumps(self.report, indent=2) + "\n"
                (self.output / "result.json").write_text(result)
                print(json.dumps(self.report, indent=2), flush=True)

    def execute(self) -> None:
        try:
            self.record_host()
            self.build_and_start()
            self.report["container_memory_samples"] = []
            self.run_check(restart=False)
            self.run_check(restart=True)
            self.verify_shutdown()
            self.report["status"] = "passed"
        finally:
            self.finish()


def verify(output: Path) -> None:
    Verification(output).execute()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    verify(parser.parse_args().output)
