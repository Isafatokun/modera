import asyncio
from io import BytesIO
from struct import pack
from threading import Event
from zlib import crc32

import pytest
from PIL import Image, features

from modera import images
from modera.errors import APIError
from modera.images import ImageValidator, validate_image


def encoded_image(format: str, animated: bool = False) -> bytes:
    with BytesIO() as buffer, Image.new("RGB", (8, 8), "red") as first:
        if animated:
            with Image.new("RGB", (8, 8), "blue") as second:
                first.save(
                    buffer, format=format, save_all=True, append_images=[second], duration=100
                )
        else:
            first.save(buffer, format=format)
        return buffer.getvalue()


@pytest.mark.parametrize("format", ["JPEG", "PNG", "WEBP"])
def test_supported_images_are_fully_decoded(format: str) -> None:
    validate_image(encoded_image(format))


@pytest.mark.parametrize("format", ["GIF", "BMP", "TIFF"])
def test_other_formats_are_rejected(format: str) -> None:
    with pytest.raises(APIError) as failure:
        validate_image(encoded_image(format))
    assert failure.value.status_code == 415
    assert failure.value.code == "unsupported_image_format"


@pytest.mark.parametrize("format", ["PNG", "WEBP"])
def test_animated_images_are_not_partially_validated(format: str) -> None:
    with pytest.raises(APIError) as failure:
        validate_image(encoded_image(format, animated=True))
    assert failure.value.code == "animated_image_not_supported"


@pytest.mark.parametrize(
    "data", [b"\x89PNG\r\n\x1a\n", b"\xff\xd8\xffbroken", b"RIFF\x04\0\0\0WEBP"]
)
def test_corrupt_supported_images_are_rejected(data: bytes) -> None:
    with pytest.raises(APIError) as failure:
        validate_image(data)
    assert failure.value.status_code == 422
    assert failure.value.code == "invalid_image"


def test_header_recognition_is_not_enough() -> None:
    data = encoded_image("JPEG")[:-20]
    with pytest.raises(APIError) as failure:
        validate_image(data)
    assert failure.value.code == "invalid_image"


def test_pixel_limit_is_checked_before_decoding(
    monkeypatch: pytest.MonkeyPatch, image_bytes: bytes
) -> None:
    monkeypatch.setattr(images, "MAX_IMAGE_PIXELS", 3)

    def unexpected_load(image: Image.Image) -> None:
        raise AssertionError("oversized image must not be decoded")

    monkeypatch.setattr(Image.Image, "load", unexpected_load)
    with pytest.raises(APIError) as failure:
        validate_image(image_bytes)
    assert failure.value.status_code == 413
    assert failure.value.code == "image_too_large"


def test_pixel_limit_is_inclusive(monkeypatch: pytest.MonkeyPatch, image_bytes: bytes) -> None:
    monkeypatch.setattr(images, "MAX_IMAGE_PIXELS", 4)
    validate_image(image_bytes)


@pytest.mark.parametrize("width,height", [(10_000, 10_000), (100_000, 100_000)])
@pytest.mark.filterwarnings("error::PIL.Image.DecompressionBombWarning")
def test_decompression_bomb_headers_are_rejected_without_allocation(
    image_bytes: bytes, width: int, height: int
) -> None:
    data = bytearray(image_bytes)
    data[16:24] = pack(">II", width, height)
    data[29:33] = pack(">I", crc32(data[12:29]))
    with pytest.raises(APIError) as failure:
        validate_image(bytes(data))
    assert failure.value.code == "image_too_large"


def test_errors_release_capacity(image_bytes: bytes) -> None:
    async def scenario() -> None:
        pool = ImageValidator()
        try:
            with pytest.raises(APIError):
                await pool.validate(b"not an image")
            await pool.validate(image_bytes)
        finally:
            pool.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("cancel", [False, True])
def test_late_native_work_keeps_capacity(monkeypatch: pytest.MonkeyPatch, cancel: bool) -> None:
    started, finish = Event(), Event()

    def blocked(data: bytes) -> None:
        started.set()
        if not finish.wait(5):
            raise RuntimeError("test worker was not released")

    monkeypatch.setattr(images, "validate_image", blocked)
    monkeypatch.setattr(images, "IMAGE_VALIDATION_SECONDS", 2 if cancel else 0.05)

    async def scenario() -> None:
        pool = ImageValidator()
        task = asyncio.create_task(pool.validate(b"data"))
        try:
            assert await asyncio.to_thread(started.wait, 2)
            if cancel:
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
            else:
                with pytest.raises(APIError) as failure:
                    await task
                assert failure.value.code == "image_processing_timeout"
            with pytest.raises(APIError) as failure:
                await pool.validate(b"data")
            assert failure.value.code == "service_busy"
            assert failure.value.retryable is True
            finish.set()
            for _ in range(100):
                try:
                    await pool.validate(b"data")
                    break
                except APIError as exc:
                    assert exc.code == "service_busy"
                    await asyncio.sleep(0.005)
            else:
                raise AssertionError("image capacity did not recover")
        finally:
            finish.set()
            pool.close()

    asyncio.run(scenario())


def test_shutdown_refuses_new_work() -> None:
    pool = ImageValidator()
    pool.close()
    pool.close()

    async def scenario() -> None:
        with pytest.raises(APIError) as failure:
            await pool.validate(b"data")
        assert failure.value.code == "service_busy"

    asyncio.run(scenario())


def test_missing_codecs_fail_startup(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(features, "check", lambda codec: False)
    with pytest.raises(RuntimeError, match="required image codecs"):
        ImageValidator()
