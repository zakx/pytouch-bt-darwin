"""Transports for talking to P-Touch printers."""

from __future__ import annotations

import abc


class Transport(abc.ABC):
    """Byte-stream transport to a printer."""

    @abc.abstractmethod
    def write(self, data: bytes) -> None: ...

    @abc.abstractmethod
    def read(self, size: int, timeout: float | None = None) -> bytes:
        """Read up to *size* bytes; may return fewer on timeout."""

    @abc.abstractmethod
    def close(self) -> None: ...

    def read_exact(self, size: int, timeout: float = 10.0) -> bytes:
        """Read exactly *size* bytes or raise ``TimeoutError``."""
        import time

        buf = bytearray()
        deadline = time.monotonic() + timeout
        while len(buf) < size:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError(
                    f"expected {size} bytes, got {len(buf)} before timeout"
                )
            chunk = self.read(size - len(buf), timeout=remaining)
            if chunk:
                buf.extend(chunk)
        return bytes(buf)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


from .serialport import SerialTransport, find_serial_ports  # noqa: E402

__all__ = ["Transport", "SerialTransport", "find_serial_ports"]
