"""End-to-end tests of PTouchPrinter against a scripted fake transport."""

from PIL import Image

from ptouch_bt.printer import PrinterNotReady, PTouchPrinter
from ptouch_bt.transport import Transport

from test_status import make_status


class FakeTransport(Transport):
    """Collects writes; replies to get_status and print commands."""

    def __init__(self, status=None, ready=True):
        self.written = bytearray()
        self.pending = bytearray()
        self.status = status or make_status()
        self.ready = ready
        self.print_commands = 0

    def write(self, data: bytes) -> None:
        self.written.extend(data)
        if data == b"\x1biS":
            self.pending.extend(
                self.status if self.ready else make_status(err=0x0100)
            )
        elif data in (b"\x0c", b"\x1a"):
            self.print_commands += 1
            # phase change -> printing completed
            self.pending.extend(make_status(status_type=0x06, phase_type=1))
            self.pending.extend(make_status(status_type=0x01))

    def read(self, size: int, timeout: float | None = None) -> bytes:
        out = bytes(self.pending[:size])
        del self.pending[:size]
        return out

    def close(self) -> None:
        pass


def label_image(height=70):
    image = Image.new("L", (100, height), 255)
    for x in range(100):
        image.putpixel((x, height // 2), 0)
    return image


def test_get_status():
    printer = PTouchPrinter(FakeTransport())
    status = printer.get_status()
    assert status.model_name == "PT-E560BT"
    assert printer.profile.head_dots == 128
    assert printer.profile.d460bt_mode


def test_print_image_d460bt_sequence():
    # default fake model 0x7F = PT-E560BT -> new-generation sequence
    transport = FakeTransport(status=make_status(tape_width=12))
    printer = PTouchPrinter(transport)
    printer.print_image(label_image(), dither=False)

    wire = bytes(transport.written)
    assert b"\x1b@" in wire  # reset
    assert b"\x1bia\x01" in wire  # raster mode
    assert b"\x1bid\x0e\x00\x4d\x00" in wire  # D460BT margin magic
    assert b"M\x02" not in wire  # no compression on this generation
    assert b"Z" not in wire  # no zero-line shorthand either
    assert b"\x1biK" not in wire  # no chain packet when not chaining
    assert wire.endswith(b"\x1a")  # always finalize with print & feed
    assert transport.print_commands == 1

    # info command: width mm, raster line count, page-end byte = 0x02
    idx = wire.index(b"\x1biz")
    params = wire[idx + 3 : idx + 13]
    assert params[0] == 0  # no active fields (matches ptouch-print)
    assert params[2] == 12  # width_mm
    assert int.from_bytes(params[4:8], "little") == 100
    assert params[8] == 0x02

    # uncompressed full-width raster lines
    assert b"G\x10\x00" in wire


def test_print_image_classic_sequence():
    transport = FakeTransport(status=make_status(model=0x72, tape_width=12))
    printer = PTouchPrinter(transport)
    printer.print_image(label_image(), dither=False)

    wire = bytes(transport.written)
    assert printer.profile.name == "PT-P300BT"
    assert b"M\x02" in wire  # RLE compression
    assert b"\x1biK\x08" in wire  # no-chaining advanced mode
    assert b"\x1bid\x0e\x00" in wire  # margin (14 dots)
    assert b"\x1bid\x0e\x00\x4d\x00" not in wire  # no D460BT magic
    assert wire.endswith(b"\x1a")
    idx = wire.index(b"\x1biz")
    params = wire[idx + 3 : idx + 13]
    assert params[2] == 12
    assert params[8] == 0x00  # classic first-page flag


def test_print_two_copies_d460bt():
    transport = FakeTransport(status=make_status(tape_width=12))
    printer = PTouchPrinter(transport)
    printer.print_image(label_image(), copies=2, dither=False)
    wire = bytes(transport.written)
    assert transport.print_commands == 2
    assert wire.count(b"\x1a") == 2  # each copy finalized with eject
    assert wire.count(b"\x1biK\x00") == 1  # chain packet on first copy only
    assert wire.endswith(b"\x1a")


def test_print_two_copies_classic():
    transport = FakeTransport(status=make_status(model=0x72, tape_width=12))
    printer = PTouchPrinter(transport)
    printer.print_image(label_image(), copies=2, dither=False)
    wire = bytes(transport.written)
    assert b"\x0c" in wire  # first page: FF
    assert wire.endswith(b"\x1a")  # last page: print & feed
    assert transport.print_commands == 2


def test_short_label_padded_to_minimum():
    transport = FakeTransport(status=make_status(tape_width=12))
    printer = PTouchPrinter(transport)
    printer.print_image(label_image().crop((0, 0, 10, 70)), dither=False)
    wire = bytes(transport.written)
    idx = wire.index(b"\x1biz")
    lines = int.from_bytes(wire[idx + 7 : idx + 11], "little")
    assert lines == printer.profile.min_raster_lines


def test_not_ready_raises():
    transport = FakeTransport(ready=False)
    printer = PTouchPrinter(transport)
    try:
        printer.print_image(label_image())
    except PrinterNotReady as exc:
        assert "not ready" in str(exc)
    else:
        raise AssertionError("expected PrinterNotReady")


def test_dry_run_sends_no_print_command():
    transport = FakeTransport(status=make_status(tape_width=12))
    printer = PTouchPrinter(transport)
    printer.print_image(label_image(), dry_run=True, dither=False)
    assert transport.print_commands == 0


def test_print_text():
    transport = FakeTransport(status=make_status(tape_width=12))
    printer = PTouchPrinter(transport)
    printer.print_text("Hello")
    assert transport.print_commands == 1
