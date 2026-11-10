"""Check a running reference deployment without submitting private media."""

import json
import os
import time
import urllib.error
import urllib.request
from io import BytesIO
from typing import Any

from PIL import Image

BOUNDARY = "modera-check"


class Service:
    def __init__(self, base: str, key: str) -> None:
        self.base = base.rstrip("/")
        self.key = key
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def request(
        self,
        path: str,
        body: bytes | None = None,
        content_type: str = "application/json",
        *,
        authenticated: bool = True,
    ) -> tuple[int, dict[str, Any], Any]:
        headers = {"Content-Type": content_type}
        if authenticated:
            headers["Authorization"] = "Bearer " + self.key
        req = urllib.request.Request(self.base + path, data=body, headers=headers)
        try:
            response = self.opener.open(req, timeout=30)
        except urllib.error.HTTPError as error:
            response = error
        with response:
            content = response.read()
            data = json.loads(content) if path != "/metrics" else {}
            return response.status, data, response.headers

    def wait_until_ready(self, seconds: float = 60) -> None:
        deadline = time.monotonic() + seconds
        while True:
            try:
                if self.request("/health/ready")[0] == 200:
                    return
            except (urllib.error.URLError, TimeoutError, ConnectionError):
                pass
            if time.monotonic() >= deadline:
                raise RuntimeError("Service did not become ready")
            time.sleep(0.2)

    def expect_decision(
        self, body: bytes, content_type: str = "application/json"
    ) -> None:
        status, result, headers = self.request("/v1/moderations", body, content_type)
        assert status == 200, (
            f"Moderation failed: {status} {result.get('error', {}).get('code')}"
        )
        assert result["decision"]["action"] in ("allow", "review", "block")
        assert result["models"] and not result["fallback"]
        assert result["request_id"] == headers["X-Request-ID"]


def url_input(url: str) -> bytes:
    source = {"type": "url", "url": url}
    return json.dumps({"input": {"type": "image", "source": source}}).encode()


def white_photo_upload() -> bytes:
    with BytesIO() as buffer, Image.new("RGB", (32, 32), "white") as image:
        image.save(buffer, format="PNG")
        photo = buffer.getvalue()
    return (
        f'--{BOUNDARY}\r\nContent-Disposition: form-data; name="file"; '
        f'filename="photo.png"\r\n'
        f"Content-Type: image/png\r\n\r\n".encode()
        + photo
        + f"\r\n--{BOUNDARY}--\r\n".encode()
    )


def check_text(service: Service) -> None:
    text = json.dumps(
        {"input": {"type": "text", "text": "Hello, have a nice day."}}
    ).encode()
    assert service.request("/v1/moderations", text, authenticated=False)[0] == 401
    service.expect_decision(text)
    print("text: passed", flush=True)


def check_photo_file(service: Service) -> None:
    service.expect_decision(
        white_photo_upload(), f"multipart/form-data; boundary={BOUNDARY}"
    )
    print("photo file: passed", flush=True)


def check_photo_url(service: Service) -> None:
    url = os.environ.get("MODERA_CHECK_IMAGE_URL")
    if not url:
        print("photo URL: skipped (set MODERA_CHECK_IMAGE_URL)", flush=True)
        return
    service.expect_decision(url_input(url))
    print("photo URL: passed", flush=True)


def check_unavailable_source(service: Service) -> None:
    unavailable = os.environ.get("MODERA_CHECK_UNAVAILABLE_URL")
    if not unavailable:
        return
    status, result, headers = service.request("/v1/moderations", url_input(unavailable))
    assert status == 503 and result["error"]["retryable"]
    assert result["error"]["code"] == "input_fetch_failed"
    assert headers.get("Retry-After") and "decision" not in result
    print("unavailable source: retryable error without a decision", flush=True)


def check() -> None:
    service = Service(
        os.environ.get("MODERA_CHECK_BASE_URL", "http://127.0.0.1:8000"),
        os.environ["MODERA_API_KEY"],
    )
    service.wait_until_ready()
    check_text(service)
    check_photo_file(service)
    check_photo_url(service)
    check_unavailable_source(service)
    assert service.request("/metrics")[0] == 200
    print("health, authentication and metrics: passed", flush=True)


if __name__ == "__main__":
    check()
