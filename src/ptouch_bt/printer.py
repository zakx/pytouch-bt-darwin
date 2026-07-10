"""High-level P-Touch printer interface."""

from __future__ import annotations

import logging
import time
from typing import Iterable

from PIL import Image

from . import protocol, rasterize
from .devices import DEFAULT_PROFILE, DeviceProfile, profile_for_model_name
from .status import STATUS_SIZE, PrinterStatus
from .transport import SerialTransport, Transport, find_serial_ports

log = logging.getLogger(__name__)


class PTouchError(RuntimeError):
    pass


class PrinterNotReady(PTouchError):
    def __init__(self, status: PrinterStatus) -> None:
        problems = ", ".join(status.errors) or "busy"
        super().__init__(f"printer not ready: {problems}")
        self.status = status


class PTouchPrinter:
    """A Brother P-Touch label printer speaking the raster protocol.

    Usage::

        with PTouchPrinter.connect() as printer:          # autodetect port
            printer.print_image("label.png")

        with PTouchPrinter.connect("PT-E560BT_0334") as printer:
            printer.print_text("Hello world")
    """

    def __init__(
        self,
        transport: Transport,
        profile: DeviceProfile | None = None,
    ) -> None:
        self.transport = transport
        self._profile = profile
        self._status: PrinterStatus | None = None

    @classmethod
    def connect(cls, target: str | None = None, *, timeout: float = 10.0) -> "PTouchPrinter":
        """Connect to a printer and verify it responds.

        *target* may be a serial port (``/dev/...``), a Bluetooth MAC
        address, a device name, or ``None`` to autodetect.  Each candidate
        transport is probed with a status request; the first one that
        answers wins.  (Probing matters: e.g. the PT-E560BT's auto-created
        macOS serial port is bound to the wrong RFCOMM channel and
        swallows data silently.)
        """
        errors: list[str] = []
        for describe, factory in cls._candidate_transports(target, timeout):
            try:
                transport = factory()
            except Exception as exc:
                log.debug("%s unavailable: %s", describe, exc)
                errors.append(f"{describe}: {exc}")
                continue
            printer = cls(transport)
            try:
                printer.get_status()
            except (TimeoutError, OSError, ValueError) as exc:
                log.info("%s did not respond (%s); trying next", describe, exc)
                errors.append(f"{describe}: no response")
                transport.close()
                continue
            log.info("connected via %r", transport)
            return printer
        detail = "; ".join(errors) if errors else "no candidate transports found"
        raise PTouchError(
            f"could not reach a printer ({detail}). Is it on and paired? "
            "(macOS: System Settings > Bluetooth)"
        )

    @staticmethod
    def _candidate_transports(target: str | None, timeout: float):
        from .transport.iobluetooth import HAVE_IOBLUETOOTH, IOBluetoothTransport

        if target is not None:
            if target.startswith("/dev/") or target.upper().startswith("COM"):
                yield (
                    f"serial port {target}",
                    lambda: SerialTransport(target, timeout=timeout),
                )
            else:
                yield (
                    f"bluetooth device {target}",
                    lambda: IOBluetoothTransport(target),
                )
            return
        # Autodetect: direct RFCOMM (SDP-resolved SPP channel) first, then
        # any serial ports.
        if HAVE_IOBLUETOOTH:
            for name in IOBluetoothTransport.find_devices():
                yield (
                    f"bluetooth device {name}",
                    lambda name=name: IOBluetoothTransport(name),
                )
        for port in find_serial_ports():
            yield (
                f"serial port {port}",
                lambda port=port: SerialTransport(port, timeout=timeout),
            )

    # -- session helpers ----------------------------------------------------

    def _send(self, *commands: bytes) -> None:
        for command in commands:
            self.transport.write(command)

    def _reset(self) -> None:
        self._send(
            protocol.invalidate(),
            protocol.reset(),
            protocol.use_command_set(protocol.CommandSet.raster),
        )

    def get_status(self) -> PrinterStatus:
        """Query the printer for its current status (tape, errors, ...)."""
        self._reset()
        self._send(protocol.get_status())
        raw = self.transport.read_exact(STATUS_SIZE, timeout=10.0)
        self._status = PrinterStatus.parse(raw)
        if self._profile is None:
            self._profile = profile_for_model_name(self._status.model_name)
            log.info("detected %s", self._status.model_name)
        return self._status

    @property
    def profile(self) -> DeviceProfile:
        return self._profile or DEFAULT_PROFILE

    # -- printing -----------------------------------------------------------

    def print_image(
        self,
        image: Image.Image | str,
        *,
        copies: int = 1,
        dither: bool = True,
        end_margin_dots: int | None = None,
        auto_cut: bool = False,
        half_cut: bool = False,
        high_resolution: bool = False,
        chain: bool = False,
        compress: bool | None = None,
        dry_run: bool = False,
    ) -> PrinterStatus:
        """Print *image* (path or PIL image) on the currently loaded tape.

        The image is scaled so its height fills the tape's printable width.
        Returns the final printer status.
        """
        status = self.get_status()
        if not status.is_ready:
            raise PrinterNotReady(status)
        if status.tape_width_mm == 0:
            raise PTouchError("no tape cassette loaded")

        tape = self.profile.tape(status.tape_width_mm)
        raster = rasterize.image_to_raster(image, self.profile, tape, dither=dither)
        return self.print_raster(
            raster,
            status=status,
            copies=copies,
            end_margin_dots=end_margin_dots,
            auto_cut=auto_cut,
            half_cut=half_cut,
            high_resolution=high_resolution,
            chain=chain,
            compress=compress,
            dry_run=dry_run,
        )

    def print_text(self, text: str, *, font_path: str | None = None, **kwargs) -> PrinterStatus:
        """Render *text* with a system font and print it."""
        status = self.get_status()
        if not status.is_ready:
            raise PrinterNotReady(status)
        tape = self.profile.tape(status.tape_width_mm)
        image = rasterize.text_to_image(text, tape.print_area_dots, font_path=font_path)
        return self.print_image(image, dither=False, **kwargs)

    def print_raster(
        self,
        raster: bytes,
        *,
        status: PrinterStatus | None = None,
        copies: int = 1,
        end_margin_dots: int | None = None,
        auto_cut: bool = False,
        half_cut: bool = False,
        high_resolution: bool = False,
        chain: bool = False,
        compress: bool | None = None,
        dry_run: bool = False,
    ) -> PrinterStatus:
        """Send pre-encoded raw raster data (head-aligned 1bpp columns).

        *compress* defaults to whatever is known to work on the detected
        model (PackBits on classic models, raw on the 2020+ EDGE/D-series).
        """
        profile = self.profile
        bytes_per_line = profile.bytes_per_line
        raster_lines = len(raster) // bytes_per_line

        if status is None:
            status = self.get_status()
            if not status.is_ready:
                raise PrinterNotReady(status)

        if compress is None:
            compress = profile.supports_packbits
        if end_margin_dots is None:
            end_margin_dots = profile.min_feed_dots

        # Pad very short labels up to the model's minimum length.
        if raster_lines < profile.min_raster_lines:
            raster = raster + b"\x00" * (
                (profile.min_raster_lines - raster_lines) * bytes_per_line
            )
            raster_lines = profile.min_raster_lines

        self._reset()
        for copy in range(copies):
            last_page = copy == copies - 1
            if profile.d460bt_mode:
                self._send_page_d460bt(
                    raster, raster_lines, status,
                    end_margin_dots=end_margin_dots,
                    auto_cut=auto_cut,
                    chain=chain or not last_page,
                    compress=compress,
                )
            else:
                self._send_page_classic(
                    raster, raster_lines, status,
                    end_margin_dots=end_margin_dots,
                    auto_cut=auto_cut,
                    half_cut=half_cut,
                    high_resolution=high_resolution,
                    chain=chain,
                    compress=compress,
                    follow_up=copy > 0,
                )

            if dry_run:
                log.info("dry run: skipping print command")
                self._reset()
                return status

            if profile.d460bt_mode or last_page:
                self._send(protocol.print_and_feed())
            else:
                self._send(protocol.print_page())
            log.info("printing copy %d/%d ...", copy + 1, copies)
            status = self._wait_for_completion()
        return status

    def _send_page_classic(
        self,
        raster: bytes,
        raster_lines: int,
        status: PrinterStatus,
        *,
        end_margin_dots: int,
        auto_cut: bool,
        half_cut: bool,
        high_resolution: bool,
        chain: bool,
        compress: bool,
        follow_up: bool,
    ) -> None:
        """Sequence for PT-P300BT/P710BT/E550W-era printers (per the
        official raster command reference)."""
        page_mode = protocol.PageMode(0)
        if auto_cut and self.profile.supports_auto_cut:
            page_mode |= protocol.PageMode.auto_cut
        advanced = protocol.PageModeAdvanced(0)
        if not chain:
            advanced |= protocol.PageModeAdvanced.no_page_chaining
        if half_cut and self.profile.supports_half_cut:
            advanced |= protocol.PageModeAdvanced.half_cut
        if high_resolution:
            advanced |= protocol.PageModeAdvanced.high_resolution

        self._send(
            protocol.set_print_parameters(
                media_type=status.tape_type,
                width_mm=status.tape_width_mm,
                length_mm=status.tape_length_mm,
                raster_lines=raster_lines,
                page_flag=1 if follow_up else 0,
            ),
            protocol.set_page_mode_advanced(advanced),
            protocol.set_page_mode(page_mode),
            protocol.set_page_margin(end_margin_dots),
            protocol.set_compression(
                protocol.CompressionType.rle
                if compress
                else protocol.CompressionType.none
            ),
        )
        for line in protocol.encode_raster(
            raster, self.profile.bytes_per_line, compress
        ):
            self.transport.write(line)

    def _send_page_d460bt(
        self,
        raster: bytes,
        raster_lines: int,
        status: PrinterStatus,
        *,
        end_margin_dots: int,
        auto_cut: bool,
        chain: bool,
        compress: bool,
    ) -> None:
        """Sequence for the 2020+ generation (PT-D410/D460BT/D610BT/
        E310BT/E560BT), verified byte-for-byte against ptouch-print."""
        if compress:
            self._send(protocol.set_compression(protocol.CompressionType.rle))
        self._send(
            protocol.set_print_parameters(
                media_type=0,  # ptouch-print leaves type/flags at 0 here
                width_mm=status.tape_width_mm,
                length_mm=0,
                raster_lines=raster_lines,
                page_flag=2,  # required to feed & stop properly
                active_fields=protocol.PrintParameterField(0),
            ),
            protocol.d460bt_margin_magic(end_margin_dots),
        )
        if auto_cut:
            self._send(protocol.set_page_mode(protocol.PageMode.auto_cut))
        if chain:
            self._send(protocol.d460bt_chain())
        for line in protocol.encode_raster(
            raster,
            self.profile.bytes_per_line,
            compress,
            use_zero_lines=False,
        ):
            self.transport.write(line)

    def _wait_for_completion(
        self, timeout: float = 90.0, quiet_poll: float = 10.0
    ) -> PrinterStatus:
        """Wait for the "printing completed" status after a print command.

        Printers normally push status updates on their own; if nothing
        arrives for *quiet_poll* seconds, actively request one.
        """
        deadline = time.monotonic() + timeout
        buf = bytearray()
        last_data = time.monotonic()
        while time.monotonic() < deadline:
            chunk = self.transport.read(STATUS_SIZE - len(buf), timeout=1.0)
            if chunk:
                buf.extend(chunk)
                last_data = time.monotonic()
                if len(buf) < STATUS_SIZE:
                    continue
                status = PrinterStatus.parse(bytes(buf))
                buf.clear()
                log.debug("status during print: type=0x%02x", status.status_type)
                if status.status_type == 0x02 or status.err:
                    raise PrinterNotReady(status)
                if status.status_type == 0x01:  # printing completed
                    return status
                if status.status_type == 0x00 and status.is_ready:
                    return status  # poll reply: idle again
                # phase change etc. — keep waiting
            elif time.monotonic() - last_data > quiet_poll:
                log.debug("no status pushed; polling printer")
                self._send(protocol.get_status())
                last_data = time.monotonic()
        raise TimeoutError("timed out waiting for print completion")

    def close(self) -> None:
        self.transport.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
