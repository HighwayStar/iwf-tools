#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Vitalii Tomin
"""Inspect, unpack and repack the watch's `.iwf` watch-face archives.

The archive is the Ido/VeryFit "watch plate" container described in PROTOCOL.md 4.11.1: an
8-byte header, a directory of 40-byte records, and the members laid end to end behind it. The
watch is fed the archive packed (PROTOCOL.md 4.11), and this tool reads and writes both forms.

    iwf.py list   FILE                  what the archive holds
    iwf.py unpack FILE DIR [--raw]      the source tree the vendor app itself uses
    iwf.py pack   DIR FILE [--packed]   rebuild it, optionally in the packed transfer form

FILE may be an archive (`static_10.iwf`) or a packed one (`static_10.iwf.lz`); which one it is
is decided by looking at it, not by the name. `pack --packed` writes the packed form, which is
what goes over the wire; Gadgetbridge packs a plain archive itself, so either can be installed.

`unpack` writes the layout the vendor app keeps on the phone and `mkWatchFace::makeFile`
expects: `iwf.json`/`font.json` and the background and preview PNGs at the top, and one
directory per font holding that font's strips as PNGs (`g12/0_24bit.png`,
`month/en_sept_24bit.png` — the font prefix of the member name becomes the folder). Pictures
arrive decoded, so the tree is editable as it stands; `--raw` writes the member bytes verbatim
instead, and `pack` prefers a PNG when both exist, so an unpack --raw / pack cycle is
byte-identical while an unpack / pack cycle is pixel-identical. `pack` also takes a tree with
no `order.txt` (the vendor's own dumps), deriving the member order from the JSONs.

Directories in the older flat layout (every member one file, `*_png` beside it) still pack
unchanged.
"""

import argparse
import json
import os
import struct
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import iwf_pic

ARCHIVE_MARKER = b"iwf\0"
HEADER_LEN = 8
ENTRY_LEN = 40
NAME_LEN = 32
ORDER_FILE = "order.txt"

BLOCK_LEN = 4096
MIN_MATCH = 3
MAX_MATCH = 263
MAX_LITERALS = 32
REVISION_MARK = 0x20
LENGTH_ESCAPE = 6


# --- the packing the file channel takes ------------------------------------------------------

def unpack_block(src):
    out = bytearray()
    ip = 0
    control = src[ip] & 0x1F
    ip += 1
    while True:
        if control >= MAX_LITERALS:
            match = (control >> 5) - 1
            distance = (control & 0x1F) << 8
            if match == LENGTH_ESCAPE:
                while True:
                    extra = src[ip]
                    ip += 1
                    match += extra
                    if extra != 0xFF:
                        break
            distance += src[ip]
            ip += 1
            start = len(out) - distance - 1
            if start < 0:
                raise ValueError("match before the start of the block")
            for _ in range(match + MIN_MATCH):
                out.append(out[start])
                start += 1
        else:
            count = control + 1
            out += src[ip:ip + count]
            ip += count
        if ip >= len(src):
            break
        control = src[ip]
        ip += 1
    return bytes(out)


def pack_block(data):
    """Only the part of the format both revisions of the unpacker read the same way."""
    out = bytearray()
    recent = {}
    anchor = 0
    position = 0

    def literals(start, count):
        written = 0
        while written < count:
            run = min(MAX_LITERALS, count - written)
            out.append(run - 1)
            out.extend(data[start + written:start + written + run])
            written += run

    while position + MIN_MATCH <= len(data):
        key = data[position:position + MIN_MATCH]
        candidate = recent.get(key, -1)
        recent[key] = position

        match = 0
        if candidate >= 0:
            limit = min(len(data) - position, MAX_MATCH)
            while match < limit and data[candidate + match] == data[position + match]:
                match += 1
        if match < MIN_MATCH:
            position += 1
            continue

        literals(anchor, position - anchor)
        offset = position - candidate - 1
        length = match - MIN_MATCH
        if length < LENGTH_ESCAPE:
            out.append(((length + 1) << 5) | (offset >> 8))
        else:
            out.append((7 << 5) | (offset >> 8))
            out.append(length - LENGTH_ESCAPE)
        out.append(offset & 0xFF)

        for i in range(position + 1, position + match):
            if i + MIN_MATCH <= len(data):
                recent[data[i:i + MIN_MATCH]] = i
        position += match
        anchor = position

    literals(anchor, len(data) - anchor)
    out[0] |= REVISION_MARK
    return bytes(out)


