"""Direct mode: an ad-hoc WiFi access point via NetworkManager's `nmcli`.

Experimental substitute for WiFi Direct/P2P group negotiation (see
PLAN.md section 7): when the Linux machine and the sending phone share
no WiFi network, we stand up a temporary WPA2 hotspot with `nmcli device
wifi hotspot`, show the phone a QR code (see ui/qrcode.py) to join it,
and let the existing mDNS + Nearby Share flow run on that subnet as
usual. Torn down with `nmcli connection down/delete`.

`nmcli` calls can fail for reasons entirely outside our control --
missing polkit authorization, a WiFi adapter without AP-mode support, no
WiFi hardware at all, NetworkManager not running -- so every method here
raises `HotspotError` with nmcli's own stderr folded in rather than
letting a bare CalledProcessError/OSError reach the UI. The UI layer is
expected to catch `HotspotError` and show it as a readable dialog rather
than a stack trace (see PLAN.md M3/M4 acceptance criteria).
"""

from __future__ import annotations

import asyncio
import logging
import random
import string

log = logging.getLogger("quickshare.hotspot")

CONNECTION_NAME = "quickshare-direct"


class HotspotError(Exception):
    """Raised when nmcli fails; str(exc) is safe to show to the user."""


def _random_suffix(n: int) -> str:
    return "".join(random.choices(string.ascii_uppercase + string.digits, k=n))


def _random_password(n: int = 12) -> str:
    # WPA2-PSK requires 8-63 chars; letters+digits avoid QR/URI escaping
    # headaches (no ';', ':', ',', '"', '\' to worry about).
    alphabet = string.ascii_letters + string.digits
    return "".join(random.choices(alphabet, k=n))


async def _run_nmcli(*args: str) -> str:
    """Run nmcli, returning stdout; raises HotspotError with stderr on
    nonzero exit or if the binary itself can't be found/launched."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "nmcli", *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()
    except FileNotFoundError as exc:
        raise HotspotError(
            "nmcli not found; NetworkManager must be installed for "
            "Direct mode") from exc
    except OSError as exc:
        raise HotspotError(f"could not run nmcli: {exc}") from exc

    if proc.returncode != 0:
        message = stderr.decode(errors="replace").strip() or "nmcli failed"
        # polkit denials show up as this cryptic sentence; give the user
        # something actionable instead of quoting D-Bus internals.
        if "Not authorized" in message or "polkit" in message.lower():
            message = ("NetworkManager denied permission to create a "
                       "hotspot (polkit authorization required). Try "
                       "running from a graphical session with an active "
                       "polkit agent, or check your distro's NetworkManager "
                       "permissions.")
        raise HotspotError(message)
    return stdout.decode(errors="replace")


async def find_wifi_interface() -> str | None:
    """Return the first WiFi device name, or None if there isn't one."""
    out = await _run_nmcli("-t", "-f", "DEVICE,TYPE", "device")
    for line in out.splitlines():
        parts = line.split(":")
        if len(parts) == 2 and parts[1] == "wifi":
            return parts[0]
    return None


class Hotspot:
    """Owns the lifecycle of one `quickshare-direct` AP connection profile.

    Only one instance should be "up" at a time (enforced by the UI, which
    only shows one Direct-mode switch); this class does not itself guard
    against concurrent starts.
    """

    def __init__(self) -> None:
        self.active = False
        self.ssid: str | None = None
        self.password: str | None = None
        self.interface: str | None = None

    async def start(self) -> tuple[str, str]:
        """Create the hotspot; returns (ssid, password). Raises
        HotspotError on any failure, and leaves no partial connection
        profile behind (best-effort delete on partial failure)."""
        if self.active:
            raise HotspotError("hotspot is already running")

        interface = await find_wifi_interface()
        if interface is None:
            raise HotspotError(
                "no WiFi adapter found; Direct mode needs a WiFi interface "
                "capable of access-point mode")

        ssid = f"QuickShare-{_random_suffix(4)}"
        password = _random_password()

        try:
            await _run_nmcli(
                "device", "wifi", "hotspot",
                "ifname", interface,
                "con-name", CONNECTION_NAME,
                "ssid", ssid,
                "password", password,
            )
        except HotspotError:
            # nmcli sometimes half-creates the profile before failing
            # (e.g. AP mode unsupported after the profile is written);
            # clean up so a retry isn't blocked by a stale connection.
            await self._delete_profile()
            raise

        self.active = True
        self.ssid = ssid
        self.password = password
        self.interface = interface
        log.info("hotspot up: ssid=%r iface=%s", ssid, interface)
        return ssid, password

    async def stop(self) -> None:
        """Tear down the hotspot and restore the previous WiFi connection.

        Safe to call even if start() never succeeded (e.g. app shutdown
        after a failed start); nmcli errors here are logged, not raised,
        since there's nothing further the UI can do about a teardown
        failure other than know about it.
        """
        if not self.active:
            return
        try:
            await _run_nmcli("connection", "down", CONNECTION_NAME)
        except HotspotError as exc:
            log.warning("hotspot down failed (continuing to delete): %s", exc)
        await self._delete_profile()
        self.active = False
        self.ssid = None
        self.password = None
        self.interface = None
        log.info("hotspot torn down")

    async def _delete_profile(self) -> None:
        try:
            await _run_nmcli("connection", "delete", CONNECTION_NAME)
        except HotspotError as exc:
            # "no such connection" is expected/harmless if start() never
            # got far enough to create the profile.
            if "unknown connection" not in str(exc).lower():
                log.warning("hotspot profile delete failed: %s", exc)
