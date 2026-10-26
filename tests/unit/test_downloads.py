import asyncio
import socket
import ssl
import subprocess
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import aiohttp
import pytest
from aiohttp import web
from pydantic import HttpUrl, ValidationError

from modera import downloads
from modera.config import Settings
from modera.downloads import ImageDownloader, PublicResolver, require_public
from modera.errors import APIError


@pytest.mark.parametrize(
    "address",
    [
        "127.0.0.1",
        "10.0.0.1",
        "169.254.169.254",
        "100.64.0.1",
        "::1",
        "fc00::1",
        "ff02::1",
        "::ffff:8.8.8.8",
        "2002:0808:0808::1",
        "64:ff9b::7f00:1",
        "168.63.129.16",
    ],
)
def test_nonpublic_and_transition_addresses_are_rejected(address: str) -> None:
    with pytest.raises(APIError) as error:
        require_public(address)
    assert error.value.status_code == 400


def test_public_addresses() -> None:
    require_public("8.8.8.8")
    require_public("2606:4700:4700::1111")


@pytest.mark.parametrize(
    "host",
    [
        "*.example.com",
        "localhost",
        "127.0.0.1",
        "::1",
        "example.com:443",
        "https://example.com",
        "a.example/",
    ],
)
def test_host_configuration_rejects_ambiguous_destinations(host: str) -> None:
    with pytest.raises(ValidationError):
        Settings(image_allowed_hosts=(host,))


def test_host_configuration_normalizes_names() -> None:
    assert Settings(
        image_allowed_hosts=("Media.Example.com", "media.example.com")
    ).image_allowed_hosts == ("media.example.com",)


def test_resolver_rejects_mixed_dns_answers(monkeypatch: pytest.MonkeyPatch) -> None:
    async def scenario() -> None:
        lookup = AsyncMock(return_value=[{"host": "8.8.8.8"}, {"host": "127.0.0.1"}])
        monkeypatch.setattr(aiohttp.ThreadedResolver, "resolve", lookup)
        resolver = PublicResolver()
        try:
            with pytest.raises(APIError):
                await resolver.resolve("media.example.com", 443)
            assert lookup.await_count == 1
        finally:
            await resolver.close()

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "url",
    [
        "https://evil.example/x",
        "https://media.example.com.evil.example/x",
        "https://media.example.com:8443/x",
        "https://127.0.0.1/x",
    ],
)
def test_destination_rejected_before_network(url: str) -> None:
    async def scenario() -> None:
        worker = ImageDownloader(("media.example.com",))
        with pytest.raises(APIError) as error:
            await worker.fetch(HttpUrl(url))
        assert error.value.code == "image_url_not_allowed"

    asyncio.run(scenario())


def test_retry_restarts_download_and_releases_capacity(monkeypatch: pytest.MonkeyPatch) -> None:
    async def scenario() -> None:
        worker = ImageDownloader(("media.example.com",))
        method = AsyncMock(
            side_effect=[
                downloads.source_error(503, "input_fetch_failed", "Unavailable", retryable=True),
                b"complete",
            ]
        )
        monkeypatch.setattr(worker, "_download", method)
        assert await worker.fetch(HttpUrl("https://media.example.com/a")) == b"complete"
        assert method.await_count == 2
        assert method.await_args_list[0] == method.await_args_list[1]
        assert worker._active == 0

    asyncio.run(scenario())


def test_cancellation_and_overload(monkeypatch: pytest.MonkeyPatch) -> None:
    async def scenario() -> None:
        monkeypatch.setattr(downloads, "MAX_DOWNLOADS", 1)
        worker = ImageDownloader(("media.example.com",))
        entered = asyncio.Event()

        async def blocked(url: str) -> bytes:
            entered.set()
            await asyncio.Event().wait()
            return b""

        monkeypatch.setattr(worker, "_download", blocked)
        url = HttpUrl("https://media.example.com/a")
        task = asyncio.create_task(worker.fetch(url))
        await entered.wait()
        with pytest.raises(APIError) as error:
            await worker.fetch(url)
        assert error.value.code == "service_busy"
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert worker._active == 0
        await worker.close()
        with pytest.raises(APIError):
            await worker.fetch(url)

    asyncio.run(scenario())


