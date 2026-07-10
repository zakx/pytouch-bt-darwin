"""Per-model and per-tape parameters for P-Touch raster printing."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class TapeInfo:
    """Printable area of one tape width on a given print head."""

    width_mm: float
    print_area_dots: int  # usable pins, centered on the head

    def left_margin_dots(self, head_dots: int) -> int:
        return (head_dots - self.print_area_dots) // 2


@dataclass(frozen=True)
class DeviceProfile:
    name: str
    head_dots: int  # raster line width in dots
    dpi: int
    tapes: dict[int, TapeInfo]  # keyed by tape width in mm as reported in status
    supports_auto_cut: bool = False
    supports_half_cut: bool = False
    # Newer (2020+) Bluetooth models — PT-D410/D460BT/D610BT/E310BT/E560BT —
    # need a modified command sequence ("D460BT" mode in ptouch-print):
    # info command with page-end byte 0x02, a mandatory ESC i d ... 4D 00
    # packet before raster data, chaining via a leading ESC i K 00 packet,
    # and 0x1A as the only print command.
    d460bt_mode: bool = False
    # Whether TIFF/PackBits raster compression is known to work.
    supports_packbits: bool = True
    min_feed_dots: int = 14  # 2.0 mm at 180 dpi
    min_raster_lines: int = 31  # ~4.4 mm minimum label length

    @property
    def bytes_per_line(self) -> int:
        return self.head_dots // 8

    def tape(self, width_mm: int) -> TapeInfo:
        try:
            return self.tapes[width_mm]
        except KeyError:
            raise ValueError(
                f"{self.name}: unsupported tape width {width_mm} mm "
                f"(supported: {sorted(self.tapes)})"
            ) from None


# 128-dot, 180 dpi heads. Print-area pins per tape from Brother's official
# PT-E550W/P750W raster command reference (§2.3.5); all are centered on the
# head. ptouch-print uses slightly larger areas (12mm: 76, 18mm: 120) —
# stay conservative to avoid clipping at the tape edge.
_TAPES_128 = {
    4: TapeInfo(3.5, 24),
    6: TapeInfo(6, 32),
    9: TapeInfo(9, 50),
    12: TapeInfo(12, 70),
    18: TapeInfo(18, 112),
    24: TapeInfo(24, 128),
}

_EDGE_BT = DeviceProfile(
    # 2023+ P-touch EDGE Bluetooth generation (PT-E560BT and siblings).
    name="PT-E560BT",
    head_dots=128,
    dpi=180,
    tapes=_TAPES_128,
    supports_auto_cut=True,
    supports_half_cut=True,
    d460bt_mode=True,
    supports_packbits=False,  # ptouch-print sends raw raster to these
    min_feed_dots=14,
    min_raster_lines=34,  # 4.8 mm minimum label length
)

PROFILES: dict[str, DeviceProfile] = {
    "PT-E560BT": _EDGE_BT,
    "PT-E510": DeviceProfile(
        name="PT-E510",
        head_dots=128,
        dpi=180,
        tapes=_TAPES_128,
        supports_auto_cut=True,
        supports_half_cut=True,
        d460bt_mode=True,
        supports_packbits=False,
    ),
    "PT-E550W": DeviceProfile(
        name="PT-E550W",
        head_dots=128,
        dpi=180,
        tapes=_TAPES_128,
        supports_auto_cut=True,
        supports_half_cut=True,
    ),
    "PT-P750W": DeviceProfile(
        name="PT-P750W",
        head_dots=128,
        dpi=180,
        tapes=_TAPES_128,
        supports_auto_cut=True,
    ),
    "PT-P710BT": DeviceProfile(
        name="PT-P710BT",
        head_dots=128,
        dpi=180,
        tapes=_TAPES_128,
        supports_auto_cut=True,
    ),
    "PT-P300BT": DeviceProfile(
        name="PT-P300BT",
        head_dots=128,
        dpi=180,
        tapes={
            4: TapeInfo(3.5, 24),
            6: TapeInfo(6, 32),
            9: TapeInfo(9, 50),
            12: TapeInfo(12, 64),
        },
        supports_auto_cut=False,
    ),
}

# The user-facing default: the EDGE Bluetooth generation.
DEFAULT_PROFILE = _EDGE_BT


def profile_for_model_name(name: str) -> DeviceProfile:
    return PROFILES.get(name, DEFAULT_PROFILE)
