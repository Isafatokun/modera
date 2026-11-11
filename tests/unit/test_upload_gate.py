import threading
from pathlib import Path

import pytest

from examples.upload_gate import (
    PermanentError,
    RetryableError,
    UploadGate,
    Verdict,
    target_state,
)


class Scripted:
    """A moderator that plays back a list of verdicts or errors."""

    def __init__(self, *steps: Verdict | Exception) -> None:
        self.steps = list(steps)
        self.calls = 0

    def __call__(self, photo: bytes) -> Verdict:
        self.calls += 1
        step = self.steps.pop(0) if len(self.steps) > 1 else self.steps[0]
        if isinstance(step, Exception):
            raise step
        return step


def gate(tmp_path: Path, *steps: Verdict | Exception) -> tuple[UploadGate, Scripted]:
    moderator = Scripted(*steps)
    return UploadGate(tmp_path / "gate.db", moderator), moderator


def test_upload_is_private_until_a_decision_publishes_it(tmp_path: Path) -> None:
    g, _ = gate(tmp_path, Verdict("allow"))
    assert g.submit("u1", b"photo") == "pending"
    assert not g.is_visible("u1")
    assert g.attempt("u1") == "published"
    assert g.is_visible("u1")


@pytest.mark.parametrize(
    ("verdict", "state"),
    [
        (Verdict("block"), "rejected"),
        (Verdict("block", fallback=True), "rejected"),
        (Verdict("review"), "held"),
        (Verdict("allow", fallback=True), "held"),
    ],
)
def test_each_completed_decision_has_one_state(
    tmp_path: Path, verdict: Verdict, state: str
) -> None:
    g, _ = gate(tmp_path, verdict)
    g.submit("u1", b"photo")
    assert g.attempt("u1") == state
    assert not g.is_visible("u1")


def test_unknown_action_is_an_error_not_an_allow() -> None:
    with pytest.raises(PermanentError):
        target_state(Verdict("approve"))


def test_timeout_leaves_the_photo_pending_and_private(tmp_path: Path) -> None:
    g, _ = gate(tmp_path, RetryableError(1))
    g.submit("u1", b"photo")
    with pytest.raises(RetryableError):
        g.attempt("u1")
    assert g.state("u1") == "pending"
    assert not g.is_visible("u1")


def test_permanent_error_never_publishes(tmp_path: Path) -> None:
    g, _ = gate(tmp_path, PermanentError("model_not_loaded"))
    g.submit("u1", b"photo")
    with pytest.raises(PermanentError):
        g.decide("u1", sleep=lambda _: None)
    assert g.state("u1") == "pending"


def test_retry_after_a_timeout_publishes_once_modera_answers(tmp_path: Path) -> None:
    g, moderator = gate(
        tmp_path, RetryableError(1), RetryableError(1), Verdict("allow")
    )
    g.submit("u1", b"photo")
    waits: list[float] = []
    assert g.decide("u1", sleep=waits.append, jitter=lambda: 0.5) == "published"
    assert moderator.calls == 3
    assert len(waits) == 2
    assert all(wait >= 1 for wait in waits)  # never below Retry-After


def test_retries_add_jitter_on_top_of_retry_after(tmp_path: Path) -> None:
    g, _ = gate(tmp_path, RetryableError(1), Verdict("allow"))
    g.submit("u1", b"photo")
    waits: list[float] = []
    g.decide("u1", sleep=waits.append, jitter=lambda: 1.0, base_delay=0.5)
    assert waits == [1.5]


def test_deadline_ends_retrying_and_keeps_the_upload_pending(tmp_path: Path) -> None:
    g, moderator = gate(tmp_path, RetryableError(1))
    g.submit("u1", b"photo")
    now = [0.0]
    state = g.decide(
        "u1",
        deadline_seconds=3,
        sleep=lambda seconds: now.__setitem__(0, now[0] + seconds),
        clock=lambda: now[0],
        jitter=lambda: 0.0,
    )
    assert state == "pending"
    assert 1 < moderator.calls < 8
    assert not g.is_visible("u1")


def test_attempt_limit_bounds_the_work_per_upload(tmp_path: Path) -> None:
    g, moderator = gate(tmp_path, RetryableError(0))
    g.submit("u1", b"photo")
    state = g.decide("u1", max_attempts=4, sleep=lambda _: None, jitter=lambda: 0.0)
    assert state == "pending"
    assert moderator.calls == 4


def test_repeated_submit_and_repeated_attempt_are_idempotent(tmp_path: Path) -> None:
    g, moderator = gate(tmp_path, Verdict("review"))
    g.submit("u1", b"photo")
    g.submit("u1", b"different bytes")  # a client retry of the upload itself
    assert g.attempt("u1") == "held"
    assert g.attempt("u1") == "held"
    assert moderator.calls == 1  # decided uploads are not sent again
    assert g.review_cases() == ["u1"]


def test_concurrent_retries_open_one_review_case(tmp_path: Path) -> None:
    barrier = threading.Barrier(6)

    def moderate(photo: bytes) -> Verdict:
        barrier.wait(5)  # all six attempts are inside the model call together
        return Verdict("review")

    g = UploadGate(tmp_path / "gate.db", moderate)
    g.submit("u1", b"photo")
    results: list[str] = []
    threads = [
        threading.Thread(target=lambda: results.append(g.attempt("u1")))
        for _ in range(6)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(10)
    assert results == ["held"] * 6
    assert g.review_cases() == ["u1"]


def test_late_answer_cannot_overwrite_a_decision(tmp_path: Path) -> None:
    database = tmp_path / "gate.db"
    winner = UploadGate(database, lambda photo: Verdict("block"))

    def slow_allow(photo: bytes) -> Verdict:
        # Another attempt finishes while this one is still inside the model call.
        assert winner.attempt("u1") == "rejected"
        return Verdict("allow")

    slow = UploadGate(database, slow_allow)
    slow.submit("u1", b"photo")
    assert slow.attempt("u1") == "rejected"
    assert not slow.is_visible("u1")
