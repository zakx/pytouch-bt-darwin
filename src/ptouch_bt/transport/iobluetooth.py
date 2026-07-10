"""IOBluetooth RFCOMM transport for macOS.

This is the primary transport on macOS: some printers (e.g. PT-E560BT)
expose two RFCOMM channels — Apple iAP on channel 1 and SPP on channel 2
— and the auto-created /dev/cu.* serial node binds the iAP channel,
which silently swallows all data.  Connecting to the SDP-advertised SPP
channel directly always reaches the print engine.

Requires ``pyobjc-framework-IOBluetooth`` and Bluetooth permission for
the terminal app (System Settings > Privacy & Security > Bluetooth).

Robustness note (printer power-cycle recovery)
----------------------------------------------
An ``IOBluetoothDevice`` object lives independently of the baseband link,
and macOS caches it per address for the life of the process.  When the
printer is powered off and on, macOS often auto-reconnects a *new*
baseband link (for Apple iAP), so ``isConnected()`` keeps reporting
``True`` even though the previous RFCOMM/SPP session is dead.  Opening a
fresh SPP channel over that half-open link then fails or hangs.

To recover we (a) never trust ``isConnected()`` as proof that a channel
can be opened, (b) treat a failed/timed-out channel open as a signal to
tear the baseband down (``closeConnection``), re-page the device, refresh
the SDP query, and retry, and (c) release the baseband on ``close()`` so
each print job starts from a clean, disconnected state.  Every wait is
bounded so the caller's thread (a paho-mqtt worker in production) can
never hang forever.
"""

from __future__ import annotations

import logging
import time

from . import Transport

log = logging.getLogger(__name__)

try:
    import IOBluetooth
    import objc
    from CoreFoundation import CFRunLoopRunInMode, kCFRunLoopDefaultMode
    from Foundation import NSObject

    HAVE_IOBLUETOOTH = True
except ImportError:  # pragma: no cover
    HAVE_IOBLUETOOTH = False


def _pump(seconds: float) -> None:
    """Run the current thread's CFRunLoop for *seconds* so IOBluetooth
    delegate callbacks get delivered."""
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        CFRunLoopRunInMode(kCFRunLoopDefaultMode, 0.1, False)


def _ioreturn(status: int) -> str:
    return f"0x{status & 0xFFFFFFFF:08x}"


if HAVE_IOBLUETOOTH:

    class _RFCOMMDelegate(NSObject):
        def init(self):
            self = objc.super(_RFCOMMDelegate, self).init()
            self.buf = bytearray()
            self.closed = False
            self.open_status = None  # None until openComplete fires
            return self

        def rfcommChannelData_data_length_(self, channel, data, length):
            self.buf += bytes(data)

        def rfcommChannelOpenComplete_status_(self, channel, status):
            # Required for openRFCOMMChannelSync to complete reliably; also
            # records the status for debug logging.
            self.open_status = int(status)

        def rfcommChannelClosed_(self, channel):
            self.closed = True

    def _make_delegate():
        return _RFCOMMDelegate.alloc().init()


