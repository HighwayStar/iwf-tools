#!/usr/bin/env python3
"""A small editor for the layout of an unpacked watch-face directory.

    python3 iwf_gui.py face

Shows the face composed exactly as iwf_pic.py compose renders it, and lets you
rearrange it with the mouse: click a widget to select it (its box lights up),
drag to move, edit the quick x/y/w/h fields or the full JSON of the selected
widget, add or remove widgets, and save iwf.json. The sample values (time,
date, seconds, battery, weather, bar fill) are preview-only, like compose's,
and cover every widget type the vendor's own faces use — the strip runs, the
analog hands, the shortcut slots. Pictures are not edited here —
that stays iwf_pic.py's job; the Pack buttons only re-pack what is in the
directory, so a JSON-only edit never touches the picture members.

Requires tkinter (python3-tk) and, as everywhere in these tools, Pillow.
"""

import io
import json
import os
import subprocess
import sys
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import iwf
import iwf_pic as pic

CANVAS_MARGIN = 12

# Everything the vendor's own faces have been seen using. The strip-run types
# need a font; the rest build their own widget JSON in on_add.
TEXT_TYPES = ("hour", "min", "hourhi", "hourlo", "minhi", "minlo", "hour_one",
              "time", "date", "day", "month", "week", "year", "second", "apm",
              "step", "calorie", "distance", "heartrate", "battery", "weather",
              "units", "redpoint", "gradient")
OTHER_TYPES = ("icon", "anima", "active_time", "walk_time",
               "watch (analog hands)", "slot container")
FONTNUM_FOR = {"month": 12, "week": 7, "apm": 2, "units": 2, "weather": 20,
               "time": 11, "date": 11, "distance": 11, "gradient": 11,
               "hour_one": 12, "redpoint": 7}
MARKER_FOR = {"month": ("_jan_24bit",), "week": ("_mon_24bit",),
              "apm": ("_am_24bit",), "units": ("_km_24bit",),
              "redpoint": ("_news_24bit", "_0_24bit")}
# The three hands render_face draws: the widget field, the prefix its geometry
# fields carry, and the words a face's hand member is named with. Faces name
# these themselves — w3 calls its only hand `sec.png` — so match the word.
HAND_HINTS = (("hour", "hour", ("hourhand", "hand_h", "hour")),
              ("minute", "min", ("minutehand", "hand_m", "minute", "min")),
              ("second", "sec", ("secondhand", "hand_s", "second", "sec")))


