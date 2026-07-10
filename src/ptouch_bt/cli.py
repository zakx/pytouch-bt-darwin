"""Command-line interface: ``ptouch-bt``."""

from __future__ import annotations

import argparse
import logging
import sys

from .printer import PTouchError, PTouchPrinter
from .transport import find_serial_ports


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="ptouch-bt",
        description="Print labels on Brother P-Touch Bluetooth printers.",
    )
    parser.add_argument(
        "-p",
        "--port",
        help="serial port, Bluetooth MAC address, or device name "
        "(default: autodetect)",
    )
    parser.add_argument("-v", "--verbose", action="store_true")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("ports", help="list candidate printer serial ports")
    sub.add_parser("status", help="query and show printer status")

    p_print = sub.add_parser("print", help="print an image or text")
    source = p_print.add_mutually_exclusive_group(required=True)
    source.add_argument("-i", "--image", help="image file to print")
    source.add_argument("-t", "--text", help="text to print (\\n for multi-line)")
    p_print.add_argument("--font", help="path to a .ttf/.ttc font (text mode)")
    p_print.add_argument("--copies", type=int, default=1)
    p_print.add_argument(
        "--no-dither", action="store_true", help="threshold instead of dithering"
    )
    p_print.add_argument(
        "--margin", type=int, default=None, help="end margin in dots (180 dpi)"
    )
    p_print.add_argument(
        "--auto-cut", action="store_true", help="cut after printing (if supported)"
    )
    p_print.add_argument(
        "--half-cut", action="store_true", help="half-cut (if supported)"
    )
    p_print.add_argument(
        "--chain",
        action="store_true",
        help="don't feed after printing (saves tape between labels)",
    )
    compression = p_print.add_mutually_exclusive_group()
    compression.add_argument(
        "--compress",
        dest="compress",
        action="store_true",
        default=None,
        help="force RLE compression on",
    )
    compression.add_argument(
        "--no-compress",
        dest="compress",
        action="store_false",
        help="force RLE compression off",
    )
    p_print.add_argument(
        "--dry-run",
        action="store_true",
        help="send data but skip the final print command",
    )

    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(message)s",
    )

    if args.command == "ports":
        from .transport.iobluetooth import HAVE_IOBLUETOOTH, IOBluetoothTransport

        found = False
        if HAVE_IOBLUETOOTH:
            for name in IOBluetoothTransport.find_devices():
                print(f"{name}  (paired Bluetooth device)")
                found = True
        for port in find_serial_ports():
            print(f"{port}  (serial port)")
            found = True
        if not found:
            print(
                "No P-Touch printers found.\n"
                "Pair the printer in System Settings > Bluetooth first."
            )
            return 1
        return 0

    try:
        with PTouchPrinter.connect(args.port) as printer:
            if args.command == "status":
                print(printer.get_status().describe())
                return 0

            kwargs = dict(
                copies=args.copies,
                end_margin_dots=args.margin,
                auto_cut=args.auto_cut,
                half_cut=args.half_cut,
                chain=args.chain,
                compress=args.compress,
                dry_run=args.dry_run,
            )
            if args.text is not None:
                text = args.text.replace("\\n", "\n")
                status = printer.print_text(text, font_path=args.font, **kwargs)
            else:
                status = printer.print_image(
                    args.image, dither=not args.no_dither, **kwargs
                )
            print("Printed." if not args.dry_run else "Dry run complete.")
            if args.verbose:
                print(status.describe())
            return 0
    except (PTouchError, FileNotFoundError, TimeoutError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
