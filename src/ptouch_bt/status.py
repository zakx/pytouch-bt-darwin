"""Parsing of the 32-byte Brother P-Touch status response."""

from __future__ import annotations

import struct
from dataclasses import dataclass

STATUS_SIZE = 32
_STATUS_MAGIC = b"\x80\x20\x42"  # 0x80, size 0x20, 'B' (brother), followed by '0'

POWER = {
    0: "Battery full",
    1: "Battery half",
    2: "Battery low",
    3: "Battery critical",
    4: "AC / fully charged",
}

MODELS = {
    0x38: "QL-800",
    0x39: "QL-810W",
    0x41: "QL-820NWB",
    0x66: "PT-E550W",
    0x68: "PT-P750W",
    0x6F: "PT-P900W",
    0x70: "PT-P950NW",
    0x72: "PT-P300BT",
    0x76: "PT-P710BT",
    0x7E: "PT-E510",
    0x7F: "PT-E560BT",
}

ERR_FLAGS = {
    0: "Replace media",
    1: "Expansion buffer full",
    2: "Communication error",
    3: "Communication buffer full",
    4: "Cover opened",
    5: "Overheat / cancelled on printer side",
    6: "Feed error",
    7: "General system error",
    8: "Media not loaded",
    9: "End of media (page too long)",
    10: "Cutter jammed",
    11: "Low battery",
    12: "Printer in use",
    13: "Printer not powered",
    14: "Overvoltage",
    15: "Fan error",
}

TAPE_TYPE = {
    0x00: "Not loaded",
    0x01: "Laminated (TZe)",
    0x03: "Non-laminated (TZeN)",
    0x11: "Heat shrink tube 2:1 (HSe)",
    0x14: "TZe tape",  # reported by PT-E560BT-generation printers
    0x17: "Heat shrink tube 3:1 (HSe)",
    0x4A: "Continuous tape",
    0x4B: "Die-cut labels",
    0xFF: "Unsupported",
}

PHASES = {
    0x000000: "Ready",
    0x000001: "Feed",
    0x010000: "Printing",
    0x010014: "Cover open while receiving",
}

STATUS_TYPE = {
    0x00: "Reply to status request",
    0x01: "Printing completed",
    0x02: "Error occurred",
    0x03: "IF mode finished",
    0x04: "Power off",
    0x05: "Notification",
    0x06: "Phase change",
}

NOTIFICATIONS = {
    0x00: "N/A",
    0x01: "Cover open",
    0x02: "Cover close",
}


def _describe(code: int, table: dict) -> str:
    return f"{table.get(code, 'Unknown')} (0x{code:02x})"


def _describe_flags(flagset: int, table: dict) -> str:
    if not flagset:
        return "None"
    return ", ".join(
        table.get(bit, f"bit{bit}") for bit in range(16) if flagset & (1 << bit)
    )


@dataclass
class PrinterStatus:
    model: int
    country: int
    err2: int
    power: int
    err: int
    tape_width_mm: int
    tape_type: int
    colors: int
    fonts: int
    mode: int
    density: int
    tape_length_mm: int
    status_type: int
    phase_type: int
    phase: int
    notification: int
    expansion_area: int
    tape_bgcolor: int
    tape_fgcolor: int
    hw_settings: int
    raw: bytes

    @classmethod
    def parse(cls, data: bytes) -> "PrinterStatus":
        if len(data) != STATUS_SIZE:
            raise ValueError(
                f"status must be {STATUS_SIZE} bytes, got {len(data)}"
            )
        if data[:3] != _STATUS_MAGIC:
            raise ValueError(f"invalid status magic: {data[:4].hex()}")
        (
            model,
            country,
            err2,
            power,
            err,
            tape_width,
            tape_type,
            colors,
            fonts,
            _sbz0,
            mode,
            density,
            tape_length,
            status_type,
            phase_type,
            phase,
            notification,
            expansion,
            bgcolor,
            fgcolor,
            hw_settings,
        ) = struct.unpack(">4x4BH10BH4BI2x", data)
        return cls(
            model=model,
            country=country,
            err2=err2,
            power=power,
            err=err,
            tape_width_mm=tape_width,
            tape_type=tape_type,
            colors=colors,
            fonts=fonts,
            mode=mode,
            density=density,
            tape_length_mm=tape_length,
            status_type=status_type,
            phase_type=phase_type,
            phase=phase,
            notification=notification,
            expansion_area=expansion,
            tape_bgcolor=bgcolor,
            tape_fgcolor=fgcolor,
            hw_settings=hw_settings,
            raw=bytes(data),
        )

    @property
    def model_name(self) -> str:
        return MODELS.get(self.model, f"Unknown (0x{self.model:02x})")

    @property
    def errors(self) -> list[str]:
        return [
            ERR_FLAGS.get(bit, f"bit{bit}")
            for bit in range(16)
            if self.err & (1 << bit)
        ]

    @property
    def is_ready(self) -> bool:
        return self.err == 0 and self.phase_type == 0 and self.phase == 0

    def describe(self) -> str:
        lines = [
            f"Model:         {self.model_name}",
            f"Power:         {_describe(self.power, POWER)}",
            f"Errors:        {_describe_flags(self.err, ERR_FLAGS)}",
            f"Tape width:    {self.tape_width_mm} mm",
            f"Tape type:     {_describe(self.tape_type, TAPE_TYPE)}",
            f"Status:        {_describe(self.status_type, STATUS_TYPE)}",
            f"Phase:         {_describe(self.phase_type << 16 | self.phase, PHASES)}",
            f"Notification:  {_describe(self.notification, NOTIFICATIONS)}",
        ]
        return "\n".join(lines)
