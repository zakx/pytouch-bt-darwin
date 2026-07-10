from PIL import Image

from ptouch_bt.devices import PROFILES
from ptouch_bt.rasterize import image_to_raster, text_to_image

PROFILE = PROFILES["PT-E560BT"]


def test_black_image_fills_print_area():
    tape = PROFILE.tape(12)
    image = Image.new("L", (100, tape.print_area_dots), 0)  # all black
    raster = image_to_raster(image, PROFILE, tape, dither=False, resize=False)
    assert len(raster) == 100 * PROFILE.bytes_per_line

    lines = [
        raster[i : i + PROFILE.bytes_per_line]
        for i in range(0, len(raster), PROFILE.bytes_per_line)
    ]
    for line in lines:
        bits = "".join(f"{b:08b}" for b in line)
        assert bits.count("1") == tape.print_area_dots
        # printable area starts at the tape's left margin
        assert bits.index("1") == tape.left_margin_dots(PROFILE.head_dots)


def test_white_image_is_empty():
    tape = PROFILE.tape(24)
    image = Image.new("L", (50, tape.print_area_dots), 255)
    raster = image_to_raster(image, PROFILE, tape, dither=False, resize=False)
    assert raster == b"\x00" * (50 * PROFILE.bytes_per_line)


def test_resize_scales_to_tape():
    tape = PROFILE.tape(24)
    image = Image.new("L", (256, 64), 0)
    raster = image_to_raster(image, PROFILE, tape, dither=False)
    # 64 -> 128 tall means 256 -> 512 wide = 512 raster lines
    assert len(raster) == 512 * PROFILE.bytes_per_line


def test_orientation_left_edge_prints_first():
    tape = PROFILE.tape(24)
    # black only in the leftmost column of the label
    image = Image.new("L", (10, tape.print_area_dots), 255)
    for y in range(tape.print_area_dots):
        image.putpixel((0, y), 0)
    raster = image_to_raster(image, PROFILE, tape, dither=False, resize=False)
    lines = [
        raster[i : i + PROFILE.bytes_per_line]
        for i in range(0, len(raster), PROFILE.bytes_per_line)
    ]
    assert lines[0] != b"\x00" * PROFILE.bytes_per_line
    for line in lines[1:]:
        assert line == b"\x00" * PROFILE.bytes_per_line


def test_transparency_flattened_to_white():
    tape = PROFILE.tape(24)
    image = Image.new("RGBA", (20, tape.print_area_dots), (0, 0, 0, 0))
    raster = image_to_raster(image, PROFILE, tape, dither=False, resize=False)
    assert raster == b"\x00" * (20 * PROFILE.bytes_per_line)


def test_too_tall_image_rejected_without_resize():
    tape = PROFILE.tape(6)
    image = Image.new("L", (10, 200), 0)
    try:
        image_to_raster(image, PROFILE, tape, resize=False)
    except ValueError as exc:
        assert "exceeds" in str(exc)
    else:
        raise AssertionError("expected ValueError")


def test_text_to_image():
    image = text_to_image("Hi", 70)
    assert image.height == 70
    assert image.width > 0
    # has some black pixels
    assert image.getextrema()[0] == 0


def test_text_multiline():
    image = text_to_image("a\nb", 70)
    assert image.height == 70
