import asyncio
import warnings
from concurrent.futures import Future, ThreadPoolExecutor
from io import BytesIO
from threading import Lock

from PIL import Image, features

from modera.errors import APIError
from modera.schemas import MAX_IMAGE_BYTES

MAX_IMAGE_PIXELS = 16_000_000
IMAGE_VALIDATION_SECONDS = 5
FORMATS = ("JPEG", "PNG", "WEBP")
warnings.filterwarnings("error", category=Image.DecompressionBombWarning)


def validate_image(data: bytes) -> None:
    if len(data) > MAX_IMAGE_BYTES:
        raise APIError(413, "payload_too_large", "The image file is too large.", param="file")
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
                    413, "image_too_large", "The image exceeds the pixel limit.", param="file"
                )
            if getattr(image, "n_frames", 1) != 1:
                raise APIError(
                    415,
                    "animated_image_not_supported",
                    "Animated images are not supported.",
                    param="file",
                )
            image.verify()
        with Image.open(BytesIO(data), formats=FORMATS) as image:
            image.load()
    except (Image.DecompressionBombError, Image.DecompressionBombWarning) as exc:
        raise APIError(
            413, "image_too_large", "The image exceeds the pixel limit.", param="file"
        ) from exc
    except (OSError, ValueError, SyntaxError, EOFError) as exc:
        raise APIError(
            422, "invalid_image", "The image could not be decoded.", param="file"
        ) from exc


class ImageValidator:
    def __init__(self) -> None:
        if not all(features.check(codec) for codec in ("jpg", "zlib", "webp")):
            raise RuntimeError("required image codecs are unavailable")
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="modera-image")
        self._lock = Lock()
        self._busy = False
        self._closed = False

    def _release(self, _future: Future[None]) -> None:
        with self._lock:
            self._busy = False

    async def validate(self, data: bytes) -> None:
        with self._lock:
            if self._closed or self._busy:
                raise APIError(
                    503,
                    "service_busy",
                    "Image validation is temporarily unavailable.",
                    retryable=True,
                    headers={"Retry-After": "1"},
                )
            self._busy = True
            try:
                future = self._executor.submit(validate_image, data)
            except BaseException:
                self._busy = False
                raise
        future.add_done_callback(self._release)
        try:
            async with asyncio.timeout(IMAGE_VALIDATION_SECONDS):
                await asyncio.wrap_future(future)
        except TimeoutError as exc:
            raise APIError(
                503,
                "image_processing_timeout",
                "Image validation exceeded its deadline.",
                retryable=True,
                headers={"Retry-After": "1"},
            ) from exc

    def close(self) -> None:
        with self._lock:
            self._closed = True
        self._executor.shutdown(wait=False, cancel_futures=True)
