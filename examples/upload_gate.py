"""Reference upload gate: what an application does with Modera's answers.

An upload is private until a completed moderation decision says otherwise.
Modera stores nothing and does not deduplicate, so this module owns the state
and makes every follow-up action idempotent, keyed on the application's own
upload ID and never on Modera's request ID.

States: ``pending`` (private, no decision yet), ``published``, ``held`` (a review
case is open), ``rejected``. Only a completed evaluation leaves ``pending``; an
error or a timeout leaves the upload pending.

This is an example of the contract, not a production queue: it is single-node
SQLite and has no photo storage, authentication or worker supervision.
"""

import json
import random
import sqlite3
import time
import urllib.error
import urllib.request
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

State = Literal["pending", "published", "held", "rejected"]


@dataclass(frozen=True)
class Verdict:
    """A completed evaluation: ``action`` is allow, review or block."""

    action: str
    fallback: bool = False


class RetryableError(Exception):
    """No decision, and another attempt may succeed (503, 408, no response)."""

    def __init__(self, retry_after: float = 1.0) -> None:
        super().__init__(f"retry after {retry_after}s")
        self.retry_after = retry_after


class PermanentError(Exception):
    """No decision, and repeating the request cannot help (4xx, 500, 501)."""


Moderator = Callable[[bytes], Verdict]


def target_state(verdict: Verdict) -> State:
    """Map a completed decision to a state, never trusting a reduced one.

    A fallback ``allow`` covered less than the policy asked for, so it goes to
    review. A fallback ``block`` stays rejected.
    """
    if verdict.action == "block":
        return "rejected"
    if verdict.action == "allow" and not verdict.fallback:
        return "published"
    if verdict.action in ("allow", "review"):
        return "held"
    raise PermanentError(f"unknown action {verdict.action!r}")


class UploadGate:
    def __init__(self, database: Path | str, moderate: Moderator) -> None:
        self._database = str(database)
        self._moderate = moderate
        with self._transaction() as db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS uploads ("
                "upload_id TEXT PRIMARY KEY, "
                "state TEXT NOT NULL DEFAULT 'pending', "
                "photo BLOB NOT NULL, "
                "attempts INTEGER NOT NULL DEFAULT 0)"
            )
            db.execute(
                "CREATE TABLE IF NOT EXISTS review_cases ("
                "upload_id TEXT PRIMARY KEY REFERENCES uploads(upload_id))"
            )

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        db = sqlite3.connect(self._database, timeout=10, isolation_level=None)
        try:
            db.execute("BEGIN IMMEDIATE")
            yield db
            db.execute("COMMIT")
        except BaseException:
            if db.in_transaction:
                db.execute("ROLLBACK")
            raise
        finally:
            db.close()

    def submit(self, upload_id: str, photo: bytes) -> State:
        """Record an upload as pending. Repeating an ID changes nothing."""
        with self._transaction() as db:
            db.execute(
                "INSERT OR IGNORE INTO uploads (upload_id, photo) VALUES (?, ?)",
                (upload_id, photo),
            )
        return self.state(upload_id)

    def state(self, upload_id: str) -> State:
        with self._transaction() as db:
            row = db.execute(
                "SELECT state FROM uploads WHERE upload_id = ?", (upload_id,)
            ).fetchone()
        if row is None:
            raise KeyError(upload_id)
        return row[0]  # type: ignore[no-any-return]

    def is_visible(self, upload_id: str) -> bool:
        """Only a published upload may be shown to other people."""
        return self.state(upload_id) == "published"

    def review_cases(self) -> list[str]:
        with self._transaction() as db:
            rows = db.execute("SELECT upload_id FROM review_cases").fetchall()
        return [row[0] for row in rows]

    def attempt(self, upload_id: str) -> State:
        """Make one moderation attempt. Errors propagate and leave it pending."""
        with self._transaction() as db:
            row = db.execute(
                "SELECT state, photo FROM uploads WHERE upload_id = ?", (upload_id,)
            ).fetchone()
            if row is None:
                raise KeyError(upload_id)
            if row[0] != "pending":
                return row[0]  # type: ignore[no-any-return]
            db.execute(
                "UPDATE uploads SET attempts = attempts + 1 WHERE upload_id = ?",
                (upload_id,),
            )
            photo = row[1]
        # The call runs outside the transaction so a slow model never holds the
        # database lock. Two concurrent attempts can both reach this point; the
        # guarded transition below lets exactly one of them win.
        verdict = self._moderate(photo)
        new_state = target_state(verdict)
        with self._transaction() as db:
            changed = db.execute(
                "UPDATE uploads SET state = ? "
                "WHERE upload_id = ? AND state = 'pending'",
                (new_state, upload_id),
            ).rowcount
            if changed and new_state == "held":
                db.execute(
                    "INSERT OR IGNORE INTO review_cases (upload_id) VALUES (?)",
                    (upload_id,),
                )
            row = db.execute(
                "SELECT state FROM uploads WHERE upload_id = ?", (upload_id,)
            ).fetchone()
        return row[0]  # type: ignore[no-any-return]

    def decide(
        self,
        upload_id: str,
        *,
        deadline_seconds: float = 30.0,
        base_delay: float = 0.5,
        max_attempts: int = 8,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
        jitter: Callable[[], float] = random.random,
    ) -> State:
        """Attempt until a decision, a permanent error or the deadline.

        Waits at least ``Retry-After`` and adds jitter on top, so callers rejected
        together do not return together. When the deadline or attempt limit is
        reached the upload stays pending, which is the correct answer for "unknown".
        """
        deadline = clock() + deadline_seconds
        for attempt in range(max_attempts):
            try:
                return self.attempt(upload_id)
            except RetryableError as error:
                spread = base_delay * 2**attempt
                wait = error.retry_after + jitter() * spread
                if attempt == max_attempts - 1 or clock() + wait >= deadline:
                    break
                sleep(wait)
        return self.state(upload_id)


def http_moderator(base_url: str, api_key: str, *, timeout: float = 15.0) -> Moderator:
    """Call a running Modera and translate its answers into this module's types."""
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    url = base_url.rstrip("/") + "/v1/moderations"

    def moderate(photo: bytes) -> Verdict:
        boundary = "gate-" + uuid.uuid4().hex
        body = (
            (
                f'--{boundary}\r\nContent-Disposition: form-data; name="file"; '
                f'filename="photo"\r\nContent-Type: application/octet-stream\r\n\r\n'
            ).encode()
            + photo
            + f"\r\n--{boundary}--\r\n".encode()
        )
        request = urllib.request.Request(
            url,
            data=body,
            headers={
                "Authorization": "Bearer " + api_key,
                "Content-Type": f"multipart/form-data; boundary={boundary}",
            },
        )
        try:
            with opener.open(request, timeout=timeout) as response:
                result = json.load(response)
        except urllib.error.HTTPError as error:
            retryable = False
            with suppress(ValueError, KeyError, TypeError):
                retryable = bool(json.load(error)["error"]["retryable"])
            if retryable:
                try:
                    delay = float(error.headers.get("Retry-After", "1"))
                except ValueError:
                    delay = 1.0
                raise RetryableError(delay) from error
            raise PermanentError(f"HTTP {error.code}") from error
        except (urllib.error.URLError, TimeoutError, ConnectionError) as error:
            raise RetryableError() from error
        except ValueError as error:
            raise RetryableError() from error
        try:
            decision = result["decision"]["action"]
            fallback = bool(result["fallback"])
        except (KeyError, TypeError) as error:
            raise PermanentError("malformed moderation response") from error
        return Verdict(decision, fallback)

    return moderate
