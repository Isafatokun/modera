import json
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from threading import Thread
from typing import Any, ClassVar

import pytest

from evaluation.photos import (
    Case,
    Outcome,
    check_endpoint,
    load_cases,
    submit,
    summarize,
)


def case(name: str, expected: str, slice: str = "all") -> Case:
    return Case.model_validate(
        {"id": name, "file": f"{name}.png", "expected": expected, "slice": slice}
    )


def done(action: str, provenance: str = "p1") -> Outcome:
    return Outcome(action=action, provenance=json.dumps({"v": provenance}))


def test_misses_false_flags_and_failures_are_reported_separately() -> None:
    cases = [
        case("a", "allow"),
        case("b", "allow"),
        case("c", "block"),
        case("d", "block"),
        case("e", "review"),
        case("f", "block"),
    ]
    outcomes = {
        "a": done("allow"),
        "b": done("review"),
        "c": done("allow"),
        "d": done("block"),
        "e": done("block"),
        "f": Outcome(error="check_unavailable"),
    }
    report = summarize(cases, outcomes)
    assert report["cases"] == 6 and report["completed"] == 5
    assert report["missed"] == ["c"]
    assert report["false_flags"] == ["b"]
    assert report["wrong_level"] == ["e"]
    assert report["failed_checks"] == {"block": 1}
    assert report["failure_codes"] == {"check_unavailable": 1}
    assert report["confusion"]["block"] == {"allow": 1, "review": 0, "block": 1}
    assert report["provenance_consistent"]


def test_failed_check_is_never_counted_as_an_allow() -> None:
    report = summarize([case("a", "block")], {"a": Outcome(error="network")})
    assert report["missed"] == []
    assert report["confusion"]["block"]["allow"] == 0
    assert report["failed_checks"] == {"block": 1}


def test_changed_provenance_is_flagged_and_slices_are_split() -> None:
    cases = [case("a", "allow", "x"), case("b", "block", "y")]
    report = summarize(cases, {"a": done("allow", "p1"), "b": done("block", "p2")})
    assert not report["provenance_consistent"]
    assert set(report["slices"]) == {"x", "y"}
    assert report["slices"]["y"]["cases"] == 1


def test_cases_must_stay_beside_the_cases_file(tmp_path: Path) -> None:
    (tmp_path / "ok.png").write_bytes(b"x")
    outside = tmp_path.parent / "outside.png"
    outside.write_bytes(b"x")
    path = tmp_path / "cases.json"
    path.write_text(json.dumps([{"id": "a", "file": "ok.png", "expected": "allow"}]))
    assert load_cases(path)[0][1] == (tmp_path / "ok.png").resolve()
    for bad in (
        [{"id": "a", "file": "../outside.png", "expected": "allow"}],
        [{"id": "a", "file": "missing.png", "expected": "allow"}],
        [
            {"id": "a", "file": "ok.png", "expected": "allow"},
            {"id": "a", "file": "ok.png", "expected": "block"},
        ],
        [],
    ):
        path.write_text(json.dumps(bad))
        with pytest.raises(ValueError):
            load_cases(path)


@pytest.mark.parametrize(
    "url,ok",
    [
        ("http://127.0.0.1:8000", True),
        ("http://localhost:8000", True),
        ("https://modera.example.com", True),
        ("http://modera.example.com", False),
        ("ftp://127.0.0.1", False),
    ],
)
def test_credentials_are_only_sent_over_https_or_loopback(url: str, ok: bool) -> None:
    if ok:
        assert check_endpoint(url).endswith("/v1/moderations")
    else:
        with pytest.raises(ValueError):
            check_endpoint(url)


class Stub(BaseHTTPRequestHandler):
    replies: ClassVar[list[tuple[int, Any]]] = []
    seen: ClassVar[list[dict[str, Any]]] = []

    def do_POST(self) -> None:
        length = int(self.headers["Content-Length"])
        body = self.rfile.read(length)
        self.seen.append({"auth": self.headers["Authorization"], "body": body})
        status, payload = self.replies.pop(0)
        data = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args: object) -> None:
        pass


@pytest.fixture
def stub() -> Iterator[str]:
    Stub.replies, Stub.seen = [], []
    server = HTTPServer(("127.0.0.1", 0), Stub)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_port}/v1/moderations"
    server.shutdown()
    server.server_close()


def test_submit_reads_the_decision_and_provenance(stub: str) -> None:
    Stub.replies = [
        (
            200,
            {
                "decision": {"action": "review"},
                "policy": {"id": "default", "version": "1"},
                "models": [{"id": "m", "revision": "r"}],
                "preprocessing_version": "image-v1.x",
            },
        )
    ]
    outcome = submit(stub, "secret-key", "default", b"photo-bytes", 5)
    assert outcome.action == "review" and outcome.error is None
    assert outcome.provenance and "image-v1.x" in outcome.provenance
    assert Stub.seen[0]["auth"] == "Bearer secret-key"
    assert b"photo-bytes" in Stub.seen[0]["body"]


def test_submit_records_typed_errors_without_a_decision(stub: str) -> None:
    Stub.replies = [(503, {"error": {"code": "check_unavailable"}}), (502, "bad")]
    first = submit(stub, "k", "default", b"x", 5)
    second = submit(stub, "k", "default", b"x", 5)
    assert first.action is None and first.error == "check_unavailable"
    assert second.action is None and second.error == "http_502"


def test_submit_records_unreachable_server_as_a_failed_check() -> None:
    outcome = submit("http://127.0.0.1:9/v1/moderations", "k", "default", b"x", 1)
    assert outcome.action is None and outcome.error == "network"
