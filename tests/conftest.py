from io import BytesIO

import pytest
from PIL import Image

TEST_API_KEY = "test-only-key-for-modera-not-a-deployment-secret"


@pytest.fixture(autouse=True)
def configure_test_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MODERA_API_KEY", TEST_API_KEY)


@pytest.fixture
def auth_headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {TEST_API_KEY}"}


@pytest.fixture
def image_bytes() -> bytes:
    with BytesIO() as buffer, Image.new("RGB", (2, 2), "white") as image:
        image.save(buffer, format="PNG")
        return buffer.getvalue()
