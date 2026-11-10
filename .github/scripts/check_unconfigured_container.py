"""Check a container started with only an API key: authentication and image codecs.

Runs inside the container, which has no model or rules backend, so every
moderation request is expected to end in a typed error and never a decision.
"""

import json
import os
import urllib.error
import urllib.request
from io import BytesIO
from typing import Any

from PIL import Image

URL = "http://127.0.0.1:8000/v1/moderations"
KEY = os.environ["MODERA_API_KEY"]


def expect_error(
    request: urllib.request.Request, status: int, code: str, timeout: float
) -> dict[str, Any]:
    try:
        urllib.request.urlopen(request, timeout=timeout)
    except urllib.error.HTTPError as response:
        assert response.code == status
        body: dict[str, Any] = json.load(response)["error"]
        assert body["code"] == code
        assert body["request_id"] == response.headers["X-Request-ID"]
        return body
    raise AssertionError("expected an error response")


def check_authentication() -> None:
    for authenticated, status, code in [
        (False, 401, "authentication_required"),
        (True, 501, "moderation_not_implemented"),
    ]:
        headers = {"Content-Type": "application/json"}
        if authenticated:
            headers["Authorization"] = "Bearer " + KEY
        request = urllib.request.Request(
            URL,
            data=b'{"input":{"type":"text","text":"hello"}}',
            headers=headers,
        )
        expect_error(request, status, code, 5)
    print("Container authentication checks passed")


def check_image_codecs() -> None:
    for image_format in ("JPEG", "PNG", "WEBP"):
        with BytesIO() as buffer, Image.new("RGB", (2, 2), "white") as image:
            image.save(buffer, format=image_format)
            data = buffer.getvalue()
        body = (
            b'--smoke\r\nContent-Disposition: form-data; name="file"; '
            b'filename="image"\r\n'
            b"Content-Type: application/octet-stream\r\n\r\n"
            + data
            + b"\r\n--smoke--\r\n"
        )
        request = urllib.request.Request(
            URL,
            data=body,
            headers={
                "Authorization": "Bearer " + KEY,
                "Content-Type": "multipart/form-data; boundary=smoke",
            },
        )
        expect_error(request, 501, "moderation_not_implemented", 10)
    print("Container image codec checks passed")


if __name__ == "__main__":
    check_authentication()
    check_image_codecs()
