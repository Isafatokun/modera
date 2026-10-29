"""Linux acceptance rehearsal: Go SDK -> NGINX -> two real-model instances."""

import argparse
import json
import os
import platform
import secrets
import signal
import socket
import subprocess
import sys
import time
from collections.abc import Callable
from contextlib import ExitStack, suppress
from pathlib import Path
from typing import Any

import httpx2 as httpx
from PIL import Image

ROOT = Path(__file__).resolve().parents[2]
Request = tuple[subprocess.Popen[str], Path, Path]


def require(condition: object, message: str) -> None:
    if not condition:
        raise RuntimeError(message)


def until(check: Callable[[], bool], message: str, seconds: float = 10) -> None:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if check():
            return
        time.sleep(0.02)
    raise RuntimeError(message)


def unused_port() -> int:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def gateway_config(root: Path, port: int, backends: list[int], generation: int) -> str:
    def quote(path: Path) -> str:
        return '"' + str(path).replace("\\", "\\\\").replace('"', '\\"') + '"'

    return f"""daemon off;
worker_processes 1;
worker_shutdown_timeout 10s;
pid {quote(root / "nginx.pid")};
error_log {quote(root / "nginx.log")} notice;
events {{ worker_connections 128; }}
http {{
    access_log off;
    client_body_temp_path {quote(root / "body")};
    proxy_temp_path {quote(root / "proxy")};
    fastcgi_temp_path {quote(root / "fastcgi")};
    uwsgi_temp_path {quote(root / "uwsgi")};
    scgi_temp_path {quote(root / "scgi")};
    upstream modera {{ {"".join(f"server 127.0.0.1:{p};" for p in backends)} }}
    server {{
        listen 127.0.0.1:{port};
        server_tokens off;
        client_max_body_size 9m;
        client_body_timeout 5s;
        keepalive_timeout 0;
        location / {{
            proxy_pass http://modera;
            proxy_http_version 1.1;
            proxy_set_header Connection close;
            proxy_set_header X-Forwarded-For "";
            proxy_set_header X-Forwarded-Proto "";
            proxy_connect_timeout 1s;
            proxy_send_timeout 5s;
            proxy_read_timeout 10s;
            proxy_next_upstream off;
            proxy_intercept_errors off;
            add_header X-Rehearsal-Upstream $upstream_addr always;
            add_header X-Rehearsal-Generation {generation} always;
        }}
    }}
}}
"""