async def with_source(
    handler: Callable[[web.Request], Awaitable[web.StreamResponse]],
    check: Callable[[str], Awaitable[None]],
) -> None:
    app = web.Application()
    app.router.add_get("/image", handler)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    try:
        port = runner.addresses[0][1]
        await check(f"http://127.0.0.1:{port}/image")
    finally:
        await runner.cleanup()


@pytest.mark.parametrize(
    "status,code,retryable",
    [
        (302, "input_fetch_rejected", False),
        (404, "input_fetch_rejected", False),
        (503, "input_fetch_failed", True),
        (429, "input_fetch_failed", True),
    ],
)
def test_source_statuses(status: int, code: str, retryable: bool) -> None:
    async def handler(request: web.Request) -> web.Response:
        return web.Response(status=status, headers={"Location": "http://127.0.0.1/private"})

    async def check(url: str) -> None:
        with pytest.raises(APIError) as error:
            await ImageDownloader(())._download(url)
        assert error.value.code == code
        assert error.value.retryable is retryable
        assert "127.0.0.1" not in error.value.detail

    asyncio.run(with_source(handler, check))


@pytest.mark.parametrize("declared", [True, False])
def test_stream_size_limits(monkeypatch: pytest.MonkeyPatch, declared: bool) -> None:
    monkeypatch.setattr(downloads, "MAX_IMAGE_BYTES", 3)

    async def handler(request: web.Request) -> web.StreamResponse:
        response = web.StreamResponse(headers={"Content-Length": "4"} if declared else {})
        await response.prepare(request)
        await response.write(b"abcd")
        return response

    async def check(url: str) -> None:
        with pytest.raises(APIError) as error:
            await ImageDownloader(())._download(url)
        assert error.value.status_code == 413

    asyncio.run(with_source(handler, check))


