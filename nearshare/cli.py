"""Command-line control for the running NearShare app.

Talks JSON-lines to the Unix control socket the app's NearShareService
opens at nearshare.core.service.control_socket_path(): connect, send one
JSON request, read one JSON response line, print a human-friendly summary
(or the raw JSON with --json). See NearShareService._dispatch_control
for the request/response shapes.

Reality check: an Android phone (or any peer) only shows up in
`nearshare peers`, and can only be sent to, while its own Quick Share
receive screen (or share sheet) is open — Quick Share has no persistent
background discovery, so "not visible right now" is the normal state.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import shutil
import socket
import subprocess
import sys
from pathlib import Path
from typing import Any

from .core.mdns import Peer
from .core.service import control_socket_path

SOCKET_TIMEOUT = 5.0
PEER_WAIT_TIMEOUT = 20.0

NOT_RUNNING_MSG = "NearShare app is not running"
NO_PEERS_MSG = ("No nearby devices found. On the target device, open Quick "
               "Share's receive screen (or the share sheet) so it becomes "
               "discoverable.")

# ---------------------------------------------------- install/uninstall

# This file lives at <project>/nearshare/cli.py, so its grandparent is
# the project root -- resolved once here rather than re-derived per call.
PROJECT_ROOT = Path(__file__).resolve().parent.parent
BIN_SCRIPT = PROJECT_ROOT / "bin" / "nearshare"
DESKTOP_DIR = PROJECT_ROOT / "data"
NAUTILUS_SCRIPT_NAME = "Send with NearShare"


def _desktop_file_names() -> list[str]:
    """Every data/*.desktop file, by name -- globbed at call time (not a
    hardcoded list) so a new .desktop file dropped into data/ is picked
    up by install/uninstall without a code change."""
    return sorted(p.name for p in DESKTOP_DIR.glob("*.desktop"))


# ---------------------------------------------------------- control socket

def _control_request(req: dict[str, Any],
                     timeout: float = SOCKET_TIMEOUT) -> dict[str, Any] | None:
    """Send one JSON request to the running app; return its JSON response,
    or None if no app is listening on the control socket."""
    try:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(timeout)
        sock.connect(str(control_socket_path()))
    except OSError:
        return None
    try:
        sock.sendall(json.dumps(req).encode() + b"\n")
        buf = b""
        while not buf.endswith(b"\n"):
            chunk = sock.recv(65536)
            if not chunk:
                break
            buf += chunk
        return json.loads(buf) if buf else {"error": "empty response"}
    except OSError as exc:
        return {"error": str(exc)}
    finally:
        sock.close()


def _not_running() -> int:
    print(NOT_RUNNING_MSG, file=sys.stderr)
    return 1


def _launch_gui() -> None:
    """Start the GUI app detached, so the CLI/shortcut can return right
    away and the app keeps running after this process exits."""
    subprocess.Popen([sys.executable, "-m", "nearshare"],
                     start_new_session=True, stdin=subprocess.DEVNULL,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def _notify(summary: str, body: str) -> None:
    """Best-effort desktop notification; silently does nothing if
    notify-send isn't installed or fails."""
    if shutil.which("notify-send") is None:
        return
    try:
        subprocess.run(["notify-send", summary, body], check=False,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                       timeout=2)
    except OSError:
        pass


# ---------------------------------------------------------------- status

def cmd_status(args: argparse.Namespace) -> int:
    resp = _control_request({"cmd": "status"})
    if resp is None:
        return _not_running()
    if args.json:
        print(json.dumps(resp))
        return 0
    state = "visible" if resp.get("visible") else "hidden"
    print(f"NearShare is {state}")
    print(f"  device name : {resp.get('device_name', '?')}")
    if resp.get("port") is not None:
        print(f"  listening   : port {resp['port']}")
    print(f"  peers seen  : {resp.get('peers', 0)}")
    return 0


# ------------------------------------------------------------ visibility

def _cmd_visibility(args: argparse.Namespace, cmd: str) -> int:
    resp = _control_request({"cmd": cmd})
    if resp is None:
        if cmd in ("on", "toggle"):
            _launch_gui()
            print(f"{NOT_RUNNING_MSG}; starting it now (python -m "
                 "nearshare)...")
            return 0
        return _not_running()
    if args.json:
        print(json.dumps(resp))
        return 0
    state = ("Visible to nearby devices" if resp.get("visible")
            else "Hidden from nearby devices")
    print(state)
    if cmd == "toggle":
        _notify("NearShare", state)
    return 0


def cmd_on(args: argparse.Namespace) -> int:
    return _cmd_visibility(args, "on")


def cmd_off(args: argparse.Namespace) -> int:
    return _cmd_visibility(args, "off")


def cmd_toggle(args: argparse.Namespace) -> int:
    return _cmd_visibility(args, "toggle")


# ------------------------------------------------------------------ peers

def _format_peers(peers: list[dict[str, Any]]) -> str:
    if not peers:
        return NO_PEERS_MSG
    lines = [f"  {p['name']!r}  ({p['host']}:{p['port']})" for p in peers]
    return "Nearby devices:\n" + "\n".join(lines)


def cmd_peers(args: argparse.Namespace) -> int:
    resp = _control_request({"cmd": "peers"})
    if resp is None:
        return _not_running()
    if args.json:
        print(json.dumps(resp))
        return 0
    print(_format_peers(resp.get("peers", [])))
    return 0


# ------------------------------------------------------------------ names

def _resolve_target_ip(target: str) -> str | None:
    """Accept an IP directly, or match a peer by its displayed name."""
    if all(part.isdigit() for part in target.split(".")) and \
            target.count(".") == 3:
        return target
    resp = _control_request({"cmd": "peers"})
    if resp is None:
        return None
    needle = target.lower()
    for peer in resp.get("peers", []):
        if needle in peer.get("name", "").lower():
            return peer.get("host")
    return None


def cmd_rename(args: argparse.Namespace) -> int:
    from .core import names
    ip = _resolve_target_ip(args.target)
    if ip is None:
        print(f"No device matching {args.target!r}. Pass an IP, or run "
              "`nearshare peers` while the app is running.", file=sys.stderr)
        return 1
    names.remember(ip, args.name, manual=True)
    print(f"{ip} will now show as {args.name!r}")
    print("Reopen the send picker (or restart the app) to see it.")
    return 0


def cmd_forget(args: argparse.Namespace) -> int:
    from .core import names
    saved = names.all_names()
    if not args.target:
        if not saved:
            print("No saved device names.")
        for ip, name in sorted(saved.items()):
            print(f"  {ip:<16} {name}")
        return 0
    ip = _resolve_target_ip(args.target) or args.target
    if names.forget(ip):
        print(f"Forgot the saved name for {ip}")
        return 0
    print(f"No saved name for {ip}", file=sys.stderr)
    return 1


# -------------------------------------------------------------------- gui

def cmd_gui(args: argparse.Namespace) -> int:
    """Exec the GUI app in place of this process."""
    os.execv(sys.executable, [sys.executable, "-m", "nearshare"])
    return 0  # pragma: no cover - unreachable, execv replaces the process


# ------------------------------------------------------------------- send

def _resolve_files(paths: list[str]) -> tuple[list[str], list[str]]:
    """Return (absolute existing paths, originally-given missing paths)."""
    found, missing = [], []
    for raw in paths:
        p = Path(raw).expanduser().resolve()
        if p.is_file():
            found.append(str(p))
        else:
            missing.append(raw)
    return found, missing


def cmd_send(args: argparse.Namespace) -> int:
    files, missing = _resolve_files(args.files)
    if missing:
        print(f"No such file(s): {', '.join(missing)}", file=sys.stderr)
        return 1

    status = _control_request({"cmd": "status"})
    if status is not None:
        return _send_via_socket(files, args.to, args.json)
    return asyncio.run(_send_ephemeral(files, args.to))


def _send_via_socket(files: list[str], to: str | None,
                     as_json: bool) -> int:
    peer_name = to
    if not peer_name:
        presp = _control_request({"cmd": "peers"})
        peers = (presp or {}).get("peers", [])
        if not peers:
            print(NO_PEERS_MSG, file=sys.stderr)
            return 2
        if len(peers) > 1:
            print("Multiple devices found; pick one with --to:")
            for p in peers:
                print(f"  {p['name']}")
            return 2
        peer_name = peers[0]["name"]

    resp = _control_request({"cmd": "send", "peer": peer_name,
                             "files": files})
    if resp is None:
        return _not_running()
    if as_json:
        print(json.dumps(resp))
        return 1 if "error" in resp else 0
    if "error" in resp:
        print(f"Send failed: {resp['error']}", file=sys.stderr)
        return 1
    print(f"Sent to {resp.get('sent_to', peer_name)}. Check that device's "
         "screen — it shows the PIN so you can confirm the transfer.")
    return 0


async def _wait_for_peer(service: "Any", to: str | None) -> Peer | None:
    """Poll the service's mDNS browser for up to PEER_WAIT_TIMEOUT seconds.
    Returns the matching/only peer, or None (ambiguous or none found)."""
    loop = asyncio.get_event_loop()
    deadline = loop.time() + PEER_WAIT_TIMEOUT
    while loop.time() < deadline:
        if to:
            peer = service._match_peer(to)
            if peer is not None:
                return peer
        elif service.browser.peers:
            peers = list(service.browser.peers.values())
            return peers[0] if len(peers) == 1 else None
        await asyncio.sleep(0.3)
    return None


async def _send_ephemeral(files: list[str], to: str | None) -> int:
    """No app is running: start just enough of the service in-process to
    browse for peers and send directly, then tear it down."""
    from .core.connection import Events, OutboundConnection
    from .core.service import NearShareService

    service = NearShareService()
    print(f"{NOT_RUNNING_MSG}; looking for nearby devices as "
         f"{service.device_name!r} (waiting up to "
         f"{int(PEER_WAIT_TIMEOUT)}s)...")
    await service.browser.start()
    try:
        peer = await _wait_for_peer(service, to)
        if peer is None:
            if to:
                print(f"No device matching {to!r} found. On the target "
                     "device, open Quick Share's receive screen (or the "
                     "share sheet).", file=sys.stderr)
            elif len(service.browser.peers) > 1:
                print("Multiple devices found; pick one with --to:")
                for p in service.browser.peers.values():
                    print(f"  {p.device_name}")
            else:
                print(NO_PEERS_MSG, file=sys.stderr)
            return 2

        print(f"Found {peer.device_name!r} at {peer.host}:{peer.port}; "
             "connecting...")
        errors: list[str] = []
        events = Events(on_error=lambda dev, err: errors.append(err))
        paths = [Path(f) for f in files]
        conn = OutboundConnection(peer.host, peer.port, events,
                                  service.device_name, paths)
        run_task = asyncio.create_task(conn.run())
        pin_shown = False
        while not run_task.done():
            if conn.pin and not pin_shown:
                print(f"PIN: {conn.pin} — confirm it matches the "
                     f"other device's screen to complete the transfer.")
                pin_shown = True
            await asyncio.sleep(0.2)
        await run_task
        if errors:
            print(f"Send failed: {errors[0]}", file=sys.stderr)
            return 1
        print(f"Sent {len(paths)} file(s) to {peer.device_name}.")
        return 0
    except Exception as exc:  # surfaced as a clean CLI error, not a trace
        print(f"Send failed: {exc}", file=sys.stderr)
        return 1
    finally:
        await service.browser.stop()


# --------------------------------------------------------------- install
#
# `nearshare install` wires the project into the desktop: a PATH symlink,
# the .desktop launchers, and a Nautilus right-click script. Every step
# is idempotent (safe to re-run) and every path is resolved through
# Path.home() / $XDG_DATA_HOME at call time (not import time) so tests
# can monkeypatch HOME/XDG_DATA_HOME and exercise the real code against
# a throwaway fake home directory. `nearshare uninstall` reverses each
# step; neither subcommand touches the GNOME keyboard shortcut (that's a
# gsettings change to the user's own desktop config, so we only print the
# commands -- see _print_shortcut_commands).


def _xdg_data_home() -> Path:
    xdg = os.environ.get("XDG_DATA_HOME")
    return Path(xdg).expanduser() if xdg else Path.home() / ".local" / "share"


def _bin_dir() -> Path:
    return Path.home() / ".local" / "bin"


def _applications_dir() -> Path:
    return _xdg_data_home() / "applications"


def _nautilus_scripts_dir() -> Path:
    return _xdg_data_home() / "nautilus" / "scripts"


def _installed_bin_path() -> Path:
    return _bin_dir() / "nearshare"


def _path_has_dir(target: Path) -> bool:
    target_norm = os.path.normpath(str(target))
    return any(os.path.normpath(p) == target_norm
              for p in os.environ.get("PATH", "").split(os.pathsep) if p)


def _install_symlink() -> None:
    target = _installed_bin_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.is_symlink() and target.resolve() == BIN_SCRIPT.resolve():
        print(f"  symlink already in place: {target} -> {BIN_SCRIPT}")
    else:
        if target.is_symlink() or target.exists():
            target.unlink()
        target.symlink_to(BIN_SCRIPT)
        print(f"  symlinked {target} -> {BIN_SCRIPT}")
    if not _path_has_dir(_bin_dir()):
        print(f"  NOTE: {_bin_dir()} is not on your PATH. Add a line like "
             f"`export PATH=\"{_bin_dir()}:$PATH\"` to your shell profile "
             "(e.g. ~/.bashrc), or keep using the full path to this "
             "symlink.")


def _uninstall_symlink() -> None:
    target = _installed_bin_path()
    if target.is_symlink() or target.exists():
        target.unlink()
        print(f"  removed symlink {target}")
    else:
        print(f"  no symlink at {target} (already removed)")


def _rewrite_exec(content: str, nearshare_bin: Path) -> str:
    """Point a .desktop file's Exec= line at the installed launcher.

    e.g. "Exec=nearshare gui" -> "Exec=/home/alice/.local/bin/nearshare
    gui" -- the absolute path so the entry works even if ~/.local/bin
    isn't on PATH for the process that launches .desktop files."""
    return re.sub(r"^Exec=nearshare\b", f"Exec={nearshare_bin}", content,
                 flags=re.MULTILINE)


def _update_desktop_database(apps_dir: Path) -> None:
    """Best-effort re-index so the new/removed launchers show up in GNOME's
    app grid and search without a re-login; silently skipped if the tool
    isn't installed."""
    if shutil.which("update-desktop-database") is None:
        print("  update-desktop-database not found; skipping (best-effort, "
             "not required for the entries to work)")
        return
    try:
        subprocess.run(["update-desktop-database", str(apps_dir)],
                       check=False, stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL, timeout=10)
        print(f"  ran update-desktop-database {apps_dir}")
    except OSError as exc:
        print(f"  update-desktop-database failed (non-fatal): {exc}")


def _install_desktop_files() -> None:
    apps_dir = _applications_dir()
    apps_dir.mkdir(parents=True, exist_ok=True)
    nearshare_bin = _installed_bin_path()
    for name in _desktop_file_names():
        content = (DESKTOP_DIR / name).read_text()
        rewritten = _rewrite_exec(content, nearshare_bin)
        dest = apps_dir / name
        if dest.exists() and dest.read_text() == rewritten:
            print(f"  {dest} already up to date")
        else:
            dest.write_text(rewritten)
            print(f"  installed {dest} (Exec rewritten to {nearshare_bin})")
    _update_desktop_database(apps_dir)


def _uninstall_desktop_files() -> None:
    apps_dir = _applications_dir()
    removed_any = False
    for name in _desktop_file_names():
        dest = apps_dir / name
        if dest.exists():
            dest.unlink()
            print(f"  removed {dest}")
            removed_any = True
        else:
            print(f"  {dest} not present (already removed)")
    if removed_any:
        _update_desktop_database(apps_dir)


_NAUTILUS_SCRIPT_TEMPLATE = """#!/usr/bin/env bash
# Installed by `nearshare install` -- re-run install to update, don't
# hand-edit (your changes would just be overwritten next install).
#
# Nautilus runs this with the user's selection in
# $NAUTILUS_SCRIPT_SELECTED_FILE_PATHS, one absolute path per line.
set -euo pipefail

files=()
while IFS= read -r line; do
    [[ -n "$line" ]] && files+=("$line")
done <<< "${{NAUTILUS_SCRIPT_SELECTED_FILE_PATHS:-}}"

if [[ ${{#files[@]}} -eq 0 ]]; then
    exit 0
fi

exec "{nearshare_bin}" send-picker "${{files[@]}}"
"""


_ICON_NAME = "dev.dhivalabs.nearshare.svg"
_ICON_SOURCE = PROJECT_ROOT / "data" / "icons" / _ICON_NAME
# Desktop files from older installs that current data/ no longer ships;
# cleaned up on install and uninstall.
_LEGACY_DESKTOP_FILES = ("nearshare.desktop",)


def _icon_dest() -> Path:
    return (_xdg_data_home() / "icons" / "hicolor" / "scalable" / "apps" /
            _ICON_NAME)


def _install_icon() -> None:
    dest = _icon_dest()
    dest.parent.mkdir(parents=True, exist_ok=True)
    content = _ICON_SOURCE.read_text()
    if dest.exists() and dest.read_text() == content:
        print(f"  icon already in place: {dest}")
    else:
        dest.write_text(content)
        print(f"  installed icon {dest}")
    # Refresh the icon cache so GNOME picks it up without re-login.
    if shutil.which("gtk-update-icon-cache"):
        subprocess.run(
            ["gtk-update-icon-cache", "-t",
             str(_xdg_data_home() / "icons" / "hicolor")],
            check=False, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, timeout=10)


def _uninstall_icon() -> None:
    dest = _icon_dest()
    if dest.exists():
        dest.unlink()
        print(f"  removed icon {dest}")
    else:
        print(f"  {dest} not present (already removed)")


def _remove_legacy_desktop_files() -> None:
    apps_dir = _applications_dir()
    for name in _LEGACY_DESKTOP_FILES:
        dest = apps_dir / name
        if dest.exists():
            dest.unlink()
            print(f"  removed legacy {dest}")


# A nautilus-python extension gives a TOP-LEVEL context-menu item (the
# Scripts submenu is the best plain scripts can do). Requires the distro
# package `python3-nautilus`; installing the file without it is harmless.
_NAUTILUS_EXTENSION_TEMPLATE = '''"""Top-level "Send with NearShare" context-menu item for Nautilus.

Installed by `nearshare install`. Requires the python3-nautilus package
(apt). Nautilus loads this from ~/.local/share/nautilus-python/extensions.
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
        subprocess.Popen(["{launcher}", "send-picker", *paths],
                         start_new_session=True)
'''


def _nautilus_extensions_dir() -> Path:
    return _xdg_data_home() / "nautilus-python" / "extensions"


def _nautilus_python_available() -> bool:
    """True if the python3-nautilus distro package is importable."""
    try:
        import gi
        gi.require_version("Nautilus", "4.0")
        return True
    except (ImportError, ValueError):
        return False


def _install_nautilus_extension() -> None:
    ext_dir = _nautilus_extensions_dir()
    ext_dir.mkdir(parents=True, exist_ok=True)
    dest = ext_dir / "nearshare_menu.py"
    content = _NAUTILUS_EXTENSION_TEMPLATE.replace(
        "{launcher}", str(_installed_bin_path()))
    if dest.exists() and dest.read_text() == content:
        print(f"  extension already in place: {dest}")
    else:
        dest.write_text(content)
        print(f"  installed {dest}")
    if _nautilus_python_available():
        print("  python3-nautilus detected -- top-level right-click item "
              "active after: nautilus -q")
    else:
        print("  NOTE: the top-level right-click item needs one system "
              "package. Run this yourself, then restart Files:")
        print("      sudo apt install python3-nautilus && nautilus -q")
        print("  (Until then the item lives under right-click -> Scripts.)")


def _uninstall_nautilus_extension() -> None:
    dest = _nautilus_extensions_dir() / "nearshare_menu.py"
    if dest.exists():
        dest.unlink()
        print(f"  removed {dest} (restart Files: nautilus -q)")
    else:
        print(f"  {dest} not present (already removed)")


def _install_nautilus_script() -> None:
    scripts_dir = _nautilus_scripts_dir()
    scripts_dir.mkdir(parents=True, exist_ok=True)
    dest = scripts_dir / NAUTILUS_SCRIPT_NAME
    content = _NAUTILUS_SCRIPT_TEMPLATE.format(
        nearshare_bin=_installed_bin_path())
    already_current = (dest.exists() and dest.read_text() == content and
                       os.access(dest, os.X_OK))
    if already_current:
        print(f"  {dest} already up to date")
    else:
        dest.write_text(content)
        dest.chmod(0o755)
        print(f"  installed Nautilus script: {dest}")
    print("  restart Nautilus for the script to appear: nautilus -q")


def _uninstall_nautilus_script() -> None:
    dest = _nautilus_scripts_dir() / NAUTILUS_SCRIPT_NAME
    if dest.exists():
        dest.unlink()
        print(f"  removed Nautilus script {dest}")
        print("  restart Nautilus for the removal to take effect: "
             "nautilus -q")
    else:
        print(f"  {dest} not present (already removed)")


def _print_shortcut_commands() -> None:
    """Print (never run) the gsettings one-liner from docs/SHORTCUT.md,
    filled in with the installed launcher's absolute path -- gsettings
    custom-keybinding commands run outside any login shell, so PATH isn't
    reliable there even once ~/.local/bin is added to it."""
    nearshare_bin = _installed_bin_path()
    key_base = "org.gnome.settings-daemon.plugins.media-keys"
    key_path = ("/org/gnome/settings-daemon/plugins/media-keys/"
               "custom-keybindings/nearshare-toggle/")
    print("  To bind Super+Shift+S to toggle visibility (see "
         "docs/SHORTCUT.md for the GUI steps), run:")
    print()
    print(f'    existing="$(gsettings get {key_base} custom-keybindings)"')
    print('    if [[ "$existing" == "@as []" || "$existing" == "[]" ]]; then')
    print(f'        new="[\'{key_path}\']"')
    print('    else')
    print(f'''        new="$(echo "$existing" | sed "s/]$/, '{key_path}']/")"''')
    print('    fi')
    print(f'    gsettings set {key_base} custom-keybindings "$new"')
    print(f'    gsettings set {key_base}.custom-keybinding:{key_path} '
         f'command "{nearshare_bin} toggle"')
    print(f'    gsettings set {key_base}.custom-keybinding:{key_path} '
         'name "NearShare: Toggle Visibility"')
    print(f'    gsettings set {key_base}.custom-keybinding:{key_path} '
         'binding "<Super><Shift>s"')


def cmd_install(args: argparse.Namespace) -> int:
    print("Installing NearShare desktop integration...")
    print("[1/5] CLI launcher symlink")
    _install_symlink()
    print("[2/5] Desktop entries + app icon (~/.local/share)")
    _remove_legacy_desktop_files()
    _install_desktop_files()
    _install_icon()
    print("[3/5] Nautilus \"Send with NearShare\" script (Scripts submenu)")
    _install_nautilus_script()
    print("[4/5] Nautilus top-level right-click item (extension)")
    _install_nautilus_extension()
    print("[5/5] Keyboard shortcut (manual step -- nothing run automatically)")
    _print_shortcut_commands()
    print("\nInstall complete.")
    return 0


def cmd_uninstall(args: argparse.Namespace) -> int:
    print("Uninstalling NearShare desktop integration...")
    print("[1/4] CLI launcher symlink")
    _uninstall_symlink()
    print("[2/4] Desktop entries + app icon")
    _remove_legacy_desktop_files()
    _uninstall_desktop_files()
    _uninstall_icon()
    print("[3/4] Nautilus \"Send with NearShare\" script")
    _uninstall_nautilus_script()
    print("[4/4] Nautilus extension")
    _uninstall_nautilus_extension()
    print("\nUninstall complete.")
    return 0


# -------------------------------------------------------------- picker

def cmd_send_picker(args: argparse.Namespace) -> int:
    """Launch the GTK "send to nearby device" dialog (nearshare/ui/
    picker.py) for files already resolved on the command line -- this is
    what the installed Nautilus script execs, so it must work with no
    terminal attached and whether or not the main GUI app is running."""
    files, missing = _resolve_files(args.files)
    if missing:
        print(f"No such file(s): {', '.join(missing)}", file=sys.stderr)
    if not files:
        return 1
    from .ui.picker import main as picker_main
    return picker_main(files)


# ------------------------------------------------------------------- main

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="nearshare",
        description="Control the NearShare app: toggle visibility, list "
                    "nearby devices, and send files, all without opening "
                    "the GUI.",
        epilog="Reality check: nearby devices (including an Android phone) "
              "only show up here while their own Quick Share receive "
              "screen is open; there is no persistent background "
              "discovery.")

    json_parent = argparse.ArgumentParser(add_help=False)
    json_parent.add_argument("--json", action="store_true",
                             help="print the raw JSON response")

    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("on", parents=[json_parent],
                       help="make this machine visible to nearby devices")
    p.set_defaults(func=cmd_on)

    p = sub.add_parser("off", parents=[json_parent],
                       help="hide this machine from nearby devices")
    p.set_defaults(func=cmd_off)

    p = sub.add_parser("toggle", parents=[json_parent],
                       help="flip visibility and notify the new state")
    p.set_defaults(func=cmd_toggle)

    p = sub.add_parser("status", parents=[json_parent],
                       help="show visibility, device name, and peer count")
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("peers", parents=[json_parent],
                       help="list nearby discoverable devices")
    p.set_defaults(func=cmd_peers)

    p = sub.add_parser("gui", help="launch the NearShare GUI")
    p.set_defaults(func=cmd_gui)

    p = sub.add_parser(
        "rename", help="give a nearby device a friendly name",
        description="Assign a permanent name to a device by IP. Modern "
                    "Android hides its user-set name in advertisements, "
                    "so NearShare otherwise shows e.g. 'Phone (6SWJ)' "
                    "until that device sends you something.")
    p.add_argument("target", metavar="IP_OR_NAME",
                   help="device IP (see `nearshare peers`), or its "
                        "currently-shown name")
    p.add_argument("name", help="the name to show from now on")
    p.set_defaults(func=cmd_rename)

    p = sub.add_parser("forget", help="remove a saved device name")
    p.add_argument("target", metavar="IP_OR_NAME", nargs="?",
                   help="device to forget; omit to list saved names")
    p.set_defaults(func=cmd_forget)

    p = sub.add_parser(
        "send", parents=[json_parent],
        help="send file(s) to a nearby device",
        description="Send file(s) to a nearby device. The target only "
                    "shows up while its Quick Share receive screen (or "
                    "share sheet) is open.")
    p.add_argument("files", nargs="+", help="file(s) to send")
    p.add_argument("--to", metavar="NAME",
                  help="target device name (default: the only peer "
                       "found, or an error if there are several)")
    p.set_defaults(func=cmd_send)

    p = sub.add_parser(
        "send-picker",
        help="GTK dialog to pick a nearby device to send file(s) to",
        description="Open a small GTK dialog listing nearby devices for "
                    "the given file(s); used by the Nautilus right-click "
                    "script installed by `nearshare install`, but can be "
                    "run directly too.")
    p.add_argument("files", nargs="+", help="file(s) to send")
    p.set_defaults(func=cmd_send_picker)

    p = sub.add_parser(
        "install",
        help="install the PATH symlink, .desktop launchers, and Nautilus "
            "right-click script (idempotent)")
    p.set_defaults(func=cmd_install)

    p = sub.add_parser(
        "uninstall",
        help="remove everything `nearshare install` set up")
    p.set_defaults(func=cmd_uninstall)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
