"""Local benchmark server with peak-memory sampling."""

import argparse
import json
import resource
import sys
import threading
import time
from pathlib import Path

import uvicorn


def monitor(output: Path) -> None:
    target = output / "memory.json"
    while True:
        usage = resource.getrusage(resource.RUSAGE_SELF)
        divisor = 1024 * 1024 if sys.platform == "darwin" else 1024
        temporary = target.with_suffix(".tmp")
        temporary.write_text(
            json.dumps(
                {
                    "peak_rss_mib": round(usage.ru_maxrss / divisor, 2),
                    "cpu_seconds": usage.ru_utime + usage.ru_stime,
                }
            )
        )
        temporary.replace(target)
        time.sleep(0.1)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("port", type=int)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    threading.Thread(target=monitor, args=(args.output,), daemon=True).start()
    uvicorn.run(
        "modera.main:create_app",
        factory=True,
        host="127.0.0.1",
        port=args.port,
        access_log=False,
        server_header=False,
        proxy_headers=False,
    )


if __name__ == "__main__":
    main()
