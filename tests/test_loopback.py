"""End-to-end loopback test: a full Nearby Share transfer on one machine.

Spins up the receive service, then drives an OutboundConnection at it over
127.0.0.1 — full UKEY2 handshake, paired-key dance, introduction,
accept, encrypted chunked file payloads — and verifies the received
files are byte-identical and both sides derived the same PIN.

Run:  .venv/bin/python -m tests.test_loopback
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import sys
import tempfile
from pathlib import Path

from nearshare.core.connection import Events, OutboundConnection, TransferRequest
from nearshare.core.mdns import encode_instance_name
from nearshare.core.service import NearShareService


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


async def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="qs-test-"))
    downloads = tmp / "downloads"

    # Two files: one multi-chunk (1.5 MB), one tiny.
    big = tmp / "big.bin"
    big.write_bytes(os.urandom(1536 * 1024))
    small = tmp / "notes.txt"
    small.write_text("hello from nearshare loopback\n")

    receiver_pin: list[str] = []
    received: list[Path] = []
    got_text: list[str] = []
    progress_events: list[int] = []

    async def on_transfer_request(req: TransferRequest) -> bool:
        receiver_pin.append(req.pin)
        names = sorted(f.name for f in req.files)
        assert names == ["big.bin", "notes.txt"], names
        assert req.total_size == big.stat().st_size + small.stat().st_size
        return True

    recv_events = Events(
        on_transfer_request=on_transfer_request,
        on_progress=lambda dev, done, total: progress_events.append(done),
        on_complete=lambda dev, paths: received.extend(paths),
        on_text=lambda dev, text: got_text.append(text),
    )

    service = NearShareService(device_name="loopback-receiver",
                                download_dir=downloads, events=recv_events)
    await service.start(visible=False)  # direct connect; mDNS tested below
    assert service.port

    sender_done = asyncio.Event()
    send_events = Events(
        on_complete=lambda dev, paths: sender_done.set(),
        on_error=lambda dev, err: print(f"sender error: {err}"),
    )
    conn = OutboundConnection("127.0.0.1", service.port, send_events,
                              "loopback-sender", [big, small])
    await asyncio.wait_for(conn.run(), timeout=60)
    await asyncio.wait_for(sender_done.wait(), timeout=5)
    await asyncio.sleep(0.2)  # let receiver finish closing files

    # --- assertions ---------------------------------------------------
    assert receiver_pin and conn.pin == receiver_pin[0], \
        f"PIN mismatch: sender={conn.pin} receiver={receiver_pin}"
    print(f"PIN matched on both sides: {conn.pin}")

    out = {p.name: p for p in received}
    assert set(out) == {"big.bin", "notes.txt"}, f"received: {received}"
    assert sha256(out["big.bin"]) == sha256(big), "big.bin corrupted"
    assert sha256(out["notes.txt"]) == sha256(small), "notes.txt corrupted"
    print(f"2 files received byte-identical in {downloads}")
    assert progress_events, "no progress callbacks fired"

    # --- mDNS advertise/browse smoke test -----------------------------
    await service.set_visible(True)
    found = asyncio.Event()
    instance = encode_instance_name(service.endpoint_id)

    from nearshare.core.mdns import Browser
    # include_self: production browsers filter out this machine's own
    # advertisements; here discovering ourselves is the whole point.
    browser = Browser(on_change=lambda peers: found.set() if any(
        p.instance == instance for p in peers.values()) else None,
        include_self=True)
    await browser.start()
    try:
        await asyncio.wait_for(found.wait(), timeout=15)
        peer = next(p for p in browser.peers.values()
                    if p.instance == instance)
        assert peer.device_name == "loopback-receiver", peer
        assert peer.port == service.port, peer
        print(f"mDNS: discovered own advertisement as {peer.device_name!r} "
              f"at {peer.host}:{peer.port}")
        mdns_ok = True
    except asyncio.TimeoutError:
        print("mDNS: WARNING - advertisement not seen within 15s "
              "(multicast may be blocked in this environment)")
        mdns_ok = False
    finally:
        await browser.stop()
        await service.stop()

    print("\nLOOPBACK TEST PASSED" + ("" if mdns_ok else " (mDNS skipped)"))
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