def unpack_file(packed):
    """The packed form: blocks of at most 4096 bytes, each with its length in front, big endian."""
    out = bytearray()
    offset = 0
    while offset + 4 <= len(packed):
        length = struct.unpack_from(">I", packed, offset)[0]
        offset += 4
        if length <= 0 or offset + length > len(packed):
            raise ValueError("truncated block")
        out += unpack_block(packed[offset:offset + length])
        offset += length
    if offset != len(packed):
        raise ValueError("trailing bytes after the last block")
    return bytes(out)


def pack_file(data):
    out = bytearray()
    for offset in range(0, len(data), BLOCK_LEN):
        block = pack_block(data[offset:offset + BLOCK_LEN])
        out += struct.pack(">I", len(block))
        out += block
    return bytes(out)


# --- the archive -----------------------------------------------------------------------------

def read_archive(path):
    data = open(path, "rb").read()
    if data[:4] == ARCHIVE_MARKER:
        return data
    return unpack_file(data)


def members(archive):
    if archive[:4] != ARCHIVE_MARKER:
        raise ValueError("not a watch face")
    version, count = struct.unpack_from("<HH", archive, 4)
    out = []
    for i in range(count):
        entry = HEADER_LEN + i * ENTRY_LEN
        name = archive[entry:entry + NAME_LEN].split(b"\0", 1)[0].decode("utf-8")
        offset, length = struct.unpack_from("<II", archive, entry + NAME_LEN)
        if offset + length > len(archive):
            raise ValueError("member %s runs past the end" % name)
        out.append((name, archive[offset:offset + length]))
    return version, out


def picture(body):
    """Width and height, for a member that is one of the pictures."""
    if len(body) < HEADER_LEN or body[:4] != b"RAW\0":
        return None
    return struct.unpack_from("<HH", body, 4)


def build(version, entries):
    header = struct.pack("<4sHH", ARCHIVE_MARKER, version, len(entries))
    directory = bytearray()
    body = bytearray()
    offset = HEADER_LEN + len(entries) * ENTRY_LEN
    for name, contents in entries:
        encoded = name.encode("utf-8")
        if len(encoded) >= NAME_LEN:
            raise ValueError("%s does not fit a member name" % name)
        directory += encoded.ljust(NAME_LEN, b"\0")
        directory += struct.pack("<II", offset + len(body), len(contents))
        body += contents
    return bytes(header + directory + body)


# --- commands --------------------------------------------------------------------------------

def cmd_list(args):
    archive = read_archive(args.file)
    version, entries = members(archive)
    print("%s: version %d, %d members, %d bytes" % (args.file, version, len(entries), len(archive)))
    for name, body in entries:
        size = picture(body)
        shape = "%d x %d" % size if size else ""
        print("  %-24s %7d  %s" % (name, len(body), shape))
    layout = dict(entries).get("iwf.json")
    if layout:
        try:
            described = json.loads(layout.decode("utf-8"))
        except ValueError:
            return
        print("  ---")
        for field in ("name", "author", "description", "deviceId", "preview", "compress"):
            if field in described:
                print("  %-12s %s" % (field, described[field]))
        print("  %-12s %d" % ("widgets", len(described.get("item", []))))


# --- the foldered source layout ----------------------------------------------------------------

def font_names_of(dir_path):
    """Font names from font.json — the prefixes that name the strip folders."""
    path = os.path.join(dir_path, "font.json")
    if not os.path.exists(path):
        return []
    try:
        described = json.load(open(path))
    except ValueError:
        return []
    return [entry.get("name") for entry in described.get("item", [])
            if entry.get("name")]


def member_files(name, font_names):
    """(png, blob) paths of a member inside the foldered layout. The font
    prefix of the member name becomes the folder — longest prefix wins, so
    `num_battery_0` lands in num_battery/ and not in num/."""
    best = None
    for font in font_names:
        if name.startswith(font + "_") and (best is None or len(font) > len(best)):
            best = font
    if best is not None:
        rest = name[len(best) + 1:]
        return os.path.join(best, rest + ".png"), os.path.join(best, rest)
    png = name if name.endswith(".png") else name + ".png"
    return png, name


def derive_order(dir_path, font_names):
    """Member order for a tree with no order.txt — the order the builder itself uses."""
    names = [name for name in ("iwf.json", "iwf1.json", "font.json")
             if os.path.exists(os.path.join(dir_path, name))]
    layout_path = os.path.join(dir_path, "iwf.json")
    if os.path.exists(layout_path):
        try:
            layout = json.load(open(layout_path))
            for field in ("preview", "bkground"):
                if layout.get(field):
                    names.append(layout[field])
        except ValueError:
            pass
    for font in font_names:
        folder = os.path.join(dir_path, font)
        if not os.path.isdir(folder):
            continue
        for file_name in sorted(os.listdir(folder)):
            if file_name.endswith(".png"):
                file_name = file_name[:-4]
            elif file_name.endswith(".bmp"):
                file_name = file_name[:-4]
            names.append("%s_%s" % (font, file_name))
    return names


