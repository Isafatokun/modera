"""A draining connection must not be confused with an open ingress listener."""

import socket
import sys
from pathlib import Path

import pytest

from deploy.rehearsal.rehearse import listener_socket, owns_socket


@pytest.mark.skipif(sys.platform != "linux", reason="Linux /proc rehearsal barrier")
def test_listener_ownership_ends_before_existing_connection_closes() -> None:
    pid = int(Path("/proc/self").resolve().name)
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        target = listener_socket(listener.getsockname()[1])
        assert owns_socket(pid, target)
        with socket.create_connection(listener.getsockname(), timeout=1) as client:
            connection, _ = listener.accept()
            with connection:
                connection.settimeout(1)
                listener.close()
                assert not owns_socket(pid, target)
                client.sendall(b"existing work still drains")
                assert connection.recv(64) == b"existing work still drains"
