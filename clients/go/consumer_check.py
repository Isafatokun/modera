"""Install an exact public SDK revision into a fresh consumer and compile its API."""

import argparse
import json
import os
import re
import subprocess
import tempfile
from pathlib import Path

MODULE = "github.com/Isafatokun/modera/clients/go"
SOURCE = """package main

import (
    "context"
    "errors"
    "strings"
    "time"
    modera "github.com/Isafatokun/modera/clients/go"
)

func main() {
    c, err := modera.New("https://moderation.invalid", strings.Repeat("x", 32),
        modera.Options{MaxAttempts: 1, TotalTimeout: time.Second})
    if err != nil { panic(err) }
    type textCall func(context.Context, string, string) (*modera.Result, error)
    var text textCall = c.ModerateText
    var url textCall = c.ModerateImageURL
    type imageCall func(context.Context, []byte, string) (*modera.Result, error)
    var image imageCall = c.ModerateImage
    _, _, _ = text, url, image
    var api *modera.APIError
    var protocol *modera.ProtocolError
    _ = errors.As(errors.New("fixture"), &api)
    _ = errors.As(errors.New("fixture"), &protocol)
}
"""


def check(revision: str) -> None:
    if not re.fullmatch(r"[0-9a-f]{40}", revision):
        raise ValueError("revision must be a full lowercase Git commit SHA")
    environment = {k: v for k, v in os.environ.items() if not k.startswith("MODERA_")}
    environment.update(GOWORK="off", GOFLAGS="")
    with tempfile.TemporaryDirectory(prefix="modera-consumer-") as temporary:
        directory = Path(temporary)

        def go(*args: str) -> str:
            return subprocess.check_output(
                ["go", *args],
                cwd=directory,
                env=environment,
                text=True,
                timeout=180,
            )

        go("mod", "init", "example.invalid/modera-consumer")
        go("get", MODULE + "@" + revision)
        module = json.loads(go("list", "-m", "-json", MODULE))
        if module["Path"] != MODULE or "Replace" in module:
            raise RuntimeError("consumer resolved a replacement or unexpected module")
        (directory / "main.go").write_text(SOURCE)
        go("run", ".")  # Constructs a client; makes no network moderation request.
        go("mod", "verify")
        print(
            json.dumps(
                {
                    "status": "passed",
                    "module": MODULE,
                    "requested_commit": revision,
                    "resolved_version": module["Version"],
                    "go_version": go("version").strip(),
                    "scope": "external module resolution and API compilation",
                }
            )
        )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--revision", required=True)
    check(parser.parse_args().revision)
