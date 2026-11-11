"""A draining connection must not be confused with an open ingress listener."""

import os
import socket
import sys
from pathlib import Path

import pytest

from e2e.rehearsal.rehearse import Worker, listener_socket, owns_socket, start_time


@pytest.mark.skipif(sys.platform != "linux", reason="Linux /proc rehearsal barrier")
def test_listener_ownership_ends_before_existing_connection_closes() -> None:
    pid = int(Path("/proc/self").resolve().name)
    started = start_time(pid)
    assert started is not None
    worker = Worker(pid, started)
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        target = listener_socket(listener.getsockname()[1])
        assert owns_socket(worker, target)
        with socket.create_connection(listener.getsockname(), timeout=1) as client:
            connection, _ = listener.accept()
            with connection:
                connection.settimeout(1)
                listener.close()
                assert not owns_socket(worker, target)
                client.sendall(b"existing work still drains")
                assert connection.recv(64) == b"existing work still drains"


TARGET = "socket:[12345]"


def process(proc: Path, pid: int, comm: str, started: str, *, fd: str | None) -> None:
    """Write a fake /proc entry: 52 stat fields with the start time at field 22."""
    entry = proc / str(pid)
    entry.mkdir(parents=True)
    fields = ["S"] + ["0"] * 18 + [started] + ["0"] * 30
    (entry / "stat").write_text(f"{pid} ({comm}) " + " ".join(fields) + "\n")
    (entry / "fd").mkdir()
    if fd is not None:
        (entry / "fd" / "7").symlink_to(fd)


def test_worker_holding_the_listener_owns_it(tmp_path: Path) -> None:
    process(tmp_path, 4242, "nginx", "900", fd=TARGET)
    assert owns_socket(Worker(4242, "900"), TARGET, tmp_path)


def test_worker_with_other_sockets_does_not_own_it(tmp_path: Path) -> None:
    process(tmp_path, 4242, "nginx", "900", fd="socket:[999]")
    assert not owns_socket(Worker(4242, "900"), TARGET, tmp_path)


def test_worker_that_has_exited_owns_nothing(tmp_path: Path) -> None:
    assert not owns_socket(Worker(4242, "900"), TARGET, tmp_path)


def test_reused_pid_is_not_the_old_worker(tmp_path: Path) -> None:
    process(tmp_path, 4242, "other", "901", fd=TARGET)
    assert not owns_socket(Worker(4242, "900"), TARGET, tmp_path)


def test_command_names_with_spaces_and_parentheses_are_parsed(tmp_path: Path) -> None:
    process(tmp_path, 4242, "nginx: worker (old) process", "900", fd=TARGET)
    assert start_time(4242, tmp_path) == "900"


@pytest.mark.skipif(
    not hasattr(os, "geteuid") or os.geteuid() == 0,
    reason="permissions need a non-root user",
)
def test_unreadable_descriptors_are_treated_as_still_owning(tmp_path: Path) -> None:
    process(tmp_path, 4242, "nginx", "900", fd=TARGET)
    descriptors = tmp_path / "4242" / "fd"
    descriptors.chmod(0)
    try:
        assert owns_socket(Worker(4242, "900"), TARGET, tmp_path)
    finally:
        descriptors.chmod(0o700)
