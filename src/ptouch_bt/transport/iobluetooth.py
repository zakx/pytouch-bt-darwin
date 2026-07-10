"""IOBluetooth RFCOMM transport for macOS.

This is the primary transport on macOS: some printers (e.g. PT-E560BT)
expose two RFCOMM channels — Apple iAP on channel 1 and SPP on channel 2
— and the auto-created /dev/cu.* serial node binds the iAP channel,
which silently swallows all data.  Connecting to the SDP-advertised SPP
channel directly always reaches the print engine.

Requires ``pyobjc-framework-IOBluetooth`` and Bluetooth permission for
the terminal app (System Settings > Privacy & Security > Bluetooth).
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
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        CFRunLoopRunInMode(kCFRunLoopDefaultMode, 0.1, False)


if HAVE_IOBLUETOOTH:

    class _RFCOMMDelegate(NSObject):
        def init(self):
            self = objc.super(_RFCOMMDelegate, self).init()
            self.buf = bytearray()
            self.closed = False
            return self

        def rfcommChannelData_data_length_(self, channel, data, length):
            self.buf += bytes(data)

        def rfcommChannelOpenComplete_status_(self, channel, status):
            # Required for openRFCOMMChannelSync to complete reliably.
            pass

        def rfcommChannelClosed_(self, channel):
            self.closed = True


class IOBluetoothTransport(Transport):
    """RFCOMM channel to a paired Bluetooth Classic device."""

    SPP_UUID16 = 0x1101

    def __init__(self, address_or_name: str, channel_id: int | None = None) -> None:
        if not HAVE_IOBLUETOOTH:
            raise ImportError(
                "IOBluetoothTransport needs pyobjc-framework-IOBluetooth "
                "(pip install pyobjc-framework-IOBluetooth)"
            )
        device = self._resolve_device(address_or_name)
        if not device.isConnected():
            status = device.openConnection()
            if status != 0:
                raise ConnectionError(
                    f"baseband connection to {address_or_name} failed "
                    f"(IOReturn 0x{status & 0xffffffff:08x})"
                )
            _pump(1.0)  # let the connection settle before opening a channel
        if channel_id is None:
            channel_id = self._query_rfcomm_channel(device)

        self._delegate = _RFCOMMDelegate.alloc().init()
        last_error = 0
        channel = None
        for attempt in range(3):
            status, channel = device.openRFCOMMChannelSync_withChannelID_delegate_(
                None, channel_id, self._delegate
            )
            if status == 0 and channel is not None:
                break
            last_error = status
            log.debug(
                "RFCOMM open attempt %d failed (IOReturn 0x%08x)",
                attempt + 1,
                status & 0xFFFFFFFF,
            )
            _pump(1.0)
        else:
            raise ConnectionError(
                f"RFCOMM channel {channel_id} open failed "
                f"(IOReturn 0x{last_error & 0xffffffff:08x})"
            )
        self._device = device
        self._channel = channel
        self.name = str(device.name())
        self.channel_id = channel_id

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

    def _query_rfcomm_channel(self, device) -> int:
        """Find the RFCOMM channel of the SPP service (not Apple iAP)."""
        device.performSDPQuery_(None)
        _pump(2.0)
        uuid = IOBluetooth.IOBluetoothSDPUUID.uuid16_(self.SPP_UUID16)
        record = device.getServiceRecordForUUID_(uuid)
        if record is not None:
            ok, channel_id = record.getRFCOMMChannelID_(None)
            if ok == 0:
                log.debug("SDP: SPP service on RFCOMM channel %d", channel_id)
                return channel_id
        log.warning("SDP query found no SPP record; assuming channel 1")
        return 1

    def write(self, data: bytes) -> None:
        mtu = self._channel.getMTU() or 512
        for i in range(0, len(data), mtu):
            chunk = bytes(data[i : i + mtu])
            status = self._channel.writeSync_length_(chunk, len(chunk))
            if status != 0:
                raise ConnectionError(
                    f"RFCOMM write failed (IOReturn 0x{status & 0xffffffff:08x})"
                )

    def read(self, size: int, timeout: float | None = None) -> bytes:
        deadline = time.monotonic() + (timeout if timeout is not None else 10.0)
        while not self._delegate.buf and not self._delegate.closed:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            CFRunLoopRunInMode(kCFRunLoopDefaultMode, min(0.1, remaining), False)
        out = bytes(self._delegate.buf[:size])
        del self._delegate.buf[:size]
        return out

    def close(self) -> None:
        try:
            self._channel.closeChannel()
            _pump(0.2)
        except Exception:
            pass

    def __repr__(self) -> str:
        return f"IOBluetoothTransport({self.name!r}, channel={self.channel_id})"