def test_interrupted_body_is_discarded_on_restart(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = 0

    async def handler(request: web.Request) -> web.StreamResponse:
        nonlocal calls
        calls += 1
        if calls == 1:
            response = web.StreamResponse(headers={"Content-Length": "100"})
            await response.prepare(request)
            await response.write(b"partial")
            assert request.transport is not None
            request.transport.close()
            return response
        assert "Range" not in request.headers
        assert "Authorization" not in request.headers
        return web.Response(body=b"whole image")

    async def check(url: str) -> None:
        worker = ImageDownloader(("media.example.com",))
        original = worker._download

        async def local(_url: str) -> bytes:
            return await original(url)

        monkeypatch.setattr(worker, "_download", local)
        assert await worker.fetch(HttpUrl("https://media.example.com/image")) == b"whole image"
        assert calls == 2

    asyncio.run(with_source(handler, check))


def test_stalled_body_exhausts_retries(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(downloads, "STALL_SECONDS", 0.01)
    calls = 0

    async def handler(request: web.Request) -> web.StreamResponse:
        nonlocal calls
        calls += 1
        response = web.StreamResponse()
        await response.prepare(request)
        await asyncio.sleep(0.05)
        return response

    async def check(url: str) -> None:
        worker = ImageDownloader(("media.example.com",))
        original = worker._download

        async def local(_url: str) -> bytes:
            return await original(url)

        monkeypatch.setattr(worker, "_download", local)
        with pytest.raises(APIError) as error:
            await worker.fetch(HttpUrl("https://media.example.com/image"))
        assert error.value.retryable
        assert calls == 2
        assert worker._active == 0

    asyncio.run(with_source(handler, check))


def test_total_deadline_and_permanent_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    async def scenario() -> None:
        worker = ImageDownloader(("media.example.com",))
        method = AsyncMock(
            side_effect=downloads.source_error(422, "input_fetch_rejected", "Rejected")
        )
        monkeypatch.setattr(worker, "_download", method)
        with pytest.raises(APIError):
            await worker.fetch(HttpUrl("https://media.example.com/a"))
        assert method.await_count == 1
        monkeypatch.setattr(downloads, "DOWNLOAD_SECONDS", 0.01)

        async def blocked(url: str) -> bytes:
            await asyncio.sleep(1)
            return b""

        monkeypatch.setattr(worker, "_download", blocked)
        with pytest.raises(APIError) as error:
            await worker.fetch(HttpUrl("https://media.example.com/a"))
        assert error.value.retryable
        assert worker._active == 0

    asyncio.run(scenario())


def test_compressed_response_is_not_decoded() -> None:
    async def handler(request: web.Request) -> web.Response:
        return web.Response(body=b"not gzip", headers={"Content-Encoding": "gzip"})

    async def check(url: str) -> None:
        with pytest.raises(APIError) as error:
            await ImageDownloader(())._download(url)
        assert error.value.status_code == 415

    asyncio.run(with_source(handler, check))


def test_tls_hostname_and_single_resolution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    certificate = tmp_path / "cert.pem"
    key = tmp_path / "key.pem"
    subprocess.run(
        [
            "openssl",
            "req",
            "-x509",
            "-newkey",
            "rsa:2048",
            "-nodes",
            "-days",
            "1",
            "-keyout",
            str(key),
            "-out",
            str(certificate),
            "-subj",
            "/CN=media.example.com",
            "-addext",
            "subjectAltName=DNS:media.example.com",
        ],
        check=True,
        capture_output=True,
    )
    server_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    server_context.load_cert_chain(certificate, key)
    names: list[str | None] = []
    server_context.set_servername_callback(lambda _socket, name, _context: names.append(name))
    client_context = ssl.create_default_context(cafile=str(certificate))
    connector_class = aiohttp.TCPConnector

    def trusted_connector(**kwargs: Any) -> aiohttp.TCPConnector:
        return connector_class(ssl=client_context, **kwargs)

    monkeypatch.setattr(aiohttp, "TCPConnector", trusted_connector)
    checked: list[str] = []
    monkeypatch.setattr(downloads, "require_public", checked.append)

    async def scenario() -> None:
        async def handler(request: web.Request) -> web.Response:
            assert request.host.startswith("media.example.com:")
            return web.Response(body=b"image bytes")

        app = web.Application()
        app.router.add_get("/image", handler)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0, ssl_context=server_context)
        await site.start()
        try:
            port = runner.addresses[0][1]
            lookup = AsyncMock(
                return_value=[
                    (
                        socket.AF_INET,
                        socket.SOCK_STREAM,
                        socket.IPPROTO_TCP,
                        "",
                        ("127.0.0.1", port),
                    )
                ]
            )
            monkeypatch.setattr(asyncio.get_running_loop(), "getaddrinfo", lookup)
            worker = ImageDownloader(())
            assert (
                await worker._download(f"https://media.example.com:{port}/image") == b"image bytes"
            )
            assert lookup.await_count == 1
            assert checked == ["127.0.0.1"]
            assert names == ["media.example.com"]
            with pytest.raises(APIError) as error:
                await worker._download(f"https://wrong.example.com:{port}/image")
            assert error.value.code == "input_fetch_rejected"
            assert not error.value.retryable
        finally:
            await runner.cleanup()

    asyncio.run(scenario())


def test_source_retry_after_is_preserved(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = 0

    async def handler(request: web.Request) -> web.Response:
        nonlocal calls
        calls += 1
        return web.Response(status=429, headers={"Retry-After": "60"})

    async def check(url: str) -> None:
        worker = ImageDownloader(("media.example.com",))
        original = worker._download

        async def local(_url: str) -> bytes:
            return await original(url)

        monkeypatch.setattr(worker, "_download", local)
        with pytest.raises(APIError) as error:
            await worker.fetch(HttpUrl("https://media.example.com/image"))
        assert error.value.headers == {"Retry-After": "60"}
        assert calls == 1

    asyncio.run(with_source(handler, check))