class Rehearsal:
    def __init__(self, output: Path, nginx: str, stack: ExitStack) -> None:
        self.output, self.nginx, self.stack = output, nginx, stack
        self.environment = {
            k: v for k, v in os.environ.items() if not k.startswith(("MODERA_", "HF_"))
        }
        self.environment.update(
            MODERA_API_KEY=secrets.token_urlsafe(32),
            MODERA_MODEL_FILE=str(output / "models.json"),
            MODERA_POLICY_FILE=str(ROOT / "deploy/reference/policies.json"),
            MODERA_MAX_IN_FLIGHT_REQUESTS="1",
            MODERA_INFERENCE_THREADS="1",
            MODERA_INFERENCE_TIMEOUT_SECONDS="60",
            HF_HUB_OFFLINE="1",
            HF_HUB_DISABLE_TELEMETRY="1",
        )
        self.counter, self.port = 0, unused_port()
        self.base = f"http://127.0.0.1:{self.port}"
        self.instances: dict[str, tuple[subprocess.Popen[str], int, Path]] = {}
        self.gateway: subprocess.Popen[str] | None = None
        self.http = stack.enter_context(httpx.Client(trust_env=False, timeout=1))
        self.report: dict[str, Any] = {
            "status": "failed",
            "platform": platform.platform(),
            "logical_cpus": os.cpu_count(),
            "gateway_mode": "master_workers",
            "python_version": platform.python_version(),
            "checks": [],
            "scope": (
                "Linux process rehearsal, not target-platform, "
                "accuracy or throughput certification"
            ),
        }

    def run(self, args: list[str], name: str, timeout: float = 300) -> None:
        with (self.output / name).open("w") as log:
            subprocess.run(
                args,
                cwd=ROOT,
                env=self.environment,
                stdout=log,
                stderr=log,
                text=True,
                check=True,
                timeout=timeout,
            )

    def spawn(
        self, args: list[str], name: str, pass_fds: tuple[int, ...] = ()
    ) -> subprocess.Popen[str]:
        log = self.stack.enter_context((self.output / name).open("w"))
        process = subprocess.Popen(
            args,
            cwd=ROOT,
            env=self.environment,
            stdout=log,
            stderr=log,
            text=True,
            pass_fds=pass_fds,
            start_new_session=True,
        )

        def cleanup() -> None:
            with suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGKILL)
            process.wait(timeout=5)

        self.stack.callback(cleanup)
        return process

    def start(self, name: str, *, stubborn_exit: bool = False) -> None:
        control = self.output / name
        control.mkdir(exist_ok=True)
        for marker in ("armed", "entered", "released"):
            (control / marker).unlink(missing_ok=True)
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            port = int(listener.getsockname()[1])
            process = self.spawn(
                [
                    sys.executable,
                    str(ROOT / "deploy/rehearsal/bootstrap.py"),
                    "--fd",
                    str(listener.fileno()),
                    "--control",
                    str(control),
                    *(["--stubborn-exit"] if stubborn_exit else []),
                ],
                f"{name}-{self.counter}.log",
                (listener.fileno(),),
            )
        self.counter += 1
        self.instances[name] = process, port, control

        def ready() -> bool:
            require(process.poll() is None, f"instance {name} exited during startup")
            try:
                return (
                    self.http.get(f"http://127.0.0.1:{port}/health/ready").status_code
                    == 200
                )
            except httpx.HTTPError:
                return False

        until(ready, f"instance {name} readiness timed out", 180)

    def route(self, names: list[str]) -> None:
        generation = self.counter
        config = self.output / "nginx.conf"
        config.write_text(
            gateway_config(
                self.output,
                self.port,
                [self.instances[n][1] for n in names],
                generation,
            )
        )
        args = [self.nginx, "-p", str(self.output) + "/", "-c", str(config)]
        self.run([*args, "-t"], f"config-{generation}.log", 10)
        self.counter += 1
        if self.gateway is None:
            self.gateway = self.spawn(args, "gateway-process.log")
        else:
            self.gateway.send_signal(signal.SIGHUP)
        gateway = self.gateway

        def routed() -> bool:
            require(gateway.poll() is None, "gateway exited")
            try:
                response = self.http.get(self.base + "/health/ready")
                return response.status_code == 200 and response.headers.get(
                    "X-Rehearsal-Generation"
                ) == str(generation)
            except httpx.HTTPError:
                return False

        until(routed, "new gateway configuration was not acknowledged")

    def submit(self, image: bool = False, attempts: int = 1) -> Request:
        self.counter += 1
        result, trace = [
            self.output / f"request-{self.counter}.{suffix}"
            for suffix in ("json", "jsonl")
        ]
        stdout = self.stack.enter_context(result.open("w"))
        stderr = self.stack.enter_context(trace.open("w"))
        args = [
            str(self.output / "go-rehearse"),
            "--base-url",
            self.base,
            "--attempts",
            str(attempts),
        ]
        if image:
            args += ["--image", str(self.output / "photo.png")]
        process = subprocess.Popen(
            args,
            cwd=ROOT,
            env=self.environment,
            stdout=stdout,
            stderr=stderr,
            text=True,
        )

        def cleanup() -> None:
            if process.poll() is None:
                process.kill()
            process.wait(timeout=5)

        self.stack.callback(cleanup)
        return process, result, trace

    def finish(self, request: Request) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        process, result, trace = request
        process.wait(timeout=20)
        require(process.returncode == 0, "Go probe failed; inspect request logs")
        return json.loads(result.read_text()), [
            json.loads(line) for line in trace.read_text().splitlines()
        ]

    def success(
        self, request: Request, kind: str
    ) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        output, trace = self.finish(request)
        result = output["result"]
        require(
            result is not None
            and not output.get("failure")
            and not output.get("api_error"),
            "no trustworthy decision",
        )
        model = json.loads((self.output / "models.json").read_text())[kind]
        require(
            result["input_type"] == kind and not result["fallback"],
            "modality/fallback mismatch",
        )
        require(
            result["models"] == [{"id": model["id"], "revision": model["revision"]}],
            "model provenance mismatch",
        )
        require(
            result["policy"] == {"id": "default", "version": "reference-1"},
            "policy mismatch",
        )
        return result, trace

    def arm(self) -> Path:
        control = self.instances["a"][2]
        for marker in ("entered", "released"):
            (control / marker).unlink(missing_ok=True)
        (control / "armed").touch()
        return control

    def active(self, control: Path, request: Request) -> None:
        def entered() -> bool:
            require(request[0].poll() is None, "request exited before gate")
            return (control / "entered").exists()

        until(entered, "request did not enter real inference")

    def check(self, name: str, **evidence: Any) -> None:
        self.report["checks"].append({"name": name, **evidence})
        print(name + ": passed", flush=True)

    def execute(self, cache: Path, download: bool) -> None:
        models = json.loads((ROOT / "deploy/reference/models.json").read_text())
        for model in models.values():
            model["cache_dir"] = str(cache.resolve())
        (self.output / "models.json").write_text(json.dumps(models, indent=2) + "\n")
        self.report.update(
            models=models,
            commit=subprocess.check_output(
                ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True
            ).strip(),
        )
        self.report["go_version"] = subprocess.check_output(
            ["go", "version"], text=True
        ).strip()
        self.report["nginx_version"] = subprocess.run(
            [self.nginx, "-v"], capture_output=True, text=True, check=True
        ).stderr.strip()
        if download:
            self.environment.pop("HF_HUB_OFFLINE")
            try:
                self.run(
                    [
                        sys.executable,
                        "-m",
                        "modera.prepare_models",
                        "--config",
                        str(self.output / "models.json"),
                    ],
                    "prepare.log",
                    900,
                )
            finally:
                self.environment["HF_HUB_OFFLINE"] = "1"
        self.run(
            [
                "go",
                "-C",
                str(ROOT / "clients/go"),
                "build",
                "-o",
                str(self.output / "go-rehearse"),
                "./cmd/rehearse",
            ],
            "go-build.log",
        )
        with Image.new("RGB", (32, 32), "white") as photo:
            photo.save(self.output / "photo.png")
        self.start("a")
        self.start("b")
        self.route(["a", "b"])
        seen = set()
        baseline = None
        for _ in range(4):
            baseline, trace = self.success(self.submit(image=True), "image")
            seen.add(trace[0]["upstream"])
        require(len(seen) == 2, "both instances were not used")
        self.success(self.submit(), "text")
        self.check("real_models_and_round_robin", upstreams=sorted(seen))

        self.route(["a"])
        control = self.arm()
        pending = self.submit(image=True)
        self.active(control, pending)
        overload, trace = self.finish(self.submit())
        require(
            overload["result"] is None
            and overload["api_error"]["Code"] == "service_busy",
            "overload invented a decision",
        )
        require(
            overload["api_error"]["Attempts"] == 1
            and len(trace) == 1
            and trace[0]["status"] == 503
            and trace[0]["retry_after"] == "1",
            "overload contract mismatch",
        )
        retry = self.submit(attempts=3)
        until(
            lambda: '"status":503' in retry[2].read_text(),
            "retry did not observe overload",
        )
        (control / "released").touch()
        self.success(pending, "image")
        _, trace = self.success(retry, "text")
        require(
            len(trace) == 2 and [event["status"] for event in trace] == [503, 200],
            "unexpected SDK recovery",
        )
        require(
            trace[1]["started"] - trace[0]["received"] >= 1
            and trace[0]["request_id"] != trace[1]["request_id"],
            "retry delay/identity mismatch",
        )
        self.check("bounded_overload_and_sdk_retry", attempts=trace)

        control = self.arm()
        pending = self.submit(image=True)
        self.active(control, pending)
        self.route(["b"])
        for _ in range(5):
            _, trace = self.success(self.submit(), "text")
            require(
                trace[0]["upstream"] == f"127.0.0.1:{self.instances['b'][1]}",
                "removed instance received new work",
            )
        process = self.instances["a"][0]
        process.terminate()
        time.sleep(0.25)
        require(
            process.poll() is None and pending[0].poll() is None,
            "active request failed to drain",
        )
        (control / "released").touch()
        self.success(pending, "image")
        process.wait(timeout=5)
        require(
            process.returncode in (0, -signal.SIGTERM), "graceful termination failed"
        )
        self.success(self.submit(), "text")
        self.check(
            "remove_before_termination_and_drain",
            fresh_requests_to_remaining_instance=5,
        )

        self.start("a", stubborn_exit=True)
        self.route(["a"])
        restarted, _ = self.success(self.submit(image=True), "image")
        require(
            baseline is not None
            and restarted["findings"] == baseline["findings"]
            and restarted["request_id"] != baseline["request_id"],
            "restart changed findings or reused request ID",
        )
        self.check("restart_and_rejoin")

        control = self.arm()
        pending = self.submit(image=True)
        self.active(control, pending)
        self.route(["b"])
        process = self.instances["a"][0]
        started = time.monotonic()
        process.terminate()
        try:
            process.wait(timeout=4)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)
        elapsed = time.monotonic() - started
        require(
            process.returncode == -signal.SIGKILL and elapsed < 6,
            "final termination deadline was not enforced",
        )
        failed, _ = self.finish(pending)
        require(
            failed["result"] is None
            and failed.get("failure") in {"protocol", "transport", "deadline"},
            "interrupted request invented a decision",
        )
        self.success(self.submit(), "text")
        self.check(
            "forced_termination_deadline",
            seconds=elapsed,
            injected_fault="ignore_reraised_term_with_blocked_native_thread",
            exit_code=process.returncode,
            interrupted_request=failed,
        )

        gateway = self.gateway
        if gateway is None:
            raise RuntimeError("missing gateway")
        gateway.send_signal(signal.SIGQUIT)
        gateway.wait(timeout=12)
        require(gateway.returncode == 0, "gateway shutdown failed")
        remaining = self.instances["b"][0]
        remaining.terminate()
        remaining.wait(timeout=5)
        require(
            remaining.returncode in (0, -signal.SIGTERM),
            "remaining instance shutdown failed",
        )
        self.check("clean_shutdown")
        self.report["status"] = "passed"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--cache-dir", type=Path, required=True)
    parser.add_argument("--nginx", default="nginx")
    parser.add_argument("--download", action="store_true")
    args = parser.parse_args()
    output = args.output.resolve()
    require(
        not any(char in str(output) for char in "$\n\r"),
        "unsupported output path characters",
    )
    output.mkdir(parents=True, exist_ok=False)
    with ExitStack() as stack:
        rehearsal = Rehearsal(output, args.nginx, stack)
        try:
            rehearsal.execute(args.cache_dir, args.download)
        finally:
            (output / "result.json").write_text(
                json.dumps(rehearsal.report, indent=2) + "\n"
            )


if __name__ == "__main__":
    main()
