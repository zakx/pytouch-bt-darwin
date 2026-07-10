"""Serial-port transport.

On macOS, pairing a Bluetooth Classic device that offers the Serial Port
Profile creates a ``/dev/cu.<DeviceName>`` device; opening it establishes
the RFCOMM connection.  On Linux, ``rfcomm bind`` provides ``/dev/rfcomm*``.
"""

from __future__ import annotations

import glob
import sys

import serial

from . import Transport

_MACOS_PATTERNS = (
    "/dev/cu.PT-E5*",
    "/dev/cu.PT-*",
    "/dev/cu.*PT-*",
)
_LINUX_PATTERNS = ("/dev/rfcomm*",)


def find_serial_ports() -> list[str]:
    """Return candidate P-Touch serial ports, most specific first."""
    patterns = _MACOS_PATTERNS if sys.platform == "darwin" else _LINUX_PATTERNS
    seen: list[str] = []
    for pattern in patterns:
        for port in sorted(glob.glob(pattern)):
            if port not in seen:
                seen.append(port)
    return seen


class SerialTransport(Transport):
    def __init__(self, port: str, *, timeout: float = 10.0) -> None:
        self.port = port
        # write_timeout guards against the RFCOMM link stalling silently.
        self._serial = serial.Serial(
            port, baudrate=115200, timeout=timeout, write_timeout=30.0
        )

    @classmethod
    def autodetect(cls, *, timeout: float = 10.0) -> "SerialTransport":
        ports = find_serial_ports()
        if not ports:
            raise FileNotFoundError(
                "No P-Touch serial port found. Pair the printer first "
                "(System Settings > Bluetooth), then check for /dev/cu.PT-*."
            )
        return cls(ports[0], timeout=timeout)

    def write(self, data: bytes) -> None:
        self._serial.write(data)
        self._serial.flush()

    def read(self, size: int, timeout: float | None = None) -> bytes:
        if timeout is not None:
            self._serial.timeout = timeout
        return self._serial.read(size)

    def close(self) -> None:
        try:
            self._serial.close()
        except Exception:
            pass

    def __repr__(self) -> str:
        return f"SerialTransport({self.port!r})"
