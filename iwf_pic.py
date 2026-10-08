#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Vitalii Tomin
"""Decode and rebuild the pictures inside an unpacked watch-face directory.

The picture members of a `.iwf` archive (see iwf.py) are not PNGs whatever their
names say. Each is a 16-byte header followed by one LZ4 block:

     0..3    "RAW" 00
     4..5    u16    width
     6..7    u16    height
     8       0x85   the picture format the watch asked for in its screen info
     9       0x66   per-pixel alpha present (every font strip), 0x00 otherwise
    10..15   zero
    16..     LZ4 block, standard format, no framing; it unpacks to exactly
            width*height*3 bytes when byte 9 is 0x66, else width*height*2:
            per pixel [RGB565 low, RGB565 high] and, when alpha is present,
            [.. , .. , A8] — the "rgba5658" layout of get_rgba5658_pixel_buff
            in the vendor library (Ghidra, bitmap_tool.c).

The encoder here emits the same container with a fresh LZ4 block; any block the
stock decoder accepts will do, so sizes differ from the vendor's without the
watch caring.

    iwf_pic.py info FILE                 what a picture member holds
    iwf_pic.py decode FILE [OUT.png]     one member to a PNG
    iwf_pic.py encode IN.png OUT         one PNG back to a member
    iwf_pic.py decode-dir DIR            every member of DIR into DIR_png/
    iwf_pic.py encode-dir DIR            DIR_png/ back into DIR (names from
                                         the manifest decode-dir wrote)
    iwf_pic.py compose DIR OUT.png       render the face as the watch would

decode-dir writes a `pics.json` beside the PNGs recording each PNG's member
name, so encode-dir restores the exact names (several members already end in
`.png`, which a plain suffix rule could not tell apart). decode and encode
take the alpha channel from the PNG itself: an RGBA PNG becomes a 0x66 member,
an RGB PNG a 0x00 one.
"""

import argparse
import glob
import json
import os
import struct
import sys

MAGIC = b"RAW\0"
HEADER_LEN = 16
DEFAULT_FORMAT = 0x85
ALPHA_FLAG = 0x66
MANIFEST = "pics.json"


# --- LZ4, both directions ---------------------------------------------------------------------

def lz4_decompress(src, out_size):
    out = bytearray()
    i = 0
    n = len(src)
    while i < n:
        token = src[i]
        i += 1
        literals = token >> 4
        if literals == 15:
            while True:
                extra = src[i]
                i += 1
                literals += extra
                if extra != 0xFF:
                    break
        out += src[i:i + literals]
        i += literals
        if i >= n:
            break
        offset = src[i] | (src[i + 1] << 8)
        i += 2
        if offset == 0 or offset > len(out):
            raise ValueError("bad LZ4 offset %d" % offset)
        match = token & 15
        if match == 15:
            while True:
                extra = src[i]
                i += 1
                match += extra
                if extra != 0xFF:
                    break
        match += 4
        start = len(out) - offset
        for k in range(match):
            out.append(out[start + k])
    if len(out) != out_size:
        raise ValueError("LZ4 output is %d bytes, expected %d" % (len(out), out_size))
    if i != n:
        raise ValueError("%d bytes of the LZ4 block were never read" % (n - i))
    return bytes(out)


