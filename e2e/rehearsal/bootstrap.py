"""Rehearsal-only inference gate. Never included in the serving image."""

import argparse
import signal
import time
from pathlib import Path

import uvicorn

from modera.inference import Classifier, PreparedImage
from modera.results import Finding


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fd", type=int, required=True)
    parser.add_argument("--control", type=Path, required=True)
    parser.add_argument("--stubborn-exit", action="store_true")
    args = parser.parse_args()
    if args.stubborn_exit:
        # Uvicorn still handles the initial TERM and drains normally. On return,
        # it restores this handler and re-raises TERM. Ignoring that final signal
        # leaves the gated native thread alive: an explicit exit fault, not the
        # production server's normal behavior.
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
    predict = Classifier.predict

    def gated(self: Classifier, content: str | bytes | PreparedImage) -> list[Finding]:
        if isinstance(content, PreparedImage) and (args.control / "armed").exists():
            (args.control / "entered").touch()
            deadline = time.monotonic() + 90
            while not (args.control / "released").exists():
                if time.monotonic() > deadline:
                    raise TimeoutError("rehearsal gate expired")
                time.sleep(0.01)
        return predict(self, content)

    Classifier.predict = gated  # type: ignore[method-assign]
    uvicorn.run(
        "modera.main:create_app",
        factory=True,
        fd=args.fd,
        workers=1,
        access_log=False,
        server_header=False,
        proxy_headers=False,
        timeout_graceful_shutdown=2,
    )


if __name__ == "__main__":
    main()
