import asyncio
import warnings
from collections.abc import Callable
from contextlib import closing
from io import BytesIO

from PIL import Image, features

from modera.errors import APIError
from modera.schemas import MAX_IMAGE_BYTES
from modera.workers import BoundedWorker

MAX_IMAGE_PIXELS = 16_000_000
IMAGE_VALIDATION_SECONDS = 5
FORMATS = ("JPEG", "PNG", "WEBP")
warnings.filterwarnings("error", category=Image.DecompressionBombWarning)


def decode_image(data: bytes) -> Image.Image:
    if len(data) > MAX_IMAGE_BYTES:
        raise APIError(
            413, "payload_too_large", "The image file is too large.", param="file"
        )
    recognized = (
        data.startswith(b"\xff\xd8\xff")
        or data.startswith(b"\x89PNG\r\n\x1a\n")
        or (data.startswith(b"RIFF") and data[8:12] == b"WEBP")
    )
    if not recognized:
        raise APIError(
            415,
            "unsupported_image_format",
            "Supported image formats are JPEG, PNG, and WebP.",
            param="file",
        )
    try:
        with Image.open(BytesIO(data), formats=FORMATS) as image:
            if image.width * image.height > MAX_IMAGE_PIXELS:
                raise APIError(
                    413,
                    "image_too_large",
                    "The image exceeds the pixel limit.",
                    param="file",
                )
            if getattr(image, "n_frames", 1) != 1:
                raise APIError(
                    415,
                    "animated_image_not_supported",
                    "Animated images are not supported.",
                    param="file",
                )
            image.verify()
        image = Image.open(BytesIO(data), formats=FORMATS)
        try:
            image.load()
            return image
        except BaseException:
            image.close()
            raise
    except (Image.DecompressionBombError, Image.DecompressionBombWarning) as exc:
        raise APIError(
            413, "image_too_large", "The image exceeds the pixel limit.", param="file"
        ) from exc
    except (OSError, ValueError, SyntaxError, EOFError) as exc:
        raise APIError(
            422, "invalid_image", "The image could not be decoded.", param="file"
        ) from exc


def validate_image(data: bytes) -> None:
    with closing(decode_image(data)):
        pass


class ImageValidator:
    def __init__(self, queue_size: int = 0, wait_seconds: float = 2) -> None:
        if not all(features.check(codec) for codec in ("jpg", "zlib", "webp")):
            raise RuntimeError("required image codecs are unavailable")
        self._worker = BoundedWorker("modera-image", queue_size, wait_seconds)

    async def _run[T](self, operation: Callable[[], T]) -> T:
        try:
            async with asyncio.timeout(IMAGE_VALIDATION_SECONDS):
                return await self._worker.run(operation)
        except TimeoutError as exc:
            raise APIError(
                503,
                "image_processing_timeout",
                "Image processing exceeded its deadline.",
                retryable=True,
                headers={"Retry-After": "1"},
            ) from exc
        except APIError:
            raise
        except Exception as exc:
            raise APIError(
                503,
                "check_unavailable",
                "Image preparation did not complete.",
                retryable=True,
                headers={"Retry-After": "1"},
            ) from exc

    async def validate(self, data: bytes) -> None:
        await self._run(lambda: validate_image(data))

    async def prepare[T](self, data: bytes, operation: Callable[[Image.Image], T]) -> T:
        def prepared() -> T:
            with closing(decode_image(data)) as image:
                return operation(image)

        return await self._run(prepared)

    def close(self) -> None:
        self._worker.close()