def lz4_compress(data):
    out = bytearray()
    anchor = 0
    position = 0
    seen = {}
    n = len(data)

    last_literals = 5     # the final 5 bytes are always literals
    match_limit = 12      # the last match must start at least 12 bytes before the end

    def emit_extension(count):
        remain = count - 15
        while remain >= 0xFF:
            out.append(0xFF)
            remain -= 0xFF
        out.append(remain)

    while position + 4 <= n and position <= n - match_limit:
        key = data[position:position + 4]
        candidate = seen.get(key, -1)
        seen[key] = position
        match = 0
        if candidate >= 0 and position - candidate <= 0xFFFF:
            limit = min(n - last_literals - position, 0xFFFF)
            while match < limit and data[candidate + match] == data[position + match]:
                match += 1
        if match < 4:
            position += 1
            continue
        literals = position - anchor
        literal_code = min(literals, 15)
        match_code = min(match - 4, 15)
        out.append((literal_code << 4) | match_code)
        if literal_code == 15:
            emit_extension(literals)
        out.extend(data[anchor:position])
        out.extend(struct.pack("<H", position - candidate))
        if match_code == 15:
            emit_extension(match - 4)
        position += match
        anchor = position
    literals = n - anchor
    if literals:
        literal_code = min(literals, 15)
        out.append(literal_code << 4)
        if literal_code == 15:
            emit_extension(literals)
        out.extend(data[anchor:n])
    return bytes(out)


# --- the picture container --------------------------------------------------------------------

def read_member_bytes(data):
    if data[:4] != MAGIC or len(data) < HEADER_LEN:
        raise ValueError("not a picture member")
    width, height = struct.unpack_from("<HH", data, 4)
    return data[8], data[9], width, height, data[HEADER_LEN:]


def read_member(path):
    return read_member_bytes(open(path, "rb").read())


def unpack_pixels(flags, width, height, block):
    """The rgba5658 codec: RGB565 little-endian, +A8 per pixel when 0x66."""
    alpha = flags == ALPHA_FLAG
    plane = lz4_decompress(block, width * height * (3 if alpha else 2))
    rows = []
    step = 3 if alpha else 2
    for y in range(height):
        row = []
        for x in range(width):
            base = (y * width + x) * step
            value = plane[base] | (plane[base + 1] << 8)
            red = (value >> 11) & 0x1F
            green = (value >> 5) & 0x3F
            blue = value & 0x1F
            row.append(((red << 3) | (red >> 2),
                        (green << 2) | (green >> 4),
                        (blue << 3) | (blue >> 2),
                        plane[base + 2] if alpha else 255))
        rows.append(row)
    return rows