def cmd_unpack(args):
    archive = read_archive(args.file)
    version, entries = members(archive)
    font_names = font_names_of_entries(entries)
    os.makedirs(args.dir, exist_ok=True)
    formats = []
    with open(os.path.join(args.dir, ORDER_FILE), "w") as order:
        order.write("version %d\nlayout dirs\n" % version)
        for name, body in entries:
            if body[:4] == iwf_pic.MAGIC:
                formats.append(body[8])
                png, blob = member_files(name, font_names)
                path = os.path.join(args.dir, blob if args.raw else png)
                folder = os.path.dirname(path)
                if folder:
                    os.makedirs(folder, exist_ok=True)
                if args.raw:
                    open(path, "wb").write(body)
                else:
                    rows = iwf_pic.member_rows(name, body)
                    iwf_pic.save_png(path, rows, body[9] == iwf_pic.ALPHA_FLAG)
            else:
                open(os.path.join(args.dir, name), "wb").write(body)
            order.write("%s\n" % name)
        if formats:
            order.write("format 0x%02x\n" % formats[0])
    print("%d members into %s (%s)" % (len(entries), args.dir,
                                       "raw members" if args.raw else "PNG source tree"))


def font_names_of_entries(entries):
    for name, body in entries:
        if name == "font.json":
            try:
                described = json.loads(body.decode("utf-8"))
            except ValueError:
                return []
            return [entry.get("name") for entry in described.get("item", [])
                    if entry.get("name")]
    return []


def cmd_pack(args):
    version = 1
    picture_format = 0x85
    names = []
    header = {}
    order = os.path.join(args.dir, ORDER_FILE)
    if os.path.exists(order):
        for line in open(order):
            line = line.strip()
            if " " in line:
                key, value = line.split(" ", 1)
                header[key] = value
                if key == "version":
                    version = int(value)
                elif key == "format":
                    picture_format = int(value, 0)
            elif line:
                names.append(line)
        foldered = header.get("layout") == "dirs"
    else:
        font_names = font_names_of(args.dir)
        foldered = bool(font_names) and any(
            os.path.isdir(os.path.join(args.dir, font)) for font in font_names)
        if foldered:
            names = derive_order(args.dir, font_names)
        else:
            names = sorted(os.listdir(args.dir))

    font_names = font_names_of(args.dir)
    entries = []
    for name in names:
        if not foldered:
            path = os.path.join(args.dir, name)
            if not os.path.isfile(path):
                raise SystemExit("%s is missing" % path)
            entries.append((name, open(path, "rb").read()))
            continue
        png, blob = member_files(name, font_names)
        for candidate in (png, blob):
            path = os.path.join(args.dir, candidate)
            if not os.path.exists(path):
                continue
            data = open(path, "rb").read()
            if data[:4] == iwf_pic.MAGIC or not candidate.endswith(".png"):
                entries.append((name, data))
            else:
                pixels, has_alpha = iwf_pic.load_png(path)
                entries.append((name, iwf_pic.member_from_pixels_named(
                    name, pixels, has_alpha, picture_format)))
            break
        else:
            raise SystemExit("%s is missing (looked for %s and %s)"
                             % (name, png, blob))

    archive = build(version, entries)
    if args.packed:
        archive = pack_file(archive)
    with open(args.file, "wb") as out:
        out.write(archive)
    print("%d members, %d bytes into %s" % (len(entries), len(archive), args.file))


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    commands = parser.add_subparsers(dest="command", required=True)

    listing = commands.add_parser("list", help="what the archive holds")
    listing.add_argument("file")
    listing.set_defaults(run=cmd_list)

    unpacking = commands.add_parser("unpack", help="the vendor source tree of a face")
    unpacking.add_argument("file")
    unpacking.add_argument("dir")
    unpacking.add_argument("--raw", action="store_true",
                           help="write member bytes verbatim instead of PNGs")
    unpacking.set_defaults(run=cmd_unpack)

    packing = commands.add_parser("pack", help="rebuild an archive from a directory")
    packing.add_argument("dir")
    packing.add_argument("file")
    packing.add_argument("--packed", action="store_true",
                         help="write the packed form the watch is fed")
    packing.set_defaults(run=cmd_pack)

    args = parser.parse_args()
    args.run(args)


if __name__ == "__main__":
    sys.exit(main())
