"""Top-level "Send with NearShare" context-menu item for Nautilus.

Installed SYSTEM-WIDE by the nearshare Debian package at
/usr/share/nautilus-python/extensions/nearshare_menu.py -- this is one of
nautilus-python's three documented extension search paths (the other two,
$XDG_DATA_HOME/nautilus-python/extensions and the nautilus_prefix one, are
per-user/per-prefix; see packaging/DEB.md), so this file is loaded for
every user with no per-user step. Requires the python3-nautilus package
(apt) to actually load -- this package Recommends it, but installing this
file without it is harmless (Nautilus just never imports it).

This is a build-time copy of nearshare/cli.py's _NAUTILUS_EXTENSION_TEMPLATE
with "{launcher}" hardcoded to /usr/bin/nearshare (the deb's wrapper path)
instead of being filled in per-user from _installed_bin_path(). Keep the
get_file_items/_activate logic here in sync with that template by hand --
this file is packaging-owned (debian/**), not generated from cli.py at
build time.
"""
import subprocess
from urllib.parse import unquote, urlparse

from gi.repository import GObject, Nautilus


class NearShareMenu(GObject.GObject, Nautilus.MenuProvider):
    def get_file_items(self, files):
        paths = []
        for f in files:
            if f.get_uri_scheme() != "file" or f.is_directory():
                return []
            paths.append(unquote(urlparse(f.get_uri()).path))
        if not paths:
            return []
        item = Nautilus.MenuItem(
            name="NearShareMenu::send",
            label="Send with NearShare",
            tip="Send the selected files to a nearby device")
        item.connect("activate", self._activate, paths)
        return [item]

    def _activate(self, _item, paths):
        subprocess.Popen(["/usr/bin/nearshare", "send-picker", *paths],
                         start_new_session=True)