def unpack_pixels_565(flags, width, height, body):
    """The get_565_pixel_buff codec: literal big-endian RGB565, with a trailing
    4-bit-per-pixel alpha plane on font strips (byte 9 = 0x66, pixel byte count
    at +0x0c). Uncompressed — the builder only LZ4s _24bit/_lz4-named sources."""
    alpha = flags == ALPHA_FLAG
    pixels = width * height * 2
    if len(body) < pixels:
        raise ValueError("565 member is %d bytes, pixels alone need %d" % (len(body), pixels))
    nibbles = body[pixels:pixels + (width // 2) * height]
    rows = []
    for y in range(height):
        row = []
        for x in range(width):
            base = (y * width + x) * 2
            value = (body[base] << 8) | body[base + 1]
            red = (value >> 11) & 0x1F
            green = (value >> 5) & 0x3F
            blue = value & 0x1F
            if alpha and (width // 2):
                nibble = nibbles[y * (width // 2) + (x >> 1)]
                nibble = (nibble >> 4) if (x % 2 == 0) else (nibble & 0xF)
            else:
                nibble = 15
            row.append(((red << 3) | (red >> 2),
                        (green << 2) | (green >> 4),
                        (blue << 3) | (blue >> 2),
                        nibble * 17))
        rows.append(row)
    return rows


def member_rows(name, data):
    """Decode any picture member, picking the codec the way the builder did:
    `_24bit`/`_lz4`-named sources went through get_rgba5658_pixel_buff
    (LZ4, little-endian, per-pixel A8), everything else through
    get_565_pixel_buff (big-endian literals, nibble alpha plane on fonts)."""
    picture_format, flags, width, height, body = read_member_bytes(data)
    if "_24bit" in name or "_lz4" in name:
        return unpack_pixels(flags, width, height, body)
    return unpack_pixels_565(flags, width, height, body)


def pack_pixels(pixels, with_alpha):
    width = len(pixels[0])
    height = len(pixels)
    plane = bytearray(width * height * (3 if with_alpha else 2))
    step = 3 if with_alpha else 2
    for y, row in enumerate(pixels):
        for x, (red, green, blue, alpha) in enumerate(row):
            value = ((red >> 3) << 11) | ((green >> 2) << 5) | (blue >> 3)
            base = (y * width + x) * step
            plane[base] = value & 0xFF
            plane[base + 1] = value >> 8
            if with_alpha:
                plane[base + 2] = alpha
    return lz4_compress(bytes(plane))


def member_from_pixels(pixels, with_alpha, picture_format):
    width = len(pixels[0])
    height = len(pixels)
    body = pack_pixels(pixels, with_alpha)
    header = struct.pack("<4sHHBBHI", MAGIC, width, height,
                         picture_format, ALPHA_FLAG if with_alpha else 0, 0, 0)
    return header + body


def pack_pixels_565(pixels, with_alpha):
    width = len(pixels[0])
    height = len(pixels)
    body = bytearray(width * height * 2)
    for y, row in enumerate(pixels):
        for x, (red, green, blue, alpha) in enumerate(row):
            value = ((red >> 3) << 11) | ((green >> 2) << 5) | (blue >> 3)
            base = (y * width + x) * 2
            body[base] = value >> 8
            body[base + 1] = value & 0xFF
    if not with_alpha or width < 2:
        return bytes(body), 0
    nibbles = bytearray((width // 2) * height)
    for y, row in enumerate(pixels):
        for x in range(0, (width // 2) * 2, 2):
            byte = (row[x][3] & 0xF0) | (row[x + 1][3] >> 4)
            nibbles[y * (width // 2) + (x >> 1)] = byte
    return bytes(body + nibbles), width * height * 2


def member_from_pixels_named(name, pixels, has_alpha, picture_format):
    """Encode a PNG's pixels for a given member name, the same codec choice the
    builder makes: _24bit/_lz4 names get rgba5658+LZ4, others get the literal
    big-endian 565 form (with the nibble alpha plane when has_alpha)."""
    if "_24bit" in name or "_lz4" in name:
        return member_from_pixels(pixels, has_alpha, picture_format)
    width = len(pixels[0])
    height = len(pixels)
    body, pixel_count = pack_pixels_565(pixels, has_alpha)
    header = struct.pack("<4sHHBBHI", MAGIC, width, height,
                         picture_format, ALPHA_FLAG if has_alpha else 0, 0,
                         pixel_count)
    return header + body


def load_png(path):
    from PIL import Image
    image = Image.open(path)
    if image.mode not in ("RGB", "RGBA"):
        image = image.convert("RGBA" if "A" in image.getbands() else "RGB")
    width, height = image.size
    pixels = []
    raw = image.load()
    for y in range(height):
        row = []
        for x in range(width):
            pixel = raw[x, y]
            if len(pixel) == 3:
                row.append((pixel[0], pixel[1], pixel[2], 255))
            else:
                row.append(tuple(pixel))
        pixels.append(row)
    return pixels, "A" in image.getbands()


def save_png(path, rows, with_alpha):
    from PIL import Image
    height = len(rows)
    width = len(rows[0])
    image = Image.new("RGBA" if with_alpha else "RGB", (width, height))
    raw = image.load()
    for y, row in enumerate(rows):
        for x, pixel in enumerate(row):
            raw[x, y] = pixel[:4] if with_alpha else pixel[:3]
    image.save(path)


# --- commands ---------------------------------------------------------------------------------

def is_member(path):
    try:
        with open(path, "rb") as handle:
            return handle.read(4) == MAGIC
    except OSError:
        return False


def cmd_info(args):
    picture_format, flags, width, height, block = read_member(args.file)
    alpha = flags == ALPHA_FLAG
    plane = width * height * (3 if alpha else 2)
    print("%s: %d x %d, format 0x%02x, %s" %
          (args.file, width, height, picture_format,
           "RGB565+A8" if alpha else "RGB565"))
    print("  LZ4 block %d bytes -> %d pixels bytes" % (len(block), plane))


def cmd_decode(args):
    _, flags, width, height, block = read_member(args.file)
    rows = unpack_pixels(flags, width, height, block)
    out = args.out or os.path.splitext(args.file)[0] + ".png"
    save_png(out, rows, flags == ALPHA_FLAG)
    print("%s -> %s" % (args.file, out))


def cmd_encode(args):
    pixels, has_alpha = load_png(args.png)
    if args.rgb:
        has_alpha = False
    member = member_from_pixels(pixels, has_alpha, args.format)
    with open(args.out, "wb") as handle:
        handle.write(member)
    print("%s -> %s (%d bytes, %s)" %
          (args.png, args.out, len(member),
           "RGB565+A8" if has_alpha else "RGB565"))


def cmd_decode_dir(args):
    out_dir = args.dir + "_png"
    os.makedirs(out_dir, exist_ok=True)
    manifest = {}
    count = 0
    for path in sorted(glob.glob(os.path.join(args.dir, "*"))):
        if not os.path.isfile(path) or not is_member(path):
            continue
        name = os.path.basename(path)
        _, flags, width, height, block = read_member(path)
        rows = unpack_pixels(flags, width, height, block)
        png_name = name[:-4] + ".png" if name.endswith(".png") else name + ".png"
        save_png(os.path.join(out_dir, png_name), rows, flags == ALPHA_FLAG)
        manifest[png_name] = name
        count += 1
    with open(os.path.join(out_dir, MANIFEST), "w") as handle:
        json.dump(manifest, handle, indent=1, sort_keys=True)
    print("%d pictures into %s" % (count, out_dir))


def cmd_encode_dir(args):
    png_dir = args.dir + "_png"
    manifest_path = os.path.join(png_dir, MANIFEST)
    if os.path.exists(manifest_path):
        manifest = json.load(open(manifest_path))
    else:
        manifest = None
    count = 0
    for path in sorted(glob.glob(os.path.join(png_dir, "*.png"))):
        name = os.path.basename(path)
        member_name = manifest.get(name) if manifest else None
        if member_name is None:
            member_name = name[:-4] if name.endswith(".png") else name
        pixels, has_alpha = load_png(path)
        member = member_from_pixels(pixels, has_alpha, args.format)
        with open(os.path.join(args.dir, member_name), "wb") as handle:
            handle.write(member)
        count += 1
    print("%d PNGs from %s back into %s" % (count, png_dir, args.dir))


def load_strips(png_dir):
    """Every PNG under a directory (sub-folders included), keyed the way the
    renderer looks things up: the relative path with `/` folded to `_` and the
    extension dropped — `g12/0_24bit.png` and `g12_0_24bit.png` both become
    `g12_0_24bit`, so the flat and foldered layouts behave alike."""
    from PIL import Image
    strips = {}
    for folder, _, files in os.walk(png_dir):
        for file_name in files:
            if not file_name.endswith(".png"):
                continue
            relative = os.path.relpath(os.path.join(folder, file_name), png_dir)
            key = relative[:-4].replace(os.sep, "_")
            strips[key] = Image.open(os.path.join(folder, file_name))
    return strips


def png_dir_of(dir_path):
    """Where a face directory keeps its PNGs: the `_png` sibling of the flat
    layout, or the directory itself in the foldered one."""
    sibling = dir_path + "_png"
    return sibling if os.path.isdir(sibling) else dir_path


def background_path(png_dir, layout):
    """The background member, whatever the vendor named it: some dumps keep
    real .bmp files, members can carry a .bmp name but sit as .png, and the
    wizard-generated faces have no background member at all."""
    name = layout.get("bkground") or ""
    candidates = [name, name + ".png"]
    if name.endswith(".bmp"):
        candidates.append(name[:-4] + ".png")
    for candidate in candidates:
        if candidate and os.path.exists(os.path.join(png_dir, candidate)):
            return os.path.join(png_dir, candidate)
    return None


def fallback_size(layout):
    """Canvas size for the faces with no background member: everything the
    widgets claim, so no pasted strip is cut off."""
    width = height = 0
    for item in layout.get("item", []):
        width = max(width, item.get("x", 0) + item.get("w", 0))
        height = max(height, item.get("y", 0) + item.get("h", 0))
    return max(width, 240), max(height, 240)


def load_background(png_dir, layout):
    """The face's background image, or the black canvas the wizard faces are
    drawn on (the watch clears to black behind them)."""
    from PIL import Image
    path = background_path(png_dir, layout)
    if path is not None:
        return Image.open(path).copy()
    return Image.new("RGB", fallback_size(layout), (0, 0, 0))


def render_face(layout, strips, background, hour, minute, day, month, weekday,
                digits, fill_fraction, seconds=0, battery=80, condition=1):
    """Draw the face as the watch would: background, then every widget.

    The sample values are previews only. The widget-type census this covers
    comes from the vendor's own faces: digit runs (hour, min, day, time, date,
    year, second, step, calorie, distance, battery, heartrate, the split
    hourhi/hourlo/minhi/minlo), single-glyph types (month, week, apm, units,
    weather, hour_one, redpoint, icon, anima, gradient), the types the watch
    draws with its own font (active_time, walk_time — approximated with a
    system font here), analog hands (widget "watch"), and the customizable
    shortcut slots ("slot" containers).
    """
    from datetime import date
    from PIL import Image, ImageDraw, ImageFont

    canvas = background.convert("RGBA")

    def at(item):
        """Where a widget sits. Read through .get like every other field: the
        editor hands us whatever the user typed, and a missing coordinate is a
        widget drawn at the origin, not a broken render."""
        return item.get("x", 0), item.get("y", 0)

    def colour_of(item, field, fallback=(255, 255, 255)):
        """An 0xAARRGGBB string as RGB."""
        text = item.get(field) or ""
        try:
            return tuple(int(text[i:i + 2], 16) for i in (4, 6, 8))
        except ValueError:
            return fallback

    def digit(font, value):
        for suffix in ("_24bit", ""):
            glyph = strips.get("%s_%d%s" % (font, value, suffix))
            if glyph is not None:
                return glyph
        return None

    def named(font, word):
        for key, image in strips.items():
            if not key.startswith(font + "_"):
                continue
            if key.endswith("_" + word + "_24bit") or key.endswith("_" + word):
                return image
        return None

    def paste_glyph(canvas, glyph, position):
        if "A" in glyph.getbands():
            canvas.paste(glyph, position, glyph)
        else:
            canvas.paste(glyph, position)

    def run(item, text):
        """A digit run; a non-digit character pastes strip 10 — every font
        that has one keeps its separator (colon, dot) there."""
        offset = 0
        for character in str(text):
            glyph = digit(item.get("font", ""),
                          int(character) if character.isdigit() else 10)
            if glyph is None:
                continue
            x, y = at(item)
            paste_glyph(canvas, glyph, (x + offset, y))
            offset += glyph.width

    def system_text(item, text):
        """active_time / walk_time: the watch renders these with its own font
        (fontFamily, fontsize) and can rotate them (rotangle)."""
        try:
            face = ImageFont.truetype("/usr/share/fonts/google-noto/"
                                      "NotoSans-Bold.ttf", item.get("fontsize", 18))
        except OSError:
            return
        colour = colour_of(item, "fgcolor")
        box = Image.new("RGBA", (max(1, item.get("w", 40)),
                                 max(1, item.get("h", 24))), (0, 0, 0, 0))
        drawn = ImageDraw.Draw(box)
        width = drawn.textlength(text, font=face)
        top = (box.height - face.size) / 2
        if item.get("align") == "left":
            origin = (0, top)
        elif item.get("align") == "right":
            origin = (box.width - width, top)
        else:
            origin = ((box.width - width) / 2, top)
        drawn.text(origin, text, font=face, fill=colour + (255,))
        if item.get("rotangle"):
            box = box.rotate(-item["rotangle"])
        canvas.paste(box, at(item), box)

    def hand(item, field, prefix, angle):
        """One analog hand: the strip points up from its pivot (prefix+center*
        inside the image) which sits at prefix+anchor* on the dial."""
        glyph = strips.get(item.get(field, "").rsplit(".", 1)[0])
        if glyph is None:
            return
        pivot = (item.get(prefix + "centerx", glyph.width // 2),
                 item.get(prefix + "centery", glyph.height - 8))
        anchor = (item.get(prefix + "anchorx", item.get("w", 1) // 2),
                  item.get(prefix + "anchory", item.get("h", 1) // 2))
        layer = Image.new("RGBA", (item.get("w", 1), item.get("h", 1)), (0, 0, 0, 0))
        layer.paste(glyph, (anchor[0] - pivot[0], anchor[1] - pivot[1]))
        layer = layer.rotate(-angle, center=anchor, resample=Image.BILINEAR)
        canvas.paste(layer, at(item), layer)

    months = ["jan", "feb", "mar", "apr", "may", "june", "july", "aug",
              "sept", "oct", "nov", "dec"]
    weeks = ["sun", "mon", "tue", "wed", "thur", "fri", "sat"]
    texts = {"hour": "%02d" % hour, "min": "%02d" % minute, "day": "%02d" % day,
             "time": "%02d:%02d" % (hour, minute), "second": "%02d" % seconds,
             "year": str(date.today().year), "date": "%d.%d" % (month, day),
             "battery": str(battery), "heartrate": "72",
             "step": "0" * digits, "calorie": "0" * digits, "distance": "0.0",
             "hourhi": str(hour // 10), "hourlo": str(hour % 10),
             "minhi": str(minute // 10), "minlo": str(minute % 10)}
    for item in layout.get("item", []):
        if item.get("widget") == "progressbar":
            continue
        kind = item.get("type")
        if item.get("widget") == "watch":          # analog hands
            hand(item, "hour", "hour", (hour % 12 + minute / 60.0) * 30)
            hand(item, "minute", "min", minute * 6)
            hand(item, "second", "sec", seconds * 6)
        elif "slot" in item:                       # customizable shortcut slots
            for holder in item["slot"]:
                for child in holder.get("container", []):
                    if child.get("type") == "icon" and child.get("font"):
                        glyph = digit(child["font"], child.get("selfont", 0))
                        if glyph is not None:
                            paste_glyph(canvas, glyph, at(child))
        elif kind in ("month", "week"):
            glyph = named(item.get("font", ""), months[month - 1] if kind == "month"
                          else weeks[weekday])
            if glyph is not None:
                paste_glyph(canvas, glyph, at(item))
        elif kind == "apm":
            glyph = named(item.get("font", ""), "am" if hour < 12 else "pm")
            if glyph is not None:
                paste_glyph(canvas, glyph, at(item))
        elif kind == "units":
            glyph = named(item.get("font", ""),
                          "mi" if item.get("metricinch") else "km")
            if glyph is not None:
                paste_glyph(canvas, glyph, at(item))
        elif kind == "icon":
            glyph = (strips.get(item.get("bg", "").rsplit(".", 1)[0])
                     if item.get("bg") else
                     digit(item.get("font", ""), item.get("selfont", 0)))
            if glyph is not None:
                paste_glyph(canvas, glyph, at(item))
        elif kind == "anima":
            glyph = digit(item.get("animaicon", "anima"), 0)
            if glyph is not None:
                paste_glyph(canvas, glyph, at(item))
        elif kind in ("weather", "hour_one", "redpoint"):
            glyph = (named(item.get("font", ""), "news") if kind == "redpoint"
                     else digit(item.get("font", ""),
                                hour % 12 if kind == "hour_one" else condition))
            if glyph is None and kind == "redpoint":
                glyph = digit(item.get("font", ""), 0)
            if glyph is not None:
                paste_glyph(canvas, glyph, at(item))
        elif kind == "gradient":                   # full-state underlay, value on top
            glyph = digit(item.get("font", ""), 10)
            if glyph is not None:
                paste_glyph(canvas, glyph, at(item))
            run(item, str(battery))
        elif kind in ("active_time", "walk_time"):
            system_text(item, "128" if kind == "active_time" else "45")
        elif kind in texts:
            run(item, texts[kind])
    for item in layout.get("item", []):
        if item.get("widget") != "progressbar":
            continue
        width = max(1, item.get("w", 1))
        height = max(1, item.get("h", 1))
        colour = colour_of(item, "fgcolor", (83, 228, 0))
        bar = Image.new("RGBA", (width, height), colour + (255,))
        mask = Image.new("L", (width, height), 0)
        ImageDraw.Draw(mask).rectangle(
            [0, 0, width - 1, int(height * fill_fraction)], fill=255)
        canvas.paste(bar, at(item), mask)
    return canvas.convert("RGB")


def cmd_compose(args):
    layout = json.load(open(os.path.join(args.dir, "iwf.json")))
    png_dir = png_dir_of(args.dir)
    strips = load_strips(png_dir)
    background = load_background(png_dir, layout)
    hour, minute = (int(part) for part in args.time.split(":"))
    canvas = render_face(layout, strips, background, hour, minute, args.day,
                         args.month, args.weekday, args.digits, args.fill,
                         seconds=args.seconds, battery=args.battery,
                         condition=args.weather)
    canvas.save(args.out)
    print("composed %s" % args.out)


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    commands = parser.add_subparsers(dest="command", required=True)

    info = commands.add_parser("info", help="what a picture member holds")
    info.add_argument("file")
    info.set_defaults(run=cmd_info)

    decode = commands.add_parser("decode", help="one member to a PNG")
    decode.add_argument("file")
    decode.add_argument("out", nargs="?")
    decode.set_defaults(run=cmd_decode)

    encode = commands.add_parser("encode", help="one PNG back to a member")
    encode.add_argument("png")
    encode.add_argument("out")
    encode.add_argument("--format", type=lambda text: int(text, 0), default=DEFAULT_FORMAT)
    encode.add_argument("--rgb", action="store_true", help="drop the alpha channel")
    encode.set_defaults(run=cmd_encode)

    decode_dir = commands.add_parser("decode-dir", help="every member of a face directory")
    decode_dir.add_argument("dir")
    decode_dir.set_defaults(run=cmd_decode_dir)

    encode_dir = commands.add_parser("encode-dir", help="a face directory's PNGs back to members")
    encode_dir.add_argument("dir")
    encode_dir.add_argument("--format", type=lambda text: int(text, 0), default=DEFAULT_FORMAT)
    encode_dir.set_defaults(run=cmd_encode_dir)

    compose = commands.add_parser("compose", help="render the face as the watch would")
    compose.add_argument("dir")
    compose.add_argument("out")
    compose.add_argument("--time", default="10:08")
    compose.add_argument("--day", type=int, default=8)
    compose.add_argument("--month", type=int, default=9)
    compose.add_argument("--weekday", type=int, default=2)
    compose.add_argument("--digits", type=int, default=5)
    compose.add_argument("--fill", type=float, default=0.6)
    compose.add_argument("--seconds", type=int, default=0)
    compose.add_argument("--battery", type=int, default=80)
    compose.add_argument("--weather", type=int, default=1,
                         help="weather condition, an index into the font's strips")
    compose.set_defaults(run=cmd_compose)

    args = parser.parse_args()
    args.run(args)


if __name__ == "__main__":
    sys.exit(main())
