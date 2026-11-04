"""Newline-delimited JSON over an AF_UNIX stream, with SCM_RIGHTS fd passing.

The connection itself is the lease. That is the whole point of using a socket:
abrupt process death closes its file descriptors in the kernel, so the
supervisor learns that an owner is gone without depending on Python signal
handling, a heartbeat, or fixture finalization — none of which run after
``SIGKILL``.
"""

from __future__ import annotations

import json
import socket

MAX_FDS = 8
_CHUNK = 65536


class ProtocolError(RuntimeError):
    pass


class Conn:
    """A framed connection. Every read goes through ``recv_fds`` so ancillary
    data can never be silently dropped by an intervening plain ``recv``."""

    def __init__(self, sock: socket.socket) -> None:
        self.sock = sock
        self._buf = b""
        self._fds: list[int] = []

    def fileno(self) -> int:
        return self.sock.fileno()

    def send(self, message: dict, fds: list[int] | None = None) -> None:
        payload = (json.dumps(message, default=str) + "\n").encode("utf-8")
        if fds:
            socket.send_fds(self.sock, [payload], fds)
        else:
            self.sock.sendall(payload)

    def recv(self) -> tuple[dict | None, list[int]]:
        """Return (message, fds). ``(None, [])`` means the peer closed — EOF.

        EOF on a lease connection is the abrupt-death signal; the caller must
        treat it as owner loss, not as an orderly goodbye.
        """
        while b"\n" not in self._buf:
            try:
                data, fds, _flags, _addr = socket.recv_fds(self.sock, _CHUNK, MAX_FDS)
            except (ConnectionResetError, OSError):
                return None, []
            if fds:
                self._fds.extend(fds)
            if not data:
                return None, []
            self._buf += data
        line, _, self._buf = self._buf.partition(b"\n")
        fds, self._fds = self._fds, []
        try:
            return json.loads(line.decode("utf-8")), fds
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            for fd in fds:
                try:
                    import os

                    os.close(fd)
                except OSError:
                    pass
            raise ProtocolError(f"undecodable frame: {line[:200]!r}") from exc

    def request(self, message: dict, fds: list[int] | None = None) -> dict:
        """Send and block for the matching reply. Used only by clients."""
        self.send(message, fds)
        reply, _ = self.recv()
        if reply is None:
            raise ProtocolError(
                f"supervisor closed while answering {message.get('op')!r}"
            )
        if not reply.get("ok", False):
            raise SupervisorError(reply.get("error", "unknown supervisor error"))
        return reply

    def close(self) -> None:
        try:
            self.sock.close()
        except OSError:
            pass


class SupervisorError(RuntimeError):
    pass
