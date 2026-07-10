import pytest

from ptouch_bt.status import STATUS_SIZE, PrinterStatus


def make_status(
    model=0x7F,
    err=0,
    tape_width=12,
    tape_type=0x01,
    status_type=0x00,
    phase_type=0,
    phase=0,
):
    data = bytearray(STATUS_SIZE)
    data[0:4] = b"\x80\x20\x42\x30"
    data[4] = model
    data[8:10] = err.to_bytes(2, "big")
    data[10] = tape_width
    data[11] = tape_type
    data[18] = status_type
    data[19] = phase_type
    data[20:22] = phase.to_bytes(2, "big")
    return bytes(data)


def test_parse_ready():
    status = PrinterStatus.parse(make_status())
    assert status.model_name == "PT-E560BT"
    assert status.tape_width_mm == 12
    assert status.is_ready
    assert status.errors == []


def test_parse_errors():
    status = PrinterStatus.parse(make_status(err=0x0101))
    assert not status.is_ready
    assert "Replace media" in status.errors
    assert "Media not loaded" in status.errors


def test_parse_busy_phase():
    status = PrinterStatus.parse(make_status(phase_type=1))
    assert not status.is_ready


def test_bad_magic():
    with pytest.raises(ValueError):
        PrinterStatus.parse(b"\x00" * 32)


def test_bad_length():
    with pytest.raises(ValueError):
        PrinterStatus.parse(b"\x80\x20\x42")


def test_describe_smoke():
    text = PrinterStatus.parse(make_status()).describe()
    assert "PT-E560BT" in text
    assert "12 mm" in text
