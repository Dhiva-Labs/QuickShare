"""Regression test: a second instance must never start a second service.

A real phone listed this machine four times because four independent
instances were each advertising over mDNS. The cause was
_start_control_socket unconditionally unlinking the running instance's
socket and taking over, so every launch became a full second service
(own mDNS advertisement, own TCP listener, own BLE beacon).

Run:  .venv/bin/python -m tests.test_single_instance
"""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
from pathlib import Path

from nearshare.core.service import (AlreadyRunning, NearShareService,
                                    control_socket_path)


async def main() -> int:
    os.environ["XDG_RUNTIME_DIR"] = tempfile.mkdtemp(prefix="ns-run-")
    downloads = Path(tempfile.mkdtemp(prefix="ns-dl-"))

    first = NearShareService(device_name="first", download_dir=downloads)
    await first.start(visible=False)
    print(f"first instance started on port {first.port}")

    # --- a second instance must refuse ---------------------------------
    second = NearShareService(device_name="second", download_dir=downloads)
    try:
        await second.start(visible=False)
    except AlreadyRunning as exc:
        print(f"second instance correctly refused: {exc}")
    else:
        await second.stop()
        print("FAIL: second instance started alongside the first")
        return 1

    # The first instance must be untouched and still serving.
    sock = control_socket_path()
    assert sock.exists(), "first instance's socket was removed"
    reader, writer = await asyncio.open_unix_connection(str(sock))
    writer.write(b'{"cmd": "status"}\n')
    await writer.drain()
    line = await asyncio.wait_for(reader.readline(), timeout=5)
    writer.close()
    assert b'"device_name": "first"' in line, line
    print("first instance still serving its control socket")
    await first.stop()

    # --- a stale socket must be reclaimed, not treated as live ---------
    sock.parent.mkdir(parents=True, exist_ok=True)
    sock.touch()  # looks like a socket file, nothing listening
    third = NearShareService(device_name="third", download_dir=downloads)
    try:
        await third.start(visible=False)
    except AlreadyRunning:
        print("FAIL: stale socket wrongly treated as a live instance")
        return 1
    print("stale socket correctly reclaimed")
    await third.stop()

    print("\nSINGLE-INSTANCE REGRESSION TEST PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
