"""Brother P-Touch raster protocol ("PTCBP") command builders.

Implements the command set documented in Brother's "Raster Command
Reference" manuals (PT-E550W/P750W/P710BT and siblings).  All functions
return ``bytes`` ready to be written to the printer over any transport
(Bluetooth RFCOMM, USB, serial).

Adapted from https://gist.github.com/dogtopus/64ae743825e42f2bb8ec79cea7ad2057
"""

from __future__ import annotations

import enum
import struct


class CommandSet(enum.IntEnum):
    escp = 0
    raster = 1  # a.k.a. PTCBP
    ptouch_template = 3


class CompressionType(enum.IntEnum):
    none = 0
    rle = 2  # TIFF / PackBits


class MediaType(enum.IntEnum):
    unloaded = 0x00
    laminated = 0x01
    non_laminated = 0x03
    heat_shrink_tube_21 = 0x11
    heat_shrink_tube_31 = 0x17
    continuous_tape = 0x4A
    die_cut_labels = 0x4B
    unknown = 0xFF


class PageMode(enum.IntFlag):
    """ESC i M — "various mode settings"."""

    auto_cut = 1 << 6
    mirror = 1 << 7


class PageModeAdvanced(enum.IntFlag):
    """ESC i K — "advanced mode settings"."""

    half_cut = 1 << 2
    no_page_chaining = 1 << 3
    no_cutting_on_special_tape = 1 << 4
    cut_on_last_label = 1 << 5
    high_resolution = 1 << 6
    preserve_buffer = 1 << 7


class PrintParameterField(enum.IntFlag):
    """Validity flags for set_print_parameters (ESC i z)."""

    media_type = 1 << 1
    width = 1 << 2
    length = 1 << 3
    quality = 1 << 6
    recovery = 1 << 7


# --- PackBits (TIFF) run-length encoding -----------------------------------


def packbits_encode(data: bytes) -> bytes:
    """Encode *data* with TIFF PackBits RLE as used by Brother raster mode."""
    out = bytearray()
    i = 0
    n = len(data)
    while i < n:
        run = 1
        while i + run < n and run < 128 and data[i + run] == data[i]:
            run += 1
        if run >= 2:
            out.append((257 - run) & 0xFF)
            out.append(data[i])
            i += run
        else:
            start = i
            i += 1
            while i < n and (i - start) < 128:
                if i + 1 < n and data[i] == data[i + 1]:
                    break
                i += 1
            out.append(i - start - 1)
            out.extend(data[start:i])
    return bytes(out)


def packbits_decode(data: bytes) -> bytes:
    """Decode TIFF PackBits RLE."""
    out = bytearray()
    i = 0
    n = len(data)
    while i < n:
        ctrl = data[i]
        i += 1
        if ctrl < 128:  # literal run of ctrl+1 bytes
            out.extend(data[i : i + ctrl + 1])
            i += ctrl + 1
        elif ctrl > 128:  # repeat next byte 257-ctrl times
            out.extend(data[i : i + 1] * (257 - ctrl))
            i += 1
        # ctrl == 128: no-op
    return bytes(out)


# --- Command builders -------------------------------------------------------


def invalidate(count: int = 100) -> bytes:
    """NUL padding that flushes/clears any partial command in the buffer."""
    return b"\x00" * count


def reset() -> bytes:
    """ESC @ — initialize."""
    return b"\x1b@"


def get_status() -> bytes:
    """ESC i S — request a 32-byte status response."""
    return b"\x1biS"


def use_command_set(command_set: CommandSet = CommandSet.raster) -> bytes:
    """ESC i a — switch dynamic command mode (0=ESC/P, 1=raster)."""
    return b"\x1bia" + bytes([command_set])


def set_print_parameters(
    *,
    media_type: int = MediaType.unloaded,
    width_mm: int = 0,
    length_mm: int = 0,
    raster_lines: int = 0,
    page_flag: int = 0,
    active_fields: PrintParameterField = (
        PrintParameterField.width
        | PrintParameterField.quality
        | PrintParameterField.recovery
    ),
) -> bytes:
    """ESC i z — print information command (media, size, raster count).

    *page_flag* (byte n9): 0 = starting page, 1 = follow-up page on classic
    models; on the D460BT family it must be 2 ("feed the last of the label
    and properly stop printing").
    """
    return b"\x1biz" + struct.pack(
        "<4BI2B",
        int(active_fields),
        int(media_type),
        width_mm,
        length_mm,
        raster_lines,
        page_flag,
        0,
    )


def d460bt_margin_magic(margin_dots: int = 14) -> bytes:
    """ESC i d {n1} {n2} 0x4D 0x00 — mandatory on the D460BT family.

    Replaces the classic 2-byte margin command; must be sent after the
    print information command and before the raster data, or the printer
    feeds blank tape. n1/n2 = end margin in dots (little endian), n3 must
    be 0x4D. (Source: ptouch-print, ptouch_send_d460bt_magic.)
    """
    return b"\x1bid" + struct.pack("<H", margin_dots) + b"\x4d\x00"


def d460bt_chain() -> bytes:
    """ESC i K 0x00 — enables chaining on the D460BT family.

    Sent before the raster data; when present, the final 0x1A print
    command neither feeds nor cuts.
    """
    return b"\x1biK\x00"


def set_page_mode(flags: PageMode | int = 0) -> bytes:
    """ESC i M — auto-cut / mirror flags."""
    return b"\x1biM" + bytes([int(flags) & 0xFF])


def set_page_mode_advanced(flags: PageModeAdvanced | int = 0) -> bytes:
    """ESC i K — chaining / half-cut / hi-res flags."""
    return b"\x1biK" + bytes([int(flags) & 0xFF])


def set_page_margin(dots: int) -> bytes:
    """ESC i d — feed amount (margin) in dots."""
    return b"\x1bid" + struct.pack("<H", dots)


def set_compression(compression: CompressionType) -> bytes:
    """M — select raster data compression."""
    return b"M" + bytes([int(compression)])


def raster_line(line: bytes, compress: bool = True) -> bytes:
    """G — one raster line of image data (optionally PackBits-compressed)."""
    payload = packbits_encode(line) if compress else line
    return b"G" + struct.pack("<H", len(payload)) + payload


def zero_line() -> bytes:
    """Z — an all-zero raster line."""
    return b"Z"


def print_page() -> bytes:
    """FF — print the buffered page, more pages follow (chaining)."""
    return b"\x0c"


def print_and_feed() -> bytes:
    """SUB (0x1A) — print the buffered page as the last page and feed."""
    return b"\x1a"


def encode_raster(
    data: bytes,
    bytes_per_line: int,
    compress: bool = True,
    use_zero_lines: bool = True,
):
    """Yield the wire encoding for raw 1bpp raster *data*.

    *data* is a sequence of raster lines, each ``bytes_per_line`` long,
    left-aligned to the print head (MSB of byte 0 = first pin).
    *use_zero_lines* replaces blank lines with the 1-byte Z command;
    disable for the D460BT family, where known-good drivers send full lines.
    """
    if len(data) % bytes_per_line:
        raise ValueError(
            f"raster data length {len(data)} is not a multiple of {bytes_per_line}"
        )
    zero = b"\x00" * bytes_per_line
    for i in range(0, len(data), bytes_per_line):
        chunk = data[i : i + bytes_per_line]
        if use_zero_lines and chunk == zero:
            yield zero_line()
        else:
            yield raster_line(chunk, compress)
