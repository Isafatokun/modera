"""Local HTTP capacity experiment; no accuracy evaluation or retries."""

import argparse
import asyncio
import json
import math
import os
import platform
import secrets
import socket
import subprocess
import sys
import time
from collections import Counter
from datetime import UTC, datetime
from importlib.metadata import version
from pathlib import Path
from typing import Any
from uuid import uuid4

import httpx2 as httpx
import numpy as np
from PIL import Image

ROOT = Path(__file__).resolve().parent


def latency(values: list[float]) -> dict[str, float] | None:
    values = sorted(values)
    if not values:
        return None
    return {
        key: round(values[max(0, math.ceil(len(values) * q) - 1)] * 1000, 2)
        for key, q in [("p50_ms", 0.5), ("p95_ms", 0.95), ("p99_ms", 0.99)]
    }


def memory(output: Path) -> dict[str, float] | None:
    try:
        data: dict[str, float] = json.loads((output / "memory.json").read_text())
        return data
    except FileNotFoundError:
        return None


def read_optional(path: str) -> str | None:
    try:
        return Path(path).read_text().strip()
    except OSError:
        return None


def duration(value: str) -> float:
    seconds = float(value)
    if not math.isfinite(seconds) or not 0 < seconds <= 3600:
        raise argparse.ArgumentTypeError("seconds must be between 0 and 3600")
    return seconds