class IOBluetoothTransport(Transport):
    """RFCOMM channel to a paired Bluetooth Classic device.

    Construction runs the full staged connect (baseband -> SDP -> RFCOMM
    channel), retrying once through a full baseband reset if the first
    channel open fails or times out.

    The IOBluetooth objects are injectable so the retry/reset logic can be
    exercised on non-macOS hosts:

    * *device* — a pre-resolved ``IOBluetoothDevice`` (or a fake).
    * *delegate_factory* — returns a fresh RFCOMM delegate per open attempt.
    * *pump* — ``pump(seconds)`` to service the run loop (no-op in tests).
    """

    SPP_UUID16 = 0x1101

    # Opening the SPP channel (usually 2) on a freshly-paged link fails: the
    # printer only accepts a data-channel DLCI once the RFCOMM multiplexer
    # exists, and a direct SPP open on a cold mux stalls (the async open's
    # completion callback never fires; the sync open returns kIOReturnError
    # after its ~3s internal timeout).  Opening the always-available Apple
    # iAP channel first establishes the mux, after which the SPP channel
    # opens immediately.  See _prime_mux.
    PRIME_CHANNEL = 1

    def __init__(
        self,
        address_or_name: str,
        channel_id: int | None = None,
        *,
        device=None,
        delegate_factory=None,
        pump=None,
    ) -> None:
        if device is None:
            if not HAVE_IOBLUETOOTH:
                raise ImportError(
                    "IOBluetoothTransport needs pyobjc-framework-IOBluetooth "
                    "(pip install pyobjc-framework-IOBluetooth)"
                )
            device = self._resolve_device(address_or_name)
        self._device = device
        self._target = address_or_name
        self._pump = pump if pump is not None else _pump
        self._delegate_factory = (
            delegate_factory if delegate_factory is not None else _make_delegate
        )
        self._delegate = None
        self._channel = None

        channel, resolved_channel_id = self._open_session(channel_id)

        self._channel = channel
        self.channel_id = resolved_channel_id
        self.name = str(device.name())
        log.info(
            "IOBluetooth transport up: %s on RFCOMM channel %d",
            self.name,
            resolved_channel_id,
        )

    # -- staged connect -----------------------------------------------------

    def _open_session(self, channel_id: int | None):
        """Bring up an RFCOMM channel, resetting the baseband and retrying
        once if the first attempt fails.  Returns ``(channel, channel_id)``."""
        # Pass 1: use whatever baseband link already exists (page the device
        # only if it is genuinely disconnected).
        self._ensure_baseband(reset=False)
        ch_id = channel_id if channel_id is not None else self._resolve_channel()
        channel = self._try_open_channel(ch_id)
        if channel is not None:
            return channel, ch_id

        # Pass 2: the existing link is stale (classic symptom after a printer
        # power-cycle — macOS still shows "connected").  Force a clean
        # baseband, re-query SDP, and try again.
        log.warning(
            "RFCOMM open failed on the existing link to %s; "
            "resetting baseband and retrying",
            self._target,
        )
        self._ensure_baseband(reset=True)
        ch_id = channel_id if channel_id is not None else self._resolve_channel()
        channel = self._try_open_channel(ch_id)
        if channel is not None:
            return channel, ch_id

        raise ConnectionError(
            f"RFCOMM channel {ch_id} to {self._target} would not open, even "
            "after a baseband reset. Is the printer powered on and in range?"
        )

    def _ensure_baseband(self, reset: bool) -> None:
        """Make sure a baseband link exists.  When *reset* is set, drop any
        existing (possibly stale) link first and re-page the device."""
        device = self._device
        if reset:
            log.debug("Dropping baseband link to %s", self._target)
            try:
                device.closeConnection()
            except Exception:  # pragma: no cover - framework hiccup
                pass
            self._pump(0.5)
        if reset or not device.isConnected():
            log.debug("Paging baseband link to %s", self._target)
            status = device.openConnection()
            if status != 0:
                raise ConnectionError(
                    f"baseband connection to {self._target} failed "
                    f"(IOReturn {_ioreturn(status)})"
                )
            self._pump(1.0)  # let the connection settle before a channel open
        else:
            log.debug("Baseband link to %s already up", self._target)

    def _resolve_channel(self) -> int:
        """Find the RFCOMM channel of the SPP service (not Apple iAP).

        Always issues a fresh SDP query: a cached record from a previous
        (pre-power-cycle) session can point at a stale channel.
        """
        device = self._device
        device.performSDPQuery_(None)
        self._pump(2.0)
        uuid = IOBluetooth.IOBluetoothSDPUUID.uuid16_(self.SPP_UUID16)
        record = device.getServiceRecordForUUID_(uuid)
        if record is not None:
            ok, channel_id = record.getRFCOMMChannelID_(None)
            if ok == 0:
                log.debug("SDP: SPP service on RFCOMM channel %d", channel_id)
                return channel_id
        log.warning("SDP query found no SPP record for %s; assuming channel 1", self._target)
        return 1

    def _try_open_channel(self, channel_id: int, attempts: int = 2):
        """Open *channel_id* synchronously (the open is self-bounded — it
        returns, success or error, within a few seconds).  Primes the RFCOMM
        multiplexer first so the SPP channel actually opens.  Returns the open
        channel, or ``None`` on failure (so the caller can decide whether to
        reset and retry)."""
        for attempt in range(1, attempts + 1):
            self._prime_mux(channel_id)
            delegate = self._delegate_factory()
            status, channel = self._device.openRFCOMMChannelSync_withChannelID_delegate_(
                None, channel_id, delegate
            )
            if status == 0 and channel is not None:
                self._delegate = delegate
                return channel

            log.debug(
                "RFCOMM open channel %d attempt %d failed (IOReturn %s)",
                channel_id,
                attempt,
                _ioreturn(status),
            )
            self._safe_close_channel(channel)
            self._pump(1.0)
        return None

    def _prime_mux(self, channel_id: int) -> None:
        """Establish the RFCOMM multiplexer by opening (and closing) the Apple
        iAP channel, so the subsequent SPP channel open succeeds.  A no-op
        when the target already is the prime channel."""
        if channel_id == self.PRIME_CHANNEL:
            return
        delegate = self._delegate_factory()
        status, channel = self._device.openRFCOMMChannelSync_withChannelID_delegate_(
            None, self.PRIME_CHANNEL, delegate
        )
        if status == 0 and channel is not None:
            log.debug("Primed RFCOMM mux via channel %d", self.PRIME_CHANNEL)
        else:
            log.debug(
                "Priming channel %d open returned IOReturn %s",
                self.PRIME_CHANNEL,
                _ioreturn(status),
            )
        self._safe_close_channel(channel)
        self._pump(0.3)

    @staticmethod
    def _safe_close_channel(channel) -> None:
        if channel is None:
            return
        try:
            channel.closeChannel()
        except Exception:  # pragma: no cover - framework hiccup
            pass

    # -- device resolution --------------------------------------------------

    @classmethod
    def find_devices(cls, name_filter: str = "PT-") -> list[str]:
        """Names of paired Bluetooth devices that look like P-Touch printers."""
        if not HAVE_IOBLUETOOTH:
            return []
        paired = IOBluetooth.IOBluetoothDevice.pairedDevices() or []
        return [
            str(device.name())
            for device in paired
            if device.name() and str(device.name()).startswith(name_filter)
        ]

    @staticmethod
    def _resolve_device(address_or_name: str):
        looks_like_mac = len(address_or_name) == 17 and (
            ":" in address_or_name or "-" in address_or_name
        )
        if looks_like_mac:
            device = IOBluetooth.IOBluetoothDevice.deviceWithAddressString_(
                address_or_name.replace(":", "-")
            )
            if device is not None:
                return device
        paired = IOBluetooth.IOBluetoothDevice.pairedDevices() or []
        for device in paired:
            if device.name() and address_or_name.lower() in str(device.name()).lower():
                return device
        raise LookupError(
            f"no paired Bluetooth device matching {address_or_name!r} "
            f"(paired: {[str(d.name()) for d in paired]})"
        )

    # -- byte stream --------------------------------------------------------

    def write(self, data: bytes) -> None:
        if self._delegate is not None and self._delegate.closed:
            raise ConnectionError(
                f"RFCOMM channel to {self._target} was closed by the peer "
                "(printer powered off or out of range?)"
            )
        mtu = self._channel.getMTU() or 512
        for i in range(0, len(data), mtu):
            chunk = bytes(data[i : i + mtu])
            status = self._channel.writeSync_length_(chunk, len(chunk))
            if status != 0:
                raise ConnectionError(
                    f"RFCOMM write to {self._target} failed "
                    f"(IOReturn {_ioreturn(status)})"
                )

    def read(self, size: int, timeout: float | None = None) -> bytes:
        deadline = time.monotonic() + (timeout if timeout is not None else 10.0)
        while not self._delegate.buf and not self._delegate.closed:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            self._pump(min(0.1, remaining))
        if not self._delegate.buf and self._delegate.closed:
            raise ConnectionError(
                f"RFCOMM channel to {self._target} was closed by the peer "
                "(printer powered off or out of range?)"
            )
        out = bytes(self._delegate.buf[:size])
        del self._delegate.buf[:size]
        return out

    def close(self, *, release_baseband: bool = True) -> None:
        """Close the RFCOMM channel and, by default, drop the baseband link.

        Releasing the baseband means the *next* connect starts from a clean
        disconnected state instead of inheriting a link that may have gone
        stale (e.g. after the printer was power-cycled between jobs).
        """
        try:
            if self._channel is not None:
                self._channel.closeChannel()
                self._pump(0.2)
        except Exception:
            pass
        if release_baseband:
            try:
                if self._device is not None and self._device.isConnected():
                    self._device.closeConnection()
                    self._pump(0.2)
            except Exception:
                pass

    def __repr__(self) -> str:
        return f"IOBluetoothTransport({self.name!r}, channel={self.channel_id})"