class FaceEditor:
    def __init__(self, root, face_dir):
        self.root = root
        self.dir = os.path.abspath(face_dir)
        self.png_dir = pic.png_dir_of(self.dir)
        self.layout_path = os.path.join(self.dir, "iwf.json")
        if not os.path.exists(self.layout_path):
            raise SystemExit("no iwf.json in %s — run iwf.py unpack first" % self.dir)

        self.layout = json.load(open(self.layout_path))
        self.strips = pic.load_strips(self.png_dir)
        self.background = pic.load_background(self.png_dir, self.layout)
        self.fonts = iwf.font_names_of(self.dir)
        self.selected = None
        self.drag = None
        self.refreshing = False
        self.build_ui()
        self.render()

    # --- ui ----------------------------------------------------------------------------------

    def build_ui(self):
        self.root.title("iwf editor — %s" % os.path.basename(self.dir))
        body = ttk.Frame(self.root, padding=6)
        body.pack(fill="both", expand=True)

        left = ttk.Frame(body)
        left.pack(side="left", fill="both", expand=True)
        width, height = self.background.size
        self.canvas = tk.Canvas(left, width=width + 2 * CANVAS_MARGIN,
                                height=height + 2 * CANVAS_MARGIN,
                                highlightthickness=0, bg="#404040")
        self.canvas.pack()
        self.canvas.bind("<Button-1>", self.on_press)
        self.canvas.bind("<B1-Motion>", self.on_motion)
        self.canvas.bind("<ButtonRelease-1>", self.on_release)

        right = ttk.Frame(body, width=320)
        right.pack(side="left", fill="both", expand=True, padx=(8, 0))

        samples = ttk.LabelFrame(right, text="sample values (preview only)", padding=4)
        samples.pack(fill="x")
        row = ttk.Frame(samples)
        row.pack(fill="x")
        self.var_time = tk.StringVar(value="10:08")
        self.var_day = tk.IntVar(value=8)
        self.var_month = tk.IntVar(value=9)
        self.var_weekday = tk.IntVar(value=2)
        for label, variable, width in (("time", self.var_time, 6), ("day", self.var_day, 3),
                                       ("month", self.var_month, 3), ("weekday", self.var_weekday, 3)):
            ttk.Label(row, text=label).pack(side="left")
            ttk.Entry(row, textvariable=variable, width=width).pack(side="left", padx=(1, 6))
        row2 = ttk.Frame(samples)
        row2.pack(fill="x", pady=(3, 0))
        self.var_seconds = tk.IntVar(value=0)
        self.var_battery = tk.IntVar(value=80)
        self.var_weather = tk.IntVar(value=1)
        for label, variable in (("seconds", self.var_seconds), ("battery", self.var_battery),
                                ("weather", self.var_weather)):
            ttk.Label(row2, text=label).pack(side="left")
            ttk.Entry(row2, textvariable=variable, width=4).pack(side="left", padx=(1, 6))
        self.var_fill = tk.IntVar(value=60)
        ttk.Label(samples, text="bar fill %").pack()
        ttk.Scale(samples, from_=0, to=100, variable=self.var_fill,
                  command=lambda *_: self.render()).pack(fill="x")
        for variable in (self.var_time, self.var_day, self.var_month, self.var_weekday,
                         self.var_seconds, self.var_battery, self.var_weather):
            variable.trace_add("write", lambda *_: self.render())

        widgets = ttk.LabelFrame(right, text="widgets", padding=4)
        widgets.pack(fill="both", expand=True)
        self.listbox = tk.Listbox(widgets, height=8, exportselection=False)
        self.listbox.pack(fill="both", expand=True)
        self.listbox.bind("<<ListboxSelect>>", self.on_list_select)
        add_row = ttk.Frame(widgets)
        add_row.pack(fill="x", pady=(4, 0))
        self.var_type = tk.StringVar(value=TEXT_TYPES[0])
        self.type_box = ttk.Combobox(add_row, textvariable=self.var_type, width=18,
                                     values=TEXT_TYPES + OTHER_TYPES
                                     + ("progressbar step", "progressbar calorie"),
                                     state="readonly")
        self.type_box.pack(side="left")
        ttk.Button(add_row, text="add", width=5, command=self.on_add).pack(side="left", padx=4)
        ttk.Button(add_row, text="remove", command=self.on_remove).pack(side="left")

        fields = ttk.LabelFrame(right, text="position and size", padding=4)
        fields.pack(fill="x")
        grid = ttk.Frame(fields)
        grid.pack(fill="x")
        self.entries = {}
        for row_index, name in enumerate(("x", "y", "w", "h")):
            ttk.Label(grid, text=name).grid(row=row_index // 4, column=(row_index % 4) * 2)
            variable = tk.IntVar()
            entry = ttk.Entry(grid, textvariable=variable, width=5)
            entry.grid(row=row_index // 4, column=(row_index % 4) * 2 + 1, padx=(1, 8))
            entry.bind("<Return>", lambda *_: self.apply_fields())
            variable.trace_add("write", lambda *_: self.apply_fields(live=True))
            self.entries[name] = variable

        editor = ttk.LabelFrame(right, text="selected widget (JSON)", padding=4)
        editor.pack(fill="both", expand=True)
        self.json_box = tk.Text(editor, height=9, wrap="none", font=("monospace", 9))
        self.json_box.pack(fill="both", expand=True)
        ttk.Button(editor, text="apply JSON", command=self.apply_json).pack(pady=(4, 0))

        actions = ttk.Frame(right)
        actions.pack(fill="x", pady=(6, 0))
        ttk.Button(actions, text="save iwf.json", command=self.save).pack(side="left")
        ttk.Button(actions, text="pack .iwf", command=lambda: self.pack(False)).pack(side="left", padx=4)
        ttk.Button(actions, text="pack .iwf.lz", command=lambda: self.pack(True)).pack(side="left")
        self.status = ttk.Label(right, text="", anchor="w")
        self.status.pack(fill="x", pady=(4, 0))

        self.refresh_list()

    # --- helpers -----------------------------------------------------------------------------

    def items(self):
        return self.layout.setdefault("item", [])

    def widget_label(self, item):
        if item.get("widget") == "progressbar":
            return "bar  %-8s %s" % (item.get("type"), self.where(item))
        if "slot" in item:
            return "slots  %d shortcut%s" % (len(item["slot"]),
                                             "" if len(item["slot"]) == 1 else "s")
        if item.get("widget") == "watch":
            return "analog  %s" % self.where(item)
        return "%-11s %-6s %s" % (item.get("type"), item.get("font", ""), self.where(item))

    def widget_name(self, item):
        """What to call a widget in a message — the containers and the analog
        hands have no `type` of their own."""
        if "slot" in item:
            return "slot container"
        if item.get("widget") == "watch":
            return "analog watch"
        return item.get("type") or item.get("widget") or "widget"

    def where(self, item):
        return "@%d,%d %dx%d" % (item.get("x", 0), item.get("y", 0),
                                 item.get("w", 0), item.get("h", 0))

    def refresh_list(self):
        self.listbox.delete(0, tk.END)
        for item in self.items():
            self.listbox.insert(tk.END, self.widget_label(item))
        if self.selected is not None and 0 <= self.selected < len(self.items()):
            self.listbox.selection_set(self.selected)

    def say(self, text):
        self.status.config(text=text)

    # --- rendering ---------------------------------------------------------------------------

    def render(self):
        try:
            hour, minute = (int(part) for part in self.var_time.get().split(":"))
            day = max(1, self.var_day.get())
            month = max(1, min(12, self.var_month.get()))
            weekday = max(0, min(6, self.var_weekday.get()))
        except (ValueError, tk.TclError):
            return
        image = pic.render_face(self.layout, self.strips, self.background, hour, minute,
                                day, month, weekday, 5, self.var_fill.get() / 100.0,
                                seconds=self.safe_int(self.var_seconds),
                                battery=self.safe_int(self.var_battery),
                                condition=self.safe_int(self.var_weather))
        if tk.TkVersion < 8.6:
            raise SystemExit("Tk 8.6 is needed for PNG canvas images")
        buffer = io.BytesIO()
        image.save(buffer, format="PNG")
        self.photo = tk.PhotoImage(data=buffer.getvalue())
        self.canvas.delete("all")
        self.canvas.create_image(CANVAS_MARGIN, CANVAS_MARGIN, image=self.photo, anchor="nw")
        if self.selected is not None and 0 <= self.selected < len(self.items()):
            item = self.items()[self.selected]
            self.canvas.create_rectangle(
                CANVAS_MARGIN + item.get("x", 0), CANVAS_MARGIN + item.get("y", 0),
                CANVAS_MARGIN + item.get("x", 0) + item.get("w", 1),
                CANVAS_MARGIN + item.get("y", 0) + item.get("h", 1),
                outline="#ff4040", dash=(4, 3), width=2)

    def safe_int(self, variable, fallback=0):
        try:
            return max(0, variable.get())
        except tk.TclError:
            return fallback

    # --- selection and dragging --------------------------------------------------------------

    def item_at(self, x, y):
        """Widgets without their own box (the slot containers, whose children
        carry the coordinates) are only reachable through the list."""
        best = None
        best_area = None
        for index, item in enumerate(self.items()):
            if "w" not in item or "h" not in item:
                continue
            if x < item.get("x", 0) or y < item.get("y", 0):
                continue
            if x > item.get("x", 0) + item.get("w", 0) or y > item.get("y", 0) + item.get("h", 0):
                continue
            area = max(1, item.get("w", 1)) * max(1, item.get("h", 1))
            if best_area is None or area < best_area:
                best, best_area = index, area
        return best

    def on_press(self, event):
        found = self.item_at(event.x - CANVAS_MARGIN, event.y - CANVAS_MARGIN)
        if found is None:
            return
        self.select(found)
        item = self.items()[found]
        self.drag = (event.x - item.get("x", 0), event.y - item.get("y", 0))

    def on_motion(self, event):
        if self.drag is None or self.selected is None:
            return
        item = self.items()[self.selected]
        limit_x = self.background.size[0] - 1
        limit_y = self.background.size[1] - 1
        item["x"] = max(0, min(limit_x, event.x - self.drag[0]))
        item["y"] = max(0, min(limit_y, event.y - self.drag[1]))
        self.show_item()
        self.render()
        self.refresh_list()

    def on_release(self, event):
        self.drag = None
        if self.selected is not None:
            self.say("moved to %s" % self.where(self.items()[self.selected]))

    def on_list_select(self, _event):
        selection = self.listbox.curselection()
        if selection:
            self.select(selection[0], scroll=False)

    def select(self, index, scroll=True):
        self.selected = index
        if scroll:
            self.listbox.selection_clear(0, tk.END)
            self.listbox.selection_set(index)
            self.listbox.see(index)
        self.show_item()
        self.render()

    def show_item(self):
        if self.selected is None or not 0 <= self.selected < len(self.items()):
            return
        item = self.items()[self.selected]
        self.refreshing = True
        for name in ("x", "y", "w", "h"):
            self.entries[name].set(item.get(name, 0))
        self.refreshing = False
        self.json_box.delete("1.0", tk.END)
        self.json_box.insert("1.0", json.dumps(item, indent=1, ensure_ascii=False))

    # --- editing -----------------------------------------------------------------------------

    def apply_fields(self, live=False):
        if self.selected is None or not 0 <= self.selected < len(self.items()):
            return
        if live and self.refreshing:
            return
        item = self.items()[self.selected]
        for name in ("x", "y", "w", "h"):
            try:
                item[name] = max(0, self.entries[name].get())
            except tk.TclError:
                return
        if not live:
            self.show_item()
        self.render()
        self.refresh_list()

    def apply_json(self):
        if self.selected is None or not 0 <= self.selected < len(self.items()):
            return
        try:
            item = json.loads(self.json_box.get("1.0", tk.END))
        except ValueError as error:
            messagebox.showerror("JSON", str(error))
            return
        if not isinstance(item, dict):
            messagebox.showerror("JSON", "a widget must be a JSON object")
            return
        self.items()[self.selected] = item
        self.show_item()
        self.render()
        self.refresh_list()
        self.say("widget updated")

    def font_for(self, kind):
        """The strip set that can actually draw this widget type."""
        markers = MARKER_FOR.get(kind, ("_0_24bit",))
        for marker in markers:
            for name in self.fonts:
                if any(key.startswith(name + "_") and key.endswith(marker)
                       for key in self.strips):
                    return name
        return self.fonts[0] if self.fonts else "g51"

    def picture_members(self):
        """The whole-image members: everything that is not one of a font's
        strips, less the background and the preview."""
        skip = set()
        background = pic.background_path(self.png_dir, self.layout)
        if background:
            skip.add(os.path.splitext(os.path.basename(background))[0])
        skip.update(key for key in self.strips if key.startswith("preview"))
        return [key for key in sorted(self.strips)
                if key not in skip
                and not any(key.startswith(font + "_") for font in self.fonts)]

    def image_member_for(self):
        """A whole-image member to point a new icon widget at."""
        members = self.picture_members()
        for prefix in ("files", "custom_bg", "style_bg"):
            for key in members:
                if key.startswith(prefix):
                    return key + ".png"
        return members[0] + ".png" if members else None

    def hand_members(self):
        """Which member to point each analog hand at, by the word in its name.
        A face carries only the hands it draws (w3 has a second hand and
        nothing else), so a hand with no match is simply left out."""
        found = {}
        members = self.picture_members()
        for field, _, hints in HAND_HINTS:
            for hint in hints:
                match = next((key for key in members
                              if hint in key.lower() and key not in found.values()), None)
                if match:
                    found[field] = match
                    break
        return found

    def on_add(self):
        chosen = self.var_type.get()
        if chosen.startswith("progressbar"):
            item = {"widget": "progressbar", "x": 10, "y": 90, "w": 8, "h": 270,
                    "bgcolor": "0xFF000000", "bgrender": "0x0", "style": 2,
                    "type": chosen.split()[1], "fgcolor": "0xFF53E400",
                    "ring_width": 8, "ring_round": 0, "radius": 0}
        elif chosen == "watch (analog hands)":
            width, height = self.background.size
            hands = self.hand_members()
            item = {"widget": "watch", "type": "time", "x": 0, "y": 0,
                    "w": width, "h": height, "stepless_rotation": 0}
            for field, prefix, _ in HAND_HINTS:
                key = hands.get(field)
                if key is None:
                    continue
                glyph = self.strips[key]
                # The hand points up from a pivot near its tail, and turns
                # about the dial's centre — both are a guess until the face is
                # looked at, so they are the first thing to edit.
                item[field] = key + ".png"
                item[prefix + "centerx"] = glyph.width // 2
                item[prefix + "centery"] = glyph.height - max(2, glyph.height // 5)
                item[prefix + "anchorx"] = width // 2
                item[prefix + "anchory"] = height // 2
            note = (", ".join("%s: %s" % (field, hands[field])
                              for field, _, _ in HAND_HINTS if field in hands)
                    if hands else
                    "no member here looks like a hand — point hour/minute/second "
                    "at one in the JSON box")
        elif chosen == "slot container":
            icon_font = "icon" if "icon" in self.fonts else self.font_for("icon")
            centre = (self.background.size[0] // 2 - 34, self.background.size[1] // 2 - 34)
            item = {"slot": [{"widget": "container", "container": [
                {"widget": "custom", "type": "icon", "x": centre[0], "y": centre[1],
                 "w": 68, "h": 68, "font": icon_font, "fontnum": 11, "selfont": 0},
                {"widget": "custom", "type": "shortcut", "app": "sport",
                 "x": centre[0], "y": centre[1], "w": 68, "h": 68,
                 "fgcolor": "0xFFFFFFFF"}]}]}
        elif chosen == "icon":
            item = {"widget": "custom", "type": "icon", "x": 40, "y": 40, "w": 60, "h": 60,
                    "bg": self.image_member_for() or "", "bgcolor": "0xFFFFFFFF",
                    "bgrender": "0xFFFFFFFF"}
        elif chosen == "anima":
            frames = sum(1 for key in self.strips
                         if key.startswith("anima_") and key.rsplit("_", 1)[1].isdigit())
            item = {"widget": "custom", "type": "anima", "x": 29, "y": 74, "w": 332, "h": 302,
                    "animabpp": 16, "animaformat": "png", "animaicon": "anima",
                    "animatype": "stop_last", "frame": max(1, frames),
                    "time": 2500, "turn": 1}
        elif chosen in ("active_time", "walk_time"):
            item = {"widget": "custom", "type": chosen, "x": 40, "y": 40, "w": 40, "h": 24,
                    "align": "center", "fgcolor": "0xFFFFFFFF", "fgrender": "0xFFFFFFFF",
                    "fontFamily": "OPPOSans", "fontWeight": "300", "fontsize": 18,
                    "isShowAddwh": True, "rotangle": 0}
        else:
            item = {"widget": "custom", "type": chosen, "x": 10, "y": 10, "w": 122, "h": 40,
                    "fgcolor": "0xFFFFFFFF", "fgrender": "0x0", "align": "left",
                    "font": self.font_for(chosen), "fontnum": FONTNUM_FOR.get(chosen, 10)}
            if chosen == "month":
                item["style"], item["dmonth"] = 0, 0
            elif chosen == "date":
                item["style"] = 1
        self.items().append(item)
        self.refresh_list()
        self.select(len(self.items()) - 1)
        if chosen == "watch (analog hands)":
            self.say("added analog hands (%s) — not saved until you save" % note)
        else:
            self.say("added %s — not saved until you save" % chosen)

    def on_remove(self):
        if self.selected is None or not 0 <= self.selected < len(self.items()):
            return
        gone = self.items().pop(self.selected)
        self.selected = None
        self.refresh_list()
        self.json_box.delete("1.0", tk.END)
        self.render()
        self.say("removed %s — not saved until you save" % self.widget_name(gone))

    # --- saving and packing ------------------------------------------------------------------

    def save(self):
        with open(self.layout_path, "w") as handle:
            handle.write(json.dumps(self.layout, ensure_ascii=False, separators=(",", ":")))
        self.say("saved %s" % self.layout_path)

    def pack(self, packed):
        out = filedialog.asksaveasfilename(
            initialdir=os.path.dirname(self.dir),
            initialfile=os.path.basename(self.dir) + (".iwf.lz" if packed else ".iwf"),
            defaultextension=".iwf.lz" if packed else ".iwf")
        if not out:
            return
        self.save()
        command = [sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                                "iwf.py"), "pack", self.dir, out]
        if packed:
            command.append("--packed")
        result = subprocess.run(command, capture_output=True, text=True)
        self.say((result.stdout or result.stderr).strip())


def main():
    if len(sys.argv) != 2:
        raise SystemExit(__doc__.split("\n")[0] + "\nusage: iwf_gui.py FACE_DIR")
    root = tk.Tk()
    FaceEditor(root, sys.argv[1])
    root.mainloop()


if __name__ == "__main__":
    main()