async def experiment(args: argparse.Namespace) -> None:
    repo = ROOT.parent
    output: Path = args.output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    for name, dimensions, quality in [
        ("photo", (2048, 1536), 85),
        ("large_photo", (4096, 3072), 70),
    ]:
        path = output / f"{name}.jpg"
        pixels = np.random.default_rng(42).integers(
            0, 256, (*dimensions[::-1], 3), dtype=np.uint8
        )
        Image.fromarray(pixels).save(path, quality=quality)
    headers = {"Authorization": "Bearer " + secrets.token_urlsafe(32)}
    environment = {k: v for k, v in os.environ.items() if not k.startswith("MODERA_")}
    environment.update(
        {
            "MODERA_API_KEY": headers["Authorization"][7:],
            "MODERA_POLICY_FILE": str(repo / "examples/policies.json"),
            "MODERA_TEXT_MODEL__ID": "Horizon-Labs/multilingual-toxicity-small",
            "MODERA_TEXT_MODEL__REVISION": "01adcc628a33848569816d4851a38544c321b92c",
            "MODERA_TEXT_MODEL__LABEL_MAP": '{"toxicity":"toxicity"}',
            "MODERA_TEXT_MODEL__CACHE_DIR": str(args.cache.resolve()),
            "MODERA_IMAGE_MODEL__ID": "AdamCodd/vit-base-nsfw-detector",
            "MODERA_IMAGE_MODEL__REVISION": "8587de998f441aac03fdd57a85d2e4cb808c7d64",
            "MODERA_IMAGE_MODEL__LABEL_MAP": '{"nsfw":"sexual"}',
            "MODERA_IMAGE_MODEL__CACHE_DIR": str(args.cache.resolve()),
            "MODERA_INFERENCE_THREADS": "2",
            "MODERA_MAX_IN_FLIGHT_REQUESTS": "8",
            "HF_HUB_DISABLE_TELEMETRY": "1",
            "ORT_DISABLE_TELEMETRY": "1",
            "HF_HUB_OFFLINE": "1",
            "TOKENIZERS_PARALLELISM": "false",
        }
    )
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    started = time.perf_counter()
    report: dict[str, Any] = {
        "commit": subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=repo, text=True
        ).strip(),
        "dirty": bool(
            subprocess.check_output(
                ["git", "status", "--porcelain"], cwd=repo, text=True
            ).strip()
        ),
        "python": platform.python_version(),
        "packages": {
            p: version(p)
            for p in ["torch", "transformers", "pillow", "uvicorn", "fastapi"]
        },
        "cpu": platform.processor() or platform.machine(),
        "cpu_max": read_optional("/sys/fs/cgroup/cpu.max"),
        "memory_max": read_optional("/sys/fs/cgroup/memory.max"),
        "seconds_per_scenario": args.seconds,
        "load_pattern": "closed_loop",
        "status": "running",
        "server_workers": 1,
        "inference_threads": 2,
        "http_capacity": 8,
        "fixtures": {
            n: {"bytes": (output / f"{n}.jpg").stat().st_size}
            for n in ["photo", "large_photo"]
        },
        "scenarios": [],
    }
    (output / "results.json").write_text(json.dumps(report, indent=2) + "\n")
    log = (output / "server.log").open("w")
    process: subprocess.Popen[bytes] | None = None
    try:
        process = subprocess.Popen(
            [sys.executable, str(ROOT / "server.py"), str(port), str(output)],
            cwd=repo,
            env=environment,
            stdout=log,
            stderr=log,
        )
        async with httpx.AsyncClient(
            base_url=f"http://127.0.0.1:{port}",
            headers=headers,
            trust_env=False,
            timeout=30,
            limits=httpx.Limits(max_connections=32, max_keepalive_connections=32),
        ) as client:
            for _ in range(600):
                if process.poll() is not None:
                    raise RuntimeError("Server exited; inspect server.log")
                try:
                    if (await client.get("/health/ready")).status_code == 200:
                        break
                except httpx.HTTPError:
                    pass
                await asyncio.sleep(0.1)
            else:
                raise RuntimeError("Readiness timeout")
            report["startup_seconds"] = round(time.perf_counter() - started, 2)
            report["memory_after_startup"] = memory(output)
            print(
                "Ready",
                report["startup_seconds"],
                report["memory_after_startup"],
                flush=True,
            )
            image_bodies: dict[str, bytes] = {}
            for name in ["photo", "large_photo"]:
                image_bodies[name] = (
                    b"--capacity\r\nContent-Disposition: form-data; "
                    b'name="file"; filename="photo.jpg"\r\n'
                    b"Content-Type: image/jpeg\r\n\r\n"
                    + (output / f"{name}.jpg").read_bytes()
                    + b"\r\n--capacity--\r\n"
                )
            texts = {
                "text": "Thank you for your help. I hope you have a wonderful day.",
                "long_text": (
                    "We are discussing plans for a community gathering this weekend. "
                    * 15
                ),
            }

            async def request(kind: str) -> httpx.Response:
                if kind in texts:
                    return await client.post(
                        "/v1/moderations",
                        json={"input": {"type": "text", "text": texts[kind]}},
                    )
                return await client.post(
                    "/v1/moderations",
                    content=image_bodies[kind],
                    headers={"Content-Type": "multipart/form-data; boundary=capacity"},
                )

            for kind in [*texts, *image_bodies]:
                response = await request(kind)
                result = response.json()
                fixture = report["fixtures"].setdefault(kind, {})
                fixture["warmup_status"] = response.status_code
                fixture["warmup_seconds"] = response.elapsed.total_seconds()
                if response.status_code == 200:
                    assert result["models"] and not result["fallback"]
                    fixture["provenance"] = result["models"]
                else:
                    fixture["warmup_error"] = result["error"]["code"]
                    print("Warmup failure", kind, fixture, flush=True)
                    await asyncio.sleep(15)
                if kind in texts:
                    report["fixtures"][kind]["text"] = texts[kind]

            async def run_scenario(
                kind: str, concurrency: int, rate: float | None
            ) -> None:
                successes: list[float] = []
                failures: list[float] = []
                statuses: Counter[str] = Counter()
                errors: Counter[str] = Counter()
                health: list[dict[str, float | int]] = []
                scraper: list[int] = []
                start = time.perf_counter()
                until = start + args.seconds

                async def worker(index: int) -> None:
                    ordinal = index
                    if rate:
                        await asyncio.sleep(index / rate)
                    while time.perf_counter() < until:
                        mode = (
                            ("text" if ordinal % 2 == 0 else "photo")
                            if kind == "mixed"
                            else kind
                        )
                        before = time.perf_counter()
                        try:
                            response = await request(mode)
                            elapsed = time.perf_counter() - before
                            statuses[str(response.status_code)] += 1
                            if response.status_code == 200:
                                successes.append(elapsed)
                            else:
                                failures.append(elapsed)
                                errors[
                                    response.json()
                                    .get("error", {})
                                    .get("code", "unknown")
                                ] += 1
                        except httpx.HTTPError as exc:
                            failures.append(time.perf_counter() - before)
                            errors[type(exc).__name__] += 1
                        ordinal += 1
                        if rate:
                            await asyncio.sleep(
                                max(
                                    0,
                                    concurrency / rate - (time.perf_counter() - before),
                                )
                            )

                async def monitor() -> None:
                    while time.perf_counter() < until:
                        before = time.perf_counter()
                        check = await client.get("/health/ready")
                        health.append(
                            {
                                "status": check.status_code,
                                "ms": round((time.perf_counter() - before) * 1000, 2),
                            }
                        )
                        check = await client.get("/metrics")
                        scraper.append(check.status_code)
                        await asyncio.sleep(0.25)

                async with asyncio.TaskGroup() as tasks:
                    for index in range(concurrency):
                        tasks.create_task(worker(index))
                    tasks.create_task(monitor())
                elapsed = time.perf_counter() - start
                scenario = {
                    "kind": kind,
                    "concurrency": concurrency,
                    "target_rps": rate,
                    "elapsed_seconds": round(elapsed, 3),
                    "statuses": dict(statuses),
                    "errors": dict(errors),
                    "success_rps": round(len(successes) / elapsed, 2),
                    "success_latency": latency(successes),
                    "failure_latency": latency(failures),
                    "health": health,
                    "scrape_statuses": dict(Counter(scraper)),
                    "memory": memory(output),
                }
                (output / f"samples-{kind}-{concurrency}-{rate}.json").write_text(
                    json.dumps(
                        {"success_seconds": successes, "failure_seconds": failures}
                    )
                )
                report["scenarios"].append(scenario)
                (output / "results.json").write_text(
                    json.dumps(report, indent=2) + "\n"
                )
                print(
                    json.dumps({k: v for k, v in scenario.items() if k != "health"}),
                    flush=True,
                )
                await asyncio.sleep(2)

            for kind, concurrency, rate in [
                ("text", 1, None),
                ("long_text", 1, None),
                ("photo", 1, None),
                ("large_photo", 1, None),
                ("mixed", 1, None),
                ("mixed", 4, 1),
                ("mixed", 4, 3),
                ("mixed", 4, 6),
                ("text", 8, None),
                ("photo", 8, None),
                ("mixed", 8, None),
            ]:
                await run_scenario(kind, concurrency, rate)
            response = await request("text")
            report["recovery_status"] = response.status_code
            if response.status_code != 200:
                await asyncio.sleep(15)
                response = await request("text")
                report["recovery_after_cooldown_status"] = response.status_code
            report["metrics"] = (await client.get("/metrics")).text
            report["memory_final"] = memory(output)
            report["status"] = "completed"
    finally:
        if report["status"] == "running":
            report["status"] = "interrupted"
        if process is not None and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=15)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
        log.close()
        (output / "results.json").write_text(json.dumps(report, indent=2) + "\n")
    print(f"Results: {output}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cache", type=Path, required=True)
    parser.add_argument("--seconds", type=duration, default=20)
    parser.add_argument(
        "--output",
        type=Path,
        default=ROOT
        / "results"
        / (datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid4().hex[:8]),
    )
    asyncio.run(experiment(parser.parse_args()))
