"""Render an animated NearShare transfer offscreen to PNG frames.

GNOME's Wayland session has no headless capture and its screenshot
portal needs a user to approve a dialog, so this drives the real widget
classes (`_TransferRow` and the real stylesheet) through a transfer and
renders each step to a texture. `tools/make-gif.sh` turns the frames
into a GIF.

Usage:  .venv/bin/python tools/screencast.py [output-dir]
"""
import os
import sys

sys.path.append("/usr/lib/python3/dist-packages")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("Gdk", "4.0")
from gi.repository import Adw, GLib, Graphene, Gsk, Gtk  # noqa: E402

Adw.init()
Adw.StyleManager.get_default().set_color_scheme(Adw.ColorScheme.FORCE_DARK)

from nearshare.ui.app import _TransferRow, _install_css  # noqa: E402

OUT = sys.argv[1] if len(sys.argv) > 1 else "/tmp/nearshare-frames"
os.makedirs(OUT, exist_ok=True)
_install_css()

TOTAL = 47_300_000        # the file being received
SPEED = 6_800_000.0       # bytes/sec, a realistic 5 GHz WiFi rate

win = Gtk.Window(default_width=470, default_height=620, title="NearShare")
win.set_decorated(False)
tv = Adw.ToolbarView()
win.set_child(tv)
tv.add_top_bar(Adw.HeaderBar(
    title_widget=Adw.WindowTitle(title="NearShare",
                                 subtitle="dhivakar@thinkpad")))
page = Adw.PreferencesPage()
tv.set_content(page)

vis = Adw.PreferencesGroup()
switch = Adw.SwitchRow(
    title="Visible to nearby devices",
    subtitle="Visible as dhivakar@thinkpad — nearby devices can send you files")
switch.set_active(True)
vis.add(switch)
page.add(vis)

devices = Adw.PreferencesGroup(
    title="Nearby devices",
    description="Devices with Quick Share receiving open on the same network.")
for name, sub in (("Dhivakar's S21 FE", "Phone • 192.168.0.107"),
                  ("Pixel 9 Pro", "Phone • 192.168.0.131")):
    row = Adw.ActionRow(title=name, subtitle=sub)
    row.set_title_lines(1)
    row.add_prefix(Gtk.Image.new_from_icon_name("phone-symbolic"))
    button = Gtk.Button(label="Send files…", valign=Gtk.Align.CENTER)
    button.add_css_class("suggested-action")
    row.add_suffix(button)
    devices.add(row)
page.add(devices)

transfers = Adw.PreferencesGroup(title="Transfers")
page.add(transfers)

empty = Adw.ActionRow(title="No transfers yet",
                      subtitle="Files you send or receive will appear here.")
empty.set_sensitive(False)
transfers.add(empty)

win.present()

state = {"row": None, "n": 0}


def render(index: int) -> None:
    ctx = GLib.MainContext.default()
    paintable = Gtk.WidgetPaintable.new(win)
    win.queue_draw()
    for _ in range(40):
        while ctx.pending():
            ctx.iteration(False)
    w = win.get_width() or 470
    h = win.get_height() or 620
    snapshot = Gtk.Snapshot.new()
    paintable.snapshot(snapshot, w, h)
    node = snapshot.to_node()
    if node is None:
        return
    renderer = Gsk.CairoRenderer.new()
    renderer.realize(None)
    texture = renderer.render_texture(node, Graphene.Rect().init(0, 0, w, h))
    texture.save_to_png(os.path.join(OUT, f"frame-{index:03d}.png"))
    renderer.unrealize()


# 0-2: idle. 3: transfer starts. 3..27: progress. 28..33: completed hold.
STEPS = 34


def step() -> bool:
    i = state["n"]
    if i >= STEPS:
        loop.quit()
        return False

    if i == 3:
        transfers.remove(empty)
        state["row"] = _TransferRow(
            transfers, "Receiving from Dhivakar's S21 FE", TOTAL,
            lambda r: None)
    if 3 <= i <= 27 and state["row"] is not None:
        frac = (i - 3) / 24.0
        done = int(TOTAL * frac)
        # Pretend elapsed time matches the byte rate so the speed and
        # ETA the row derives look like a real transfer.
        state["row"].started_at = (
            state["row"].started_at
            if i == 3 else state["row"].started_at)
        elapsed = max(0.6, done / SPEED)
        state["row"].started_at = __import__("time").monotonic() - elapsed
        state["row"].update_progress(done, TOTAL)
    if i == 28 and state["row"] is not None:
        state["row"].mark_complete(None, 3)

    render(i)
    state["n"] += 1
    return True


loop = GLib.MainLoop()
GLib.timeout_add(400, step)
GLib.timeout_add_seconds(120, lambda: (loop.quit(), False)[1])
loop.run()
print(f"rendered {state['n']} frames into {OUT}")
