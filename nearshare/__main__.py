"""Entry point for `python -m nearshare` (the GTK app), run from the
project's .venv.

Why this file has to fiddle with sys.path: PyGObject (`gi`) and its
GTK4/libadwaita typelibs come from the distro's `python3-gi` /
`gir1.2-gtk-4.0` / `gir1.2-adw-1` packages under
`/usr/lib/python3/dist-packages` -- that's how GNOME/Ubuntu ships them,
and there's no PyPI wheel that reliably matches a given system's
GObject-Introspection typelibs, so we can't just `pip install pygobject`
into the venv and call it done. The project's venv was deliberately
created without `--system-site-packages` (so pip-installed zeroconf/
cryptography versions don't collide with distro packages), which means
`import gi` fails from inside it even though the venv's `python3` binary
is the very same interpreter as the system one (see .venv/pyvenv.cfg:
`include-system-site-packages = false`).

So: try importing `gi` normally first (covers running under the system
python3, or a future venv built with --system-site-packages), and only
if that fails, append the distro's dist-packages directory ourselves.
This is the one deliberate crack in the venv's isolation, scoped to
exactly the GTK bindings.
"""

from __future__ import annotations

import sys


def _ensure_gi_importable() -> None:
    try:
        import gi  # noqa: F401
        return
    except ImportError:
        pass

    candidates = [
        "/usr/lib/python3/dist-packages",
        f"/usr/lib/python3.{sys.version_info.minor}/dist-packages",
        "/usr/local/lib/python3/dist-packages",
    ]
    for path in candidates:
        if path not in sys.path:
            sys.path.append(path)

    try:
        import gi  # noqa: F401
    except ImportError as exc:
        sys.exit(
            "nearshare: could not import PyGObject ('gi') even after "
            "adding the system dist-packages directories to sys.path. "
            "Install python3-gi, gir1.2-gtk-4.0, and gir1.2-adw-1 (e.g. "
            "`sudo apt install python3-gi gir1.2-gtk-4.0 gir1.2-adw-1`) "
            f"and try again.\n({exc})")


_ensure_gi_importable()

from nearshare.ui.app import main  # noqa: E402  (must follow the sys.path fix)

if __name__ == "__main__":
    sys.exit(main())
