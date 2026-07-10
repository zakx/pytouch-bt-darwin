"""ptouch-bt — print labels on Brother P-Touch Bluetooth printers.

Quick start (after pairing the printer in System Settings > Bluetooth)::

    from ptouch_bt import PTouchPrinter

    with PTouchPrinter.connect() as printer:
        print(printer.get_status().describe())
        printer.print_text("Hello world")
        printer.print_image("label.png")
"""

from .devices import PROFILES, DeviceProfile, TapeInfo
from .printer import PrinterNotReady, PTouchError, PTouchPrinter
from .status import PrinterStatus
from .transport import SerialTransport, Transport, find_serial_ports

__version__ = "0.1.0"

__all__ = [
    "PTouchPrinter",
    "PTouchError",
    "PrinterNotReady",
    "PrinterStatus",
    "DeviceProfile",
    "TapeInfo",
    "PROFILES",
    "Transport",
    "SerialTransport",
    "find_serial_ports",
    "__version__",
]
