"""mDNS discovery for Nearby Share / Quick Share peers on the LAN.

Quick Share's WiFi-LAN medium uses DNS-SD service type
``_FC9F5ED42C8A._tcp.`` (the hex string is Google's Nearby service id).

The *instance name* is base64url (no padding) of 10 bytes:
    [0]    PCP byte 0x23 (P2P_CLUSTER)
    [1:5]  4-byte endpoint id (random alphanumerics, ASCII)
    [5:8]  service id hash: 0xFC 0x9F 0x5E
    [8:10] 0x00 0x00

The TXT record key ``n`` is base64url of the endpoint-info blob (see
connection.build_endpoint_info): flags byte + 16 random bytes +
length-prefixed device name. Android's share sheet browses for this
service while the share UI is open and lists us as a target; likewise a
phone with Quick Share receiving enabled ("Everyone" visibility, screen
on the Quick Share sheet) advertises it so we can find the phone.
"""

from __future__ import annotations

import asyncio
import base64
import logging
import socket
from dataclasses import dataclass
from typing import Callable

from zeroconf import IPVersion, ServiceInfo, ServiceListener, Zeroconf
from zeroconf.asyncio import AsyncServiceBrowser, AsyncServiceInfo, AsyncZeroconf

from .connection import build_endpoint_info, parse_endpoint_info

log = logging.getLogger("quickshare.mdns")

SERVICE_TYPE = "_FC9F5ED42C8A._tcp.local."
PCP_P2P_CLUSTER = 0x23
SERVICE_ID_HASH = bytes([0xFC, 0x9F, 0x5E])


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def encode_instance_name(endpoint_id: str) -> str:
    raw = (bytes([PCP_P2P_CLUSTER]) + endpoint_id.encode("ascii") +
           SERVICE_ID_HASH + b"\x00\x00")
    return _b64(raw)


@dataclass
class Peer:
    instance: str
    endpoint_id: str
    device_name: str
    device_type: int
    host: str
    port: int


class Advertiser:
    """Advertise this machine as a Quick Share target."""

    def __init__(self, device_name: str, endpoint_id: str, port: int) -> None:
        self.device_name = device_name
        self.endpoint_id = endpoint_id
        self.port = port
        self._aiozc: AsyncZeroconf | None = None
        self._info: ServiceInfo | None = None

    async def start(self) -> None:
        if self._aiozc is not None:
            return
        instance = encode_instance_name(self.endpoint_id)
        txt = {"n": _b64(build_endpoint_info(self.device_name))}
        self._info = ServiceInfo(
            SERVICE_TYPE,
            f"{instance}.{SERVICE_TYPE}",
            port=self.port,
            properties=txt,
            server=f"{socket.gethostname()}-quickshare.local.",
            addresses=_local_addresses(),
        )
        self._aiozc = AsyncZeroconf(ip_version=IPVersion.V4Only)
        await self._aiozc.async_register_service(self._info)
        log.info("advertising as %r on port %d", self.device_name, self.port)

    async def stop(self) -> None:
        if self._aiozc is None:
            return
        try:
            await self._aiozc.async_unregister_service(self._info)
            await self._aiozc.async_close()
        finally:
            self._aiozc = None
            self._info = None

    @property
    def active(self) -> bool:
        return self._aiozc is not None


def _local_ipv4s() -> set[str]:
    """Every IPv4 assigned to this machine (for self-filtering peers).

    `ip -j addr` is authoritative on Ubuntu; the UDP-connect trick is
    the fallback when iproute2 is unavailable."""
    import json
    import subprocess
    addrs: set[str] = {"127.0.0.1"}
    try:
        out = subprocess.run(["ip", "-j", "-4", "addr"], timeout=5,
                             capture_output=True, check=True)
        for iface in json.loads(out.stdout):
            for info in iface.get("addr_info", []):
                if info.get("local"):
                    addrs.add(info["local"])
    except (OSError, subprocess.SubprocessError, ValueError):
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
                s.connect(("8.8.8.8", 80))
                addrs.add(s.getsockname()[0])
        except OSError:
            pass
    return addrs


_DEVICE_TYPE_LABEL = {1: "Phone", 2: "Tablet", 3: "Computer"}


