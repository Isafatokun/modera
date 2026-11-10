"""Check a container configured with the example text rules and policies.

Runs inside the container and expects the rules-only policy to block the
example phrase without any model involved.
"""

import json
import os
import time
import urllib.error
import urllib.request

BASE = "http://127.0.0.1:8000"


def wait_until_ready() -> None:
    for _ in range(20):
        try:
            urllib.request.urlopen(BASE + "/health/ready", timeout=2).close()
            return
        except (urllib.error.URLError, TimeoutError, ConnectionError):
            time.sleep(0.5)
    raise AssertionError("rules container did not become ready")


def check_block() -> None:
    request = urllib.request.Request(
        BASE + "/v1/moderations",
        data=json.dumps(
            {
                "input": {"type": "text", "text": "example prohibited phrase"},
                "policy": "rules-only",
            }
        ).encode(),
        headers={
            "Authorization": "Bearer " + os.environ["MODERA_API_KEY"],
            "Content-Type": "application/json",
        },
    )
    with urllib.request.urlopen(request, timeout=5) as response:
        result = json.load(response)
        assert response.status == 200
        assert result["decision"]["action"] == "block"
        assert result["models"] == []
        assert result["rules_version"] == "1"
        assert result["findings"][0]["source"] == "rules"
    print("Rules-only container checks passed")


if __name__ == "__main__":
    wait_until_ready()
    check_block()
