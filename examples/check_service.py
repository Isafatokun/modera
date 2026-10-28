"""Check a running reference deployment without submitting private media."""

import json
import os
import time
import urllib.error
import urllib.request
from io import BytesIO
from typing import Any

from PIL import Image


def check() -> None:
    base = os.environ.get("MODERA_CHECK_BASE_URL", "http://127.0.0.1:8000").rstrip("/")
    key = os.environ["MODERA_API_KEY"]
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def request(
        path: str,
        body: bytes | None = None,
        content_type: str = "application/json",
        *,
        authenticated: bool = True,
    ) -> tuple[int, dict[str, Any], Any]:
        headers = {"Content-Type": content_type}
        if authenticated:
            headers["Authorization"] = "Bearer " + key
        req = urllib.request.Request(base + path, data=body, headers=headers)
        try:
            response = opener.open(req, timeout=30)
        except urllib.error.HTTPError as error:
            response = error
        with response:
            content = response.read()
            data = json.loads(content) if path != "/metrics" else {}
            return response.status, data, response.headers

    deadline = time.monotonic() + 60
    while True:
        try:
            if request("/health/ready")[0] == 200:
                break
        except (urllib.error.URLError, TimeoutError, ConnectionError):
            pass
        if time.monotonic() >= deadline:
            raise RuntimeError("Service did not become ready")
        time.sleep(0.2)

    text = json.dumps(
        {"input": {"type": "text", "text": "Hello, have a nice day."}}
    ).encode()
    assert request("/v1/moderations", text, authenticated=False)[0] == 401

    def decision(body: bytes, content_type: str = "application/json") -> None:
        status, result, headers = request("/v1/moderations", body, content_type)
        assert status == 200, (
            f"Moderation failed: {status} {result.get('error', {}).get('code')}"
        )
        assert result["decision"]["action"] in ("allow", "review", "block")
        assert result["models"] and not result["fallback"]
        assert result["request_id"] == headers["X-Request-ID"]

    decision(text)
    print("text: passed", flush=True)
    with BytesIO() as buffer, Image.new("RGB", (32, 32), "white") as image:
        image.save(buffer, format="PNG")
        photo = buffer.getvalue()
    multipart = (
        b'--modera-check\r\nContent-Disposition: form-data; name="file"; '
        b'filename="photo.png"\r\n'
        b"Content-Type: image/png\r\n\r\n" + photo + b"\r\n--modera-check--\r\n"
    )
    decision(multipart, "multipart/form-data; boundary=modera-check")
    print("photo file: passed", flush=True)
    url = os.environ.get("MODERA_CHECK_IMAGE_URL")
    if url:
        decision(
            json.dumps(
                {"input": {"type": "image", "source": {"type": "url", "url": url}}}
            ).encode()
        )
        print("photo URL: passed", flush=True)
    else:
        print("photo URL: skipped (set MODERA_CHECK_IMAGE_URL)", flush=True)
    unavailable = os.environ.get("MODERA_CHECK_UNAVAILABLE_URL")
    if unavailable:
        status, result, headers = request(
            "/v1/moderations",
            json.dumps(
                {
                    "input": {
                        "type": "image",
                        "source": {"type": "url", "url": unavailable},
                    }
                }
            ).encode(),
        )
        assert status == 503 and result["error"]["retryable"]
        assert result["error"]["code"] == "input_fetch_failed"
        assert headers.get("Retry-After") and "decision" not in result
        print("unavailable source: retryable error without a decision", flush=True)
    assert request("/metrics")[0] == 200
    print("health, authentication and metrics: passed", flush=True)


if __name__ == "__main__":
    check()