def _local_addresses() -> list[bytes]:
    """Best-effort list of non-loopback IPv4 addresses."""
    addrs: list[bytes] = []
    try:
        # UDP connect trick: no packets sent, just picks the default route.
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("8.8.8.8", 80))
            addrs.append(socket.inet_aton(s.getsockname()[0]))
    except OSError:
        pass
    if not addrs:
        addrs.append(socket.inet_aton("127.0.0.1"))
    return addrs


class Browser:
    """Discover Quick Share receivers currently visible on the LAN."""

    def __init__(self, on_change: Callable[[dict[str, Peer]], None],
                 include_self: bool = False) -> None:
        self.peers: dict[str, Peer] = {}
        self.on_change = on_change
        self.include_self = include_self
        self._aiozc: AsyncZeroconf | None = None
        self._browser: AsyncServiceBrowser | None = None
        self._own_instances: set[str] = set()

    def ignore_instance(self, endpoint_id: str) -> None:
        """Don't list ourselves."""
        self._own_instances.add(encode_instance_name(endpoint_id))

    async def start(self) -> None:
        if self._aiozc is not None:
            return
        self._aiozc = AsyncZeroconf(ip_version=IPVersion.V4Only)
        loop = asyncio.get_running_loop()
        outer = self

        class _Listener(ServiceListener):
            def add_service(self, zc: Zeroconf, type_: str, name: str) -> None:
                asyncio.run_coroutine_threadsafe(outer._resolve(name), loop)

            def update_service(self, zc: Zeroconf, type_: str, name: str) -> None:
                asyncio.run_coroutine_threadsafe(outer._resolve(name), loop)

            def remove_service(self, zc: Zeroconf, type_: str, name: str) -> None:
                loop.call_soon_threadsafe(outer._remove, name)

        self._browser = AsyncServiceBrowser(self._aiozc.zeroconf, SERVICE_TYPE,
                                            listener=_Listener())

    async def stop(self) -> None:
        if self._aiozc is None:
            return
        try:
            await self._browser.async_cancel()
            await self._aiozc.async_close()
        finally:
            self._aiozc = None
            self._browser = None
            self.peers.clear()

    async def _resolve(self, name: str) -> None:
        instance = name.removesuffix("." + SERVICE_TYPE)
        if instance in self._own_instances:
            return
        info = AsyncServiceInfo(SERVICE_TYPE, name)
        if not await info.async_request(self._aiozc.zeroconf, 3000):
            return
        addresses = info.parsed_addresses(IPVersion.V4Only)
        if not addresses:
            return
        # Advertisements from THIS machine (the GUI app, the picker, a
        # CLI ephemeral service — each has its own endpoint id, so the
        # id-based ignore list alone can't catch siblings): drop them.
        if not self.include_self and addresses[0] in _local_ipv4s():
            return
        endpoint_id = "????"
        try:
            raw = _unb64(instance)
            if len(raw) >= 5:
                endpoint_id = raw[1:5].decode("ascii", "replace")
        except (ValueError, UnicodeDecodeError):
            pass
        device_name, device_type = "Unknown device", 0
        n = info.properties.get(b"n")
        if n:
            try:
                device_name, device_type = parse_endpoint_info(_unb64(n.decode()))
            except (ValueError, IndexError):
                pass
        if device_name == "Unknown device":
            # Phones in contact-based visibility advertise without a name
            # until a connection is made. Prefer a name learned from an
            # earlier transfer with this IP; else show something
            # identifying.
            from . import names as name_cache
            cached = name_cache.lookup(addresses[0])
            if cached:
                device_name = cached
            else:
                label = _DEVICE_TYPE_LABEL.get(device_type, "Device")
                device_name = f"{label} ({endpoint_id})"
        self.peers[name] = Peer(instance=instance, endpoint_id=endpoint_id,
                                device_name=device_name,
                                device_type=device_type,
                                host=addresses[0], port=info.port or 0)
        log.info("found peer %r at %s:%s", device_name, addresses[0], info.port)
        self.on_change(dict(self.peers))

    def _remove(self, name: str) -> None:
        if self.peers.pop(name, None) is not None:
            self.on_change(dict(self.peers))
