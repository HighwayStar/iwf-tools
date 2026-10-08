# iwf-tools

Tools to unpack, edit and repack **watch faces for the realme Watch 5** — and for other
Ido/VeryFit-chipset watches that use the same `.iwf` "watch plate" format.

The format was reverse-engineered from BLE captures of the realme Link and VeryFit apps
uploading faces to a real watch, and from the vendor's own native face builder
(`libVeryFitMulti.so`). Faces built with these tools have been uploaded and run on the watch.

| file | what it is |
|---|---|
| `iwf.py` | the `.iwf` archive and its FastLZ `.iwf.lz` transfer form: `list`, `unpack`, `pack` |
| `iwf_pic.py` | the picture codec under it: `info`, `decode`, `encode`, `decode-dir`, `encode-dir`, and `compose` (renders a whole face to a PNG preview) |
| `iwf_gui.py` | a Tk layout editor: live preview, drag-to-move, add/remove widgets, per-widget JSON, pack |

`iwf_gui.py` imports the other two, so keep the three files together.

## Requirements

- Python 3
- [Pillow](https://pypi.org/project/pillow/) (`pip install pillow`)
- tkinter, for the editor only (`python3-tk` / `python3-tkinter` on most distros)

No other dependencies — the FastLZ and LZ4 codecs are implemented in pure Python.

## Unpack

    python3 iwf.py unpack face.iwf myface          # a plain .iwf or a packed .iwf.lz

`myface/` comes out in the layout the vendor app itself keeps on the phone —
the one its native builder reads: `iwf.json` and `font.json` and the background
and preview PNGs at the top, one folder per font with that font's strips
(`g12/0_24bit.png`, `month/en_sept_24bit.png`). Pictures are decoded, so the
tree is editable as it stands. `--raw` writes the member bytes verbatim
instead, for byte-exact forensics.

## Modify

- **pictures** — edit any PNG in the tree (keep the canvas size, or change it
  only if you also change the widget's `w`/`h`). An RGBA PNG encodes as a font
  strip (per-pixel alpha), an RGB PNG as a background/preview.
- **layout** — `iwf.json` by hand, or with the editor:
  `python3 iwf_gui.py myface` — click to select, drag to move, add/remove
  widgets, edit anything else in the per-widget JSON box, and pack straight
  from the window.
- **member names are the watch's lookup keys** — the folder and file names
  under each font directory are how `pack` rebuilds them; renaming a strip
  changes which member it becomes.

The picture codec is picked per member the way the vendor's builder picks it:
`_24bit`/`_lz4`-named members (the Watch 5's faces) are LZ4-of-RGB565+A8, all
others (faces from other watches, e.g. a 320x385 dial) are uncompressed
big-endian RGB565 with a 4-bit alpha plane on font strips — both directions
handled automatically, and the uncompressed kind round-trips byte-identically.

## Check locally (no upload needed)

    python3 iwf_pic.py compose myface out.png --time 10:08 --month 9 --weekday 2

This is the same renderer the editor previews with. Sample values (time, date,
seconds, battery, weather, bar fill) are preview-only.

## Pack

    python3 iwf.py pack myface my.iwf              # plain archive
    python3 iwf.py pack myface my.iwf.lz --packed  # transfer form the watch is fed

`pack` encodes the PNGs back into picture members; a tree from a vendor dump
with no `order.txt` packs too (the member order is derived from the JSONs).
An `unpack --raw` / `pack` cycle is **byte-identical**; an `unpack` / `pack`
cycle is pixel-identical (fresh LZ4 framing, canonical tail included).
Directories in the older flat layout (one file per member, a `_png` sibling
from `iwf_pic.py decode-dir`) still pack unchanged.

## Upload

Upload either form through [Gadgetbridge](https://gadgetbridge.org/)'s app manager: the
watch stores it as the next `static_NN.iwf` and switches to it by itself. The first frames
after a switch can show stale leftovers (the watch redraws dirty regions only) — screen
off/on clears it.

Single files: `iwf.py list FILE`, `iwf_pic.py info|decode|encode FILE`.

## Format notes

**Transfer form (`.iwf.lz`).** A run of blocks, each a big-endian u32 length followed by a
FastLZ (level-2 revision, first byte `0x20`) stream that unpacks to at most 4 096 bytes and
never refers back past its own start. The vendor's packer chains `ff` length-extension
bytes, so the decoder must accept the level-2 rules.

**Archive (`.iwf`).**

```
 0..3     "iwf" 00
 4..5     u16   version (1)
 6..7     u16   member count
 8..      40-byte records: name (32 bytes, NUL-padded), u32 offset, u32 length
```

followed by the members end to end. `iwf.json` describes the face (name, `deviceId` —
`GTX10` is the Watch 5 — and an `item` list of widgets with `x`/`y`/`w`/`h`, `fgcolor`,
`align`, `font`); `font.json` and the pictures make up the rest.

**Pictures** (names say `.png`, they are not):

```
 0..3     "RAW" 00
 4..5     u16   width
 6..7     u16   height
 8        0x85  picture format family
 9        0x66 = per-pixel alpha (font strips), 0x00 otherwise
10..15    zero
16..      pixel data
```

- `_24bit`/`_lz4` members: one raw LZ4 block of little-endian RGB565, plus an A8 byte per
  pixel when byte 9 is `0x66`.
- all others: uncompressed big-endian RGB565, plus a 4-bit-per-pixel alpha plane on font
  strips.

The watch's LZ4 decoder needs the **canonical end-of-block rules**: the stream must end in a
literals-only sequence (≥ 5 bytes) and the last match must start ≥ 12 bytes before the end.
A block whose final match runs to the last byte decodes fine elsewhere but leaves garbage in
the bottom rows on the watch — which is why `iwf_pic.py`'s compressor stops short.
