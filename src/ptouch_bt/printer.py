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

    def print_images(
        self,
        images: Iterable[Image.Image | str],
        *,
        copies: int = 1,
        dither: bool = True,
        end_margin_dots: int | None = None,
        half_cut: bool = True,
        high_resolution: bool = False,
        compress: bool | None = None,
        dry_run: bool = False,
    ) -> PrinterStatus:
        """Print several *different* labels as one job — a single tape strip
        with a cut between each label and a full cut (eject) at the very end.

        This is what Brother's own apps do: on models with a half cutter the
        between-label cuts are *half* cuts, so the strip stays in one piece
        until you tear the labels apart. On models with only a full cutter the
        labels are separated with full cuts instead; on models with no cutter
        at all they come out as one continuous strip (a warning is logged).

        Each image is scaled so its height fills the loaded tape's printable
        width, exactly like :meth:`print_image`. *copies* repeats the whole
        strip. Returns the final printer status.
        """
        images = list(images)
        if not images:
            raise ValueError("print_images() needs at least one image")
        status = self.get_status()
        if not status.is_ready:
            raise PrinterNotReady(status)
        if status.tape_width_mm == 0:
            raise PTouchError("no tape cassette loaded")

        tape = self.profile.tape(status.tape_width_mm)
        rasters = [
            rasterize.image_to_raster(image, self.profile, tape, dither=dither)
            for image in images
        ]
        return self.print_rasters(
            rasters,
            status=status,
            copies=copies,
            end_margin_dots=end_margin_dots,
            half_cut=half_cut,
            high_resolution=high_resolution,
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

        if status is None:
            status = self.get_status()
            if not status.is_ready:
                raise PrinterNotReady(status)

        if compress is None:
            compress = profile.supports_packbits
        if end_margin_dots is None:
            end_margin_dots = profile.min_feed_dots

        raster, raster_lines = self._prepare_raster(raster)

        self._reset()
        for copy in range(copies):
            last_page = copy == copies - 1
            if profile.d460bt_mode:
                self._send_page_d460bt(
                    raster, raster_lines, status,
                    end_margin_dots=end_margin_dots,
                    # Inner copies chain into one uncut strip; the last page
                    # cuts, unless the caller asked to chain (leave tape uncut).
                    end_of_page="chain" if (chain or not last_page) else "full",
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

    def print_rasters(
        self,
        rasters: list[bytes],
        *,
        status: PrinterStatus | None = None,
        copies: int = 1,
        end_margin_dots: int | None = None,
        half_cut: bool = True,
        high_resolution: bool = False,
        compress: bool | None = None,
        dry_run: bool = False,
    ) -> PrinterStatus:
        """Send several pre-encoded rasters as one multi-page strip.

        The strip-and-cut behaviour is described on :meth:`print_images`;
        this is the lower-level entry point when you already have raster
        bytes. *copies* repeats the whole strip.
        """
        if not rasters:
            raise ValueError("print_rasters() needs at least one raster")
        profile = self.profile

        if status is None:
            status = self.get_status()
            if not status.is_ready:
                raise PrinterNotReady(status)

        if compress is None:
            compress = profile.supports_packbits
        if end_margin_dots is None:
            end_margin_dots = profile.min_feed_dots

        # Capability handling. Half cuts need a half cutter; separating labels
        # at all needs *some* cutter. Degrade loudly rather than silently
        # doing the wrong thing (see devices.py for the per-model flags).
        can_cut = profile.supports_auto_cut
        if half_cut and not profile.supports_half_cut:
            if can_cut:
                log.warning(
                    "%s has no half cutter; separating labels with full cuts",
                    profile.name,
                )
            else:
                log.warning(
                    "%s cannot cut; labels print as one continuous strip",
                    profile.name,
                )
            half_cut = False

        pages = [self._prepare_raster(r) for r in rasters] * copies

        self._reset()
        status_out = status
        for index, (raster, raster_lines) in enumerate(pages):
            last_page = index == len(pages) - 1
            if profile.d460bt_mode:
                # This generation ends every page with 0x1A and selects the cut
                # with a per-page ESC i K packet sent before the raster data:
                # half cut after each inner page (the strip stays in one piece
                # until torn apart) and a full cut after the last. With
                # half_cut off, every page — including the last — full-cuts
                # into its own label. See _send_page_d460bt for the verified
                # byte semantics.
                if half_cut and not last_page:
                    end_of_page = "half"
                elif half_cut and len(pages) == 1:
                    # A one-page half-cut job still wants the leading margin
                    # trimmed: with a 04 packet somewhere in the job the
                    # printer half-cuts the front margin off, but a lone 08
                    # page never engages the half cutter at all. Combine both
                    # bits (ESC i K 0C = half cut + no chaining) so the single
                    # label gets the leading trim AND the full-cut eject.
                    end_of_page = "half+full"
                else:
                    end_of_page = "full"
                self._send_page_d460bt(
                    raster, raster_lines, status,
                    end_margin_dots=end_margin_dots,
                    end_of_page=end_of_page,
                    compress=compress,
                )
            else:
                # Classic generation: the half-cut bit rides in the ESC i K
                # bitmask and the page separator is FF (more pages follow) vs
                # SUB (last page, eject + full cut). Keep chain printing ON for
                # a half-cut strip so the inner FFs don't force a full feed;
                # for full-cut separation turn chaining OFF so each page cuts.
                self._send_page_classic(
                    raster, raster_lines, status,
                    end_margin_dots=end_margin_dots,
                    auto_cut=can_cut,
                    half_cut=half_cut,
                    high_resolution=high_resolution,
                    chain=half_cut,
                    compress=compress,
                    follow_up=index > 0,
                )

            if dry_run:
                log.info("dry run: skipping print commands")
                self._reset()
                return status

            if profile.d460bt_mode or last_page:
                self._send(protocol.print_and_feed())
            else:
                self._send(protocol.print_page())
            log.info("printing label %d/%d ...", index + 1, len(pages))
            status_out = self._wait_for_completion()
        return status_out

    def _prepare_raster(self, raster: bytes) -> tuple[bytes, int]:
        """Pad a raster up to the model's minimum label length.

        Returns ``(raster, raster_line_count)``.
        """
        bytes_per_line = self.profile.bytes_per_line
        raster_lines = len(raster) // bytes_per_line
        if raster_lines < self.profile.min_raster_lines:
            raster = raster + b"\x00" * (
                (self.profile.min_raster_lines - raster_lines) * bytes_per_line
            )
            raster_lines = self.profile.min_raster_lines
        return raster, raster_lines

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
        end_of_page: str,  # "half" | "full" | "chain"
        compress: bool,
    ) -> None:
        """Sequence for the 2020+ generation (PT-D410/D460BT/D610BT/
        E310BT/E560BT), verified byte-for-byte against ptouch-print.

        *end_of_page* selects what happens after this page's trailing 0x1A
        print command: ``"half"`` = half cut (label layer cut, backing
        intact), ``"full"`` = full cut, ``"half+full"`` = half-cut mode with
        a full-cut eject (a one-page job that wants its leading margin
        trimmed), ``"chain"`` = no feed and no cut (the next page joins on,
        or the tape stays in the machine).
        """
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
        # On this generation ESC i K is a one-byte per-page packet sent
        # *before* the raster data (not the classic bitmask sent among the
        # page setup). Hardware-verified on a PT-E560BT (2026-07-11 probe):
        #   ESC i K 04 -> HALF cut after this page
        #   ESC i K 08 -> FULL cut after this page
        #   ESC i K 00 -> chain: no feed, no cut (next page joins on)
        # The packets are strictly per-page, not a sticky job mode — but a
        # page carrying no packet at all does NOT reliably cut once an
        # earlier page in the job chained, so every page sends one
        # explicitly. ESC i M 40 forces a full cut per page even alongside
        # ESC i K 04, so no ESC i M packet is ever sent on this family.
        if end_of_page == "half":
            self._send(
                protocol.set_page_mode_advanced(protocol.PageModeAdvanced.half_cut)
            )
        elif end_of_page == "full":
            self._send(
                protocol.set_page_mode_advanced(
                    protocol.PageModeAdvanced.no_page_chaining
                )
            )
        elif end_of_page == "half+full":
            self._send(
                protocol.set_page_mode_advanced(
                    protocol.PageModeAdvanced.half_cut
                    | protocol.PageModeAdvanced.no_page_chaining
                )
            )
        elif end_of_page == "chain":
            self._send(protocol.d460bt_chain())
        else:
            raise ValueError(f"unknown end_of_page: {end_of_page!r}")
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
