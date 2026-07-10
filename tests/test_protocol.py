import pytest

from ptouch_bt import protocol


def test_packbits_roundtrip():
    cases = [
        b"",
        b"\x00" * 16,
        b"\xff" * 16,
        b"\x01\x02\x03\x04",
        b"\x00\x00\x00\xaa\xbb\xcc\xcc\xcc\xcc\x00\x00",
        bytes(range(256)),
        b"\xab" * 300,
    ]
    for data in cases:
        encoded = protocol.packbits_encode(data)
        assert protocol.packbits_decode(encoded) == data, data.hex()


def test_packbits_compresses_runs():
    encoded = protocol.packbits_encode(b"\x00" * 16)
    assert len(encoded) == 2
    assert encoded == b"\xf1\x00"  # 257-16=241=0xf1


def test_command_encodings():
    assert protocol.reset() == b"\x1b@"
    assert protocol.get_status() == b"\x1biS"
    assert protocol.use_command_set() == b"\x1bia\x01"
    assert protocol.set_compression(protocol.CompressionType.rle) == b"M\x02"
    assert protocol.set_page_margin(0x1234) == b"\x1bid\x34\x12"
    assert protocol.print_page() == b"\x0c"
    assert protocol.print_and_feed() == b"\x1a"
    assert protocol.zero_line() == b"Z"
    assert protocol.invalidate(3) == b"\x00\x00\x00"


def test_set_print_parameters():
    cmd = protocol.set_print_parameters(
        media_type=protocol.MediaType.laminated,
        width_mm=24,
        length_mm=0,
        raster_lines=200,
    )
    assert cmd[:3] == b"\x1biz"
    body = cmd[3:]
    assert len(body) == 10
    assert body[1] == 0x01  # laminated
    assert body[2] == 24
    assert body[4:8] == (200).to_bytes(4, "little")


def test_raster_line_uncompressed():
    line = b"\x80" + b"\x00" * 15
    cmd = protocol.raster_line(line, compress=False)
    assert cmd == b"G\x10\x00" + line


def test_raster_line_compressed_roundtrip():
    line = b"\xff" * 16
    cmd = protocol.raster_line(line, compress=True)
    assert cmd[0:1] == b"G"
    length = int.from_bytes(cmd[1:3], "little")
    assert protocol.packbits_decode(cmd[3 : 3 + length]) == line


def test_encode_raster_zero_lines():
    data = b"\x00" * 16 + b"\xff" * 16 + b"\x00" * 16
    out = list(protocol.encode_raster(data, 16))
    assert out[0] == b"Z"
    assert out[1][0:1] == b"G"
    assert out[2] == b"Z"


def test_encode_raster_rejects_misaligned():
    with pytest.raises(ValueError):
        list(protocol.encode_raster(b"\x00" * 17, 16))
