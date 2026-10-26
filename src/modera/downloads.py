import asyncio
import ipaddress
import random
import socket
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime

import aiohttp
from aiohttp.abc import ResolveResult
from pydantic import HttpUrl

from modera.errors import APIError
from modera.schemas import MAX_IMAGE_BYTES

DOWNLOAD_SECONDS = 20
ATTEMPT_SECONDS = 8
STALL_SECONDS = 3
MAX_ATTEMPTS = 2
MAX_DOWNLOADS = 4
URL_PARAM = "input.source.url"


def source_error(status: int, code: str, message: str, *, retryable: bool = False) -> APIError:
    return APIError(
        status,
        code,
        message,
        param=URL_PARAM,
        retryable=retryable,
        headers={"Retry-After": "1"} if retryable else None,
    )


def require_public(address: str) -> None:
    ip = ipaddress.ip_address(address)
    if (
        not ip.is_global
        or ip.is_multicast
        or ip in ipaddress.ip_network("64:ff9b::/96")
        or ip == ipaddress.ip_address("168.63.129.16")
        or (
            isinstance(ip, ipaddress.IPv6Address)
            and (ip.ipv4_mapped is not None or ip.sixtofour is not None or ip.teredo is not None)
        )
    ):
        raise source_error(
            400, "image_url_not_allowed", "The image URL destination is not allowed."
        )


class PublicResolver(aiohttp.ThreadedResolver):
    async def resolve(
        self, host: str, port: int = 0, family: socket.AddressFamily = socket.AF_INET
    ) -> list[ResolveResult]:
        addresses = await super().resolve(host, port, family)
        for address in addresses:
            require_public(address["host"])
        return addresses


class ImageDownloader:
    def __init__(self, allowed_hosts: tuple[str, ...]) -> None:
        self._allowed_hosts = frozenset(allowed_hosts)
        self._active = 0
        self._closed = False

    async def close(self) -> None:
        self._closed = True

    async def fetch(self, url: HttpUrl) -> bytes:
        if url.scheme != "https" or url.host not in self._allowed_hosts or url.port != 443:
            raise source_error(
                400, "image_url_not_allowed", "The image URL destination is not allowed."
            )
        if self._closed or self._active >= MAX_DOWNLOADS:
            raise source_error(
                503, "service_busy", "Image downloading is temporarily unavailable.", retryable=True
            )
        self._active += 1
        try:
            async with asyncio.timeout(DOWNLOAD_SECONDS):
                for attempt in range(MAX_ATTEMPTS):
                    try:
                        return await self._download(str(url))
                    except APIError as exc:
                        if not exc.retryable or attempt == MAX_ATTEMPTS - 1:
                            raise
                        delay = int((exc.headers or {}).get("Retry-After", "1"))
                        if delay > 1:
                            raise
                    await asyncio.sleep(1 + random.uniform(0.1, 0.3))
        except TimeoutError as exc:
            raise source_error(
                503,
                "input_fetch_failed",
                "The image source is temporarily unavailable.",
                retryable=True,
            ) from exc
        finally:
            self._active -= 1
        raise RuntimeError("download attempts exhausted")

    async def _download(self, url: str) -> bytes:
        timeout = aiohttp.ClientTimeout(
            total=ATTEMPT_SECONDS,
            connect=STALL_SECONDS,
            sock_read=STALL_SECONDS,
        )
        resolver = PublicResolver()
        try:
            async with (
                aiohttp.ClientSession(
                    connector=aiohttp.TCPConnector(
                        resolver=resolver,
                        use_dns_cache=False,
                        force_close=True,
                        limit=1,
                    ),
                    timeout=timeout,
                    trust_env=False,
                    auto_decompress=False,
                    cookie_jar=aiohttp.DummyCookieJar(),
                    headers={
                        "Accept-Encoding": "identity",
                        "Accept": "image/jpeg,image/png,image/webp",
                    },
                ) as session,
                session.get(url, allow_redirects=False) as response,
            ):
                if response.status in {408, 429, 500, 502, 503, 504}:
                    error = source_error(
                        503,
                        "input_fetch_failed",
                        "The image source is temporarily unavailable.",
                        retryable=True,
                    )
                    if value := response.headers.get("Retry-After"):
                        try:
                            delay = (
                                int(value)
                                if value.isascii() and value.isdecimal()
                                else int(
                                    (
                                        parsedate_to_datetime(value) - datetime.now(UTC)
                                    ).total_seconds()
                                )
                                + 1
                            )
                            error.headers = {"Retry-After": str(max(1, min(delay, 86400)))}
                        except (ValueError, TypeError, OverflowError):
                            pass
                    raise error
                if response.status != 200:
                    raise source_error(
                        422, "input_fetch_rejected", "The image source did not return an image."
                    )
                if response.headers.get("Content-Encoding", "identity").lower() != "identity":
                    raise source_error(
                        415,
                        "unsupported_content_encoding",
                        "Encoded image responses are not supported.",
                    )
                if (
                    response.content_length is not None
                    and response.content_length > MAX_IMAGE_BYTES
                ):
                    raise source_error(413, "payload_too_large", "The image file is too large.")
                data = bytearray()
                async for chunk in response.content.iter_chunked(64 * 1024):
                    if len(data) + len(chunk) > MAX_IMAGE_BYTES:
                        raise source_error(413, "payload_too_large", "The image file is too large.")
                    data.extend(chunk)
                return bytes(data)
        except (aiohttp.ClientSSLError, aiohttp.InvalidURL) as exc:
            raise source_error(
                422, "input_fetch_rejected", "The image source could not be verified."
            ) from exc
        except (aiohttp.ClientConnectionError, aiohttp.ClientPayloadError, TimeoutError) as exc:
            raise source_error(
                503,
                "input_fetch_failed",
                "The image source is temporarily unavailable.",
                retryable=True,
            ) from exc
        except aiohttp.ClientError as exc:
            raise source_error(
                422, "input_fetch_rejected", "The image source returned an invalid response."
            ) from exc
        finally:
            await resolver.close()
