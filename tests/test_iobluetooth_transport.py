"""Unit tests for the IOBluetooth transport's connect/reset/close logic.

These use small fakes for the IOBluetooth device/channel/delegate objects
(injected via ``IOBluetoothTransport(..., device=, delegate_factory=,
pump=)``), so they run on any platform without pyobjc or hardware.  A
channel id is always passed explicitly so the real SDP code path (which
touches the ``IOBluetooth`` module) is never exercised.
"""

from __future__ import annotations

import pytest

from ptouch_bt.transport.iobluetooth import IOBluetoothTransport


class FakeDelegate:
    def __init__(self):
        self.buf = bytearray()
        self.closed = False
        self.open_status = None


class FakeChannel:
    def __init__(self):
        self.written = bytearray()
        self.closed = False

    def getMTU(self):
        return 64

    def writeSync_length_(self, chunk, length):
        self.written.extend(bytes(chunk)[:length])
        return 0

    def closeChannel(self):
        self.closed = True


class FakeDevice:
    """Scriptable IOBluetoothDevice stand-in.

    *open_script* is a list of outcomes consumed one per channel-open
    attempt: ``"ok"``, ``"reject"`` (immediate non-zero IOReturn),
    ``"fail"`` (openComplete reports an error) or ``"timeout"``
    (openComplete never fires).
    """

    def __init__(self, open_script, connected=True):
        self._open_script = list(open_script)
        self._connected = connected
        self.calls: list[str] = []
        self.channels: list[FakeChannel] = []

    def name(self):
        return "PT-E560BT_0334"

    def isConnected(self):
        return self._connected

    def openConnection(self):
        self.calls.append("openConnection")
        self._connected = True
        return 0

    def closeConnection(self):
        self.calls.append("closeConnection")
        self._connected = False
        return 0

    def openRFCOMMChannelAsync_withChannelID_delegate_(self, out, channel_id, delegate):
        outcome = self._open_script.pop(0) if self._open_script else "fail"
        self.calls.append(f"open:{outcome}")
        if outcome == "reject":
            return (0xE00002E8, None)
        channel = FakeChannel()
        self.channels.append(channel)
        if outcome == "ok":
            delegate.open_status = 0
        elif outcome == "fail":
            delegate.open_status = 0xE00002D8
        elif outcome == "timeout":
            delegate.open_status = None  # never completes
        return (0, channel)


def make_transport(open_script, *, connected=True, open_timeout=0.02):
    device = FakeDevice(open_script, connected=connected)
    transport = IOBluetoothTransport(
        "PT-E560BT_0334",
        channel_id=2,
        device=device,
        delegate_factory=FakeDelegate,
        pump=lambda _seconds: None,
        open_timeout=open_timeout,
    )
    return transport, device


def test_connect_happy_path():
    transport, device = make_transport(["ok"])
    assert transport.channel_id == 2
    assert transport.name == "PT-E560BT_0334"
    assert device.calls == ["open:ok"]  # already connected, no paging needed


def test_connect_pages_baseband_when_disconnected():
    transport, device = make_transport(["ok"], connected=False)
    assert "openConnection" in device.calls
    assert device.calls.index("openConnection") < device.calls.index("open:ok")


def test_stale_link_recovers_via_baseband_reset():
    # Both first-pass attempts fail on the (stale) existing link; a reset
    # then lets the channel open.  This is the printer power-cycle case.
    transport, device = make_transport(["fail", "fail", "ok"])
    assert transport.channel_id == 2
    assert "closeConnection" in device.calls  # baseband was reset
    # reset re-pages the device before the successful open
    assert device.calls == ["open:fail", "open:fail", "closeConnection", "openConnection", "open:ok"]


def test_open_timeout_triggers_reset_and_recovers():
    transport, device = make_transport(["timeout", "timeout", "ok"])
    assert transport.channel_id == 2
    assert device.calls.count("closeConnection") == 1
    # the timed-out channels get closed so they don't leak
    assert device.channels[0].closed
    assert device.channels[1].closed


def test_total_failure_raises_connection_error():
    with pytest.raises(ConnectionError) as exc:
        make_transport(["fail", "fail", "fail", "fail"])
    assert "baseband reset" in str(exc.value)


def test_close_releases_baseband_by_default():
    transport, device = make_transport(["ok"])
    transport.close()
    assert transport._channel.closed
    assert device.calls[-1] == "closeConnection"
    assert not device.isConnected()


def test_close_can_keep_baseband():
    transport, device = make_transport(["ok"])
    transport.close(release_baseband=False)
    assert transport._channel.closed
    assert "closeConnection" not in device.calls


def test_write_after_peer_close_raises():
    transport, _ = make_transport(["ok"])
    transport._delegate.closed = True
    with pytest.raises(ConnectionError):
        transport.write(b"hello")


def test_read_after_peer_close_raises():
    transport, _ = make_transport(["ok"])
    transport._delegate.closed = True
    with pytest.raises(ConnectionError):
        transport.read(32, timeout=0.01)


def test_write_chunks_by_mtu():
    transport, _ = make_transport(["ok"])
    transport.write(b"a" * 200)  # MTU is 64
    assert bytes(transport._channel.written) == b"a" * 200


def test_read_returns_buffered_data():
    transport, _ = make_transport(["ok"])
    transport._delegate.buf.extend(b"status-bytes")
    assert transport.read(6, timeout=0.01) == b"status"
    assert transport.read(6, timeout=0.01) == b"-bytes"
