"""Render NearShare's real UI widgets offscreen to PNG.

Uses the application's own _TransferRow class and libadwaita styling --
the rows are the same objects the running app builds.
"""
import os
import sys
sys.path.append("/usr/lib/python3/dist-packages")
sys.path.insert(0, "/home/dhivakar/dhiva-labs/near_share")

import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("Gdk", "4.0")
from gi.repository import Adw, GLib, Graphene, Gsk, Gtk

Adw.init()
Adw.StyleManager.get_default().set_color_scheme(Adw.ColorScheme.FORCE_DARK)

from nearshare.ui.app import _TransferRow, _install_css

OUT = "/home/dhivakar/dhiva-labs/near_share/docs/screenshots"
os.makedirs(OUT, exist_ok=True)
_install_css()

win = Gtk.Window(default_width=470, default_height=640, title="NearShare")
win.set_decorated(False)
tv = Adw.ToolbarView()
win.set_child(tv)
tv.add_top_bar(Adw.HeaderBar(
    title_widget=Adw.WindowTitle(title="NearShare",
                                 subtitle="dhivakar@thinkpad")))

page = Adw.PreferencesPage()
tv.set_content(page)

vis = Adw.PreferencesGroup()
sw = Adw.SwitchRow(
    title="Visible to nearby devices",
    subtitle="Visible as dhivakar@thinkpad — nearby devices can send you files")
sw.set_active(True)
vis.add(sw)
page.add(vis)

devices = Adw.PreferencesGroup(
    title="Nearby devices",
    description="Devices with Quick Share receiving open on the same network.")
for name, sub in (("Dhivakar's S21 FE", "Phone • 192.168.0.107"),
                  ("Pixel 9 Pro", "Phone • 192.168.0.131")):
    r = Adw.ActionRow(title=name, subtitle=sub)
    r.set_title_lines(1)
    r.add_prefix(Gtk.Image.new_from_icon_name("phone-symbolic"))
    b = Gtk.Button(label="Send files…", valign=Gtk.Align.CENTER)
    b.add_css_class("suggested-action")
    r.add_suffix(b)
    devices.add(r)
page.add(devices)

transfers = Adw.PreferencesGroup(title="Transfers")
page.add(transfers)
row_in = _TransferRow(transfers, "Receiving from Dhivakar's S21 FE",
                      47_300_000, lambda r: None)
row_in.started_at -= 7.0
row_in.update_progress(28_900_000, 47_300_000)

row_done = _TransferRow(transfers, "Sent to Pixel 9 Pro", 12_400_000,
                        lambda r: None)
row_done.started_at -= 9.0
row_done.mark_complete(None, 3)

win.present()
frames = []


def render(name):
    ctx = GLib.MainContext.default()
    paintable = Gtk.WidgetPaintable.new(win)
    win.queue_draw()
    for _ in range(120):
        while ctx.pending():
            ctx.iteration(False)
    w, h = win.get_width() or 470, win.get_height() or 640
    snap = Gtk.Snapshot.new()
    paintable.snapshot(snap, w, h)
    node = snap.to_node()
    if node is None:
        print(f"  {name}: EMPTY")
        return
    r = Gsk.CairoRenderer.new()
    r.realize(None)
    tex = r.render_texture(node, Graphene.Rect().init(0, 0, w, h))
    p = os.path.join(OUT, name)
    tex.save_to_png(p)
    r.unrealize()
    frames.append(p)
    print(f"  wrote {p} ({w}x{h})")


def go():
    render("nearshare-transfer.png")
    loop.quit()
    return False


loop = GLib.MainLoop()
GLib.timeout_add(1200, go)
GLib.timeout_add_seconds(30, lambda: (loop.quit(), False)[1])
loop.run()
print("frames:", frames)
