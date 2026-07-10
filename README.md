# ptouch-bt

Print labels on Brother P-Touch Bluetooth label printers from Python, on
macOS and Linux — no official app required.

Developed and tested on the **PT-E560BT** (P-touch EDGE). The library
detects the model and loaded tape from the printer's status response and
picks the right command sequence automatically:

- **New generation** (2020+: PT-D410, PT-D460BT, PT-D610BT, PT-E310BT,
  PT-E510, PT-E560BT): uncompressed raster, print-information command
  with page-end byte `0x02`, mandatory `ESC i d {margin} 4D 00` packet,
  chaining via a leading `ESC i K 00`, always finalized with SUB (0x1A).
- **Classic** (PT-P300BT, PT-P710BT, PT-P750W, PT-E550W era):
  PackBits-compressed raster, page modes via `ESC i M`/`ESC i K`, margin
  via `ESC i d`, FF/SUB print commands.

## Install

```sh
pip install ptouch-bt        # or: uv pip install ptouch-bt
```

From a checkout: `pip install -e .`

## Pair the printer (once)

**macOS**: turn the printer on, open *System Settings > Bluetooth*, and
pair the `PT-…` device. Grant your terminal Bluetooth permission if
prompted (*Privacy & Security > Bluetooth*).

**Linux**: `bluetoothctl` → `scan on` → `pair <addr>`, then
`rfcomm bind 0 <addr> <channel>` to get `/dev/rfcomm0`. Note that the
SPP channel may not be 1 — on the PT-E560BT it is 2 (see below).

## Use

CLI:

```sh
ptouch-bt ports                       # list candidate printers
ptouch-bt status                      # tape width/type, errors, battery
ptouch-bt print -t "Hello world"      # print text
ptouch-bt print -t "line1\nline2"     # multi-line
ptouch-bt print -i label.png          # print an image
ptouch-bt print -i label.png --copies 3 --chain
ptouch-bt print -t "test" --dry-run   # send everything except the print command
```

Library:

```python
from ptouch_bt import PTouchPrinter

with PTouchPrinter.connect() as printer:   # autodetect; or connect("PT-E560BT_0334")
    print(printer.get_status().describe()) # what tape is loaded?
    printer.print_text("Hello world")
    printer.print_image("label.png")       # scaled to the tape height
```

Images are scaled so their height fills the printable area of the loaded
tape (e.g. 70 dots on 12 mm tape at 180 dpi), dithered to 1-bit, and sent
column by column. `print_raster()` accepts pre-encoded 1bpp data if you
need full control.

## macOS notes

- Some printers (confirmed on the PT-E560BT) expose **two** RFCOMM
  channels: Apple iAP on channel 1 and SPP on channel 2. macOS binds the
  auto-created `/dev/cu.PT-…` serial port to the *iAP* channel, which
  silently swallows all data. The library therefore connects via
  IOBluetooth directly to the SDP-advertised SPP channel on macOS, and
  autodetection probes each candidate transport with a status request,
  using the first one that answers.
- The PT-E560BT does not push a "printing completed" status over
  Bluetooth; the library polls after a quiet period instead.
- If the connection stops responding after a disconnect (a known macOS
  SPP quirk), un-pair and re-pair the printer.

## Tape geometry

Printable area depends on the tape cassette (180 dpi, 128-dot head):

| Tape  | Printable dots |
|-------|----------------|
| 24 mm | 128            |
| 18 mm | 112            |
| 12 mm | 70             |
| 9 mm  | 50             |
| 6 mm  | 32             |
| 3.5 mm| 24             |

These are Brother's official (conservative) values; community drivers use
76/120 dots on 12/18 mm tape. Adjust `ptouch_bt/devices.py` if you want
the extra pixels.

## Layout

- `ptouch_bt.protocol` — raster command builders + PackBits RLE
- `ptouch_bt.status` — 32-byte status response parsing
- `ptouch_bt.devices` — per-model head/tape geometry
- `ptouch_bt.rasterize` — image/text → raster columns
- `ptouch_bt.transport` — IOBluetooth (macOS) and pyserial transports
- `ptouch_bt.printer` — high-level `PTouchPrinter`
- `ptouch_bt.cli` — the `ptouch-bt` command

## Development

```sh
uv sync
uv run pytest
```

## Acknowledgements

The protocol implementation draws on
[dogtopus' PT-P300BT gist](https://gist.github.com/dogtopus/64ae743825e42f2bb8ec79cea7ad2057),
[ptouch-print](https://github.com/probonopd/ptouch-print) (documentation
of the D460BT-family command sequence),
[the78mole/ptouch-webapp](https://github.com/the78mole/ptouch-webapp),
and Brother's official PT-E550W/P750W/P710BT raster command reference.
No code was copied from these projects.

## License

MIT
