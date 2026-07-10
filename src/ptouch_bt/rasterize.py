"""Convert images (and text) to P-Touch raster data.

Raster data layout: the label is printed column by column.  Each raster
line is one printed column, ``head_dots`` bits wide (16 bytes for a
128-dot head), MSB first.  Bit = 1 means "print" (black).

Input images are expected in natural label orientation: wider than tall,
with the image height mapping across the tape width.
"""

from __future__ import annotations

from PIL import Image, ImageDraw, ImageFont, ImageOps

from .devices import DeviceProfile, TapeInfo


def image_to_raster(
    image: Image.Image | str,
    profile: DeviceProfile,
    tape: TapeInfo,
    *,
    dither: bool = True,
    threshold: int = 128,
    resize: bool = True,
) -> bytes:
    """Convert *image* to raw raster bytes for *tape* on *profile*.

    The image is scaled (if *resize*) so its height fills the tape's
    printable area, converted to 1bpp, centered on the print head and
    rotated into column order.  Returns ``raster_lines * bytes_per_line``
    bytes.
    """
    if isinstance(image, str):
        image = Image.open(image)

    # Flatten transparency onto white before thresholding.
    if image.mode in ("RGBA", "LA", "PA"):
        background = Image.new("RGBA", image.size, (255, 255, 255, 255))
        image = Image.alpha_composite(background, image.convert("RGBA"))
    gray = image.convert("L")

    if resize and gray.height != tape.print_area_dots:
        new_width = max(1, round(gray.width * tape.print_area_dots / gray.height))
        gray = gray.resize((new_width, tape.print_area_dots), Image.LANCZOS)
    elif gray.height > tape.print_area_dots:
        raise ValueError(
            f"image height {gray.height}px exceeds printable area "
            f"{tape.print_area_dots}px for {tape.width_mm}mm tape"
        )

    # Black pixels print: invert so 1-bits are black, then to 1bpp.
    inverted = ImageOps.invert(gray)
    if dither:
        mono = inverted.convert("1", dither=Image.FLOYDSTEINBERG)
    else:
        mono = inverted.point(lambda p: 255 if p >= (255 - threshold) else 0).convert(
            "1", dither=Image.NONE
        )

    # Pad across the head: printable area is centered on the print head.
    head = profile.head_dots
    pad_top = tape.left_margin_dots(head) + (tape.print_area_dots - mono.height) // 2
    padded = Image.new("1", (mono.width, head), 0)
    padded.paste(mono, (0, pad_top))

    # Rotate so each raster line (row) is one printed column.  The head
    # prints bit 7 of byte 0 first; after rotate(-90) row y=0 holds the
    # image's left edge, mirrored so pin order matches top-of-label.
    rotated = padded.rotate(-90, expand=True)
    rotated = ImageOps.mirror(rotated)
    return rotated.tobytes()


def text_to_image(
    text: str,
    height_px: int,
    *,
    font_path: str | None = None,
    padding_px: int = 4,
) -> Image.Image:
    """Render *text* as a black-on-white image *height_px* tall.

    Supports multiple lines separated by ``\\n``.
    """
    lines = text.split("\n")
    line_height = (height_px - 2 * padding_px) // len(lines)
    font = _load_font(font_path, line_height)

    # Measure.
    probe = ImageDraw.Draw(Image.new("L", (1, 1)))
    widths = [probe.textbbox((0, 0), line, font=font)[2] for line in lines]
    width = max(widths) + 2 * padding_px

    image = Image.new("L", (max(width, 1), height_px), 255)
    draw = ImageDraw.Draw(image)
    y = padding_px
    for line in lines:
        draw.text((padding_px, y), line, font=font, fill=0)
        y += line_height
    return image


def _load_font(font_path: str | None, size: int) -> ImageFont.FreeTypeFont:
    candidates = (
        [font_path]
        if font_path
        else [
            "/System/Library/Fonts/Helvetica.ttc",
            "/System/Library/Fonts/Supplemental/Arial.ttf",
            "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        ]
    )
    for candidate in candidates:
        try:
            return ImageFont.truetype(candidate, size)
        except OSError:
            continue
    return ImageFont.load_default(size)
