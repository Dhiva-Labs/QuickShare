"""Security regression tests for peer-controlled input.

Everything a sender puts in an IntroductionFrame is attacker-controlled.
The filename in particular was an arbitrary-file-write primitive:
`download_dir / name` with an absolute name discards the download
directory entirely, and "../" walks out of it.

Run:  .venv/bin/python -m tests.test_security
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

from nearshare.core.connection import FileOffer, InboundConnection, safe_filename


ATTACKS = [
    "/etc/cron.d/evil",
    "../../../.bashrc",
    "../.ssh/authorized_keys",
    "..",
    ".",
    "....//....//etc/passwd",
    "sub/dir/photo.jpg",
    "back\\slash\\payload.sh",
    "with\x00null.txt",
    "carriage\r\nreturn.txt",
    ".hidden",
    "",
    "   ",
    "A" * 5000,
]


def test_safe_filename() -> None:
    for raw in ATTACKS:
        out = safe_filename(raw)
        assert "/" not in out, (raw, out)
        assert "\\" not in out, (raw, out)
        assert not out.startswith("."), (raw, out)
        assert out not in ("", ".", ".."), (raw, out)
        assert len(out) <= 200, (raw, out)
        assert not any(ord(c) < 32 or ord(c) == 127 for c in out), (raw, out)
    # Ordinary names must survive untouched.
    for good in ("photo.jpg", "My Report (final).pdf", "vidéo.mp4"):
        assert safe_filename(good) == good, good
    print(f"safe_filename: {len(ATTACKS)} hostile names neutralised, "
          "ordinary names preserved")


def test_open_dest_stays_inside_download_dir() -> None:
    root = Path(tempfile.mkdtemp(prefix="ns-sec-"))
    downloads = root / "Downloads"
    downloads.mkdir()
    outside = root / "SHOULD_NOT_EXIST.txt"

    conn = InboundConnection.__new__(InboundConnection)
    conn.download_dir = downloads

    for raw in ATTACKS + [str(outside)]:
        offer = FileOffer(payload_id=1, name=safe_filename(raw), size=0,
                          mime_type="application/octet-stream")
        conn._open_dest(offer)
        offer.handle.close()
        resolved = offer.dest_path.resolve()
        assert resolved.parent == downloads.resolve(), (raw, resolved)
    assert not outside.exists(), "wrote outside the download directory!"
    # Nothing escaped anywhere else in the sandbox either.
    strays = [p for p in root.iterdir() if p != downloads]
    assert not strays, strays
    print(f"_open_dest: {len(ATTACKS) + 1} attempts all confined to "
          f"{downloads.name}/")


def test_symlink_in_download_dir_not_followed() -> None:
    root = Path(tempfile.mkdtemp(prefix="ns-sec-link-"))
    downloads = root / "Downloads"
    downloads.mkdir()
    target = root / "victim.txt"
    target.write_text("original")
    # A dangling-or-live symlink planted where the next file will land.
    (downloads / "photo.jpg").symlink_to(target)

    conn = InboundConnection.__new__(InboundConnection)
    conn.download_dir = downloads
    offer = FileOffer(payload_id=1, name="photo.jpg", size=0,
                      mime_type="image/jpeg")
    conn._open_dest(offer)
    offer.handle.write(b"attacker content")
    offer.handle.close()

    assert target.read_text() == "original", "symlink was followed!"
    print("symlink: pre-planted link in the download dir was not followed")


def main() -> int:
    test_safe_filename()
    test_open_dest_stays_inside_download_dir()
    test_symlink_in_download_dir_not_followed()
    print("\nSECURITY TESTS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
