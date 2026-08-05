"""Tests for nearshare.cli against a live, in-process NearShareService.

Monkeypatches XDG_RUNTIME_DIR to an isolated temp directory so the
control socket doesn't collide with a real running app, starts a
NearShareService, then calls the cli command functions directly
(building argparse.Namespace objects by hand rather than shelling out)
and asserts on their return codes and printed output.

Run:  .venv/bin/python -m tests.test_cli
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import io
import os
import sys
import tempfile
from pathlib import Path

from nearshare import cli
from nearshare.core.connection import Events
from nearshare.core.service import NearShareService, control_socket_path


def _ns(**kwargs) -> argparse.Namespace:
    kwargs.setdefault("json", False)
    return argparse.Namespace(**kwargs)


async def _run(func, *args):
    """cli.cmd_* functions use blocking sockets; run them off-thread so
    the service's asyncio event loop (in this same process, for the
    test) keeps servicing the control socket concurrently instead of
    deadlocking against itself."""
    return await asyncio.to_thread(func, *args)


@contextlib.contextmanager
def _captured():
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        yield out, err


async def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="qs-cli-test-"))
    runtime_dir = tmp / "run"
    runtime_dir.mkdir()
    os.environ["XDG_RUNTIME_DIR"] = str(runtime_dir)
    assert control_socket_path() == runtime_dir / "nearshare.sock"

    service = NearShareService(device_name="cli-test-device",
                                download_dir=tmp / "downloads",
                                events=Events())
    await service.start(visible=False)
    try:
        # --- status: hidden ------------------------------------------
        with _captured() as (out, err):
            rc = await _run(cli.cmd_status, _ns())
        assert rc == 0, err.getvalue()
        text = out.getvalue()
        assert "hidden" in text, text
        assert "cli-test-device" in text, text
        print("status (hidden): OK")

        # --- status --json ---------------------------------------------
        with _captured() as (out, err):
            rc = await _run(cli.cmd_status, _ns(json=True))
        assert rc == 0
        assert '"visible": false' in out.getvalue(), out.getvalue()
        print("status --json: OK")

        # --- on -----------------------------------------------------
        with _captured() as (out, err):
            rc = await _run(cli.cmd_on, _ns())
        assert rc == 0, err.getvalue()
        assert "Visible to nearby devices" in out.getvalue()
        assert service.visible is True
        print("on: OK")

        # --- status: visible now ----------------------------------------
        with _captured() as (out, err):
            await _run(cli.cmd_status, _ns())
        assert "NearShare is visible" in out.getvalue()
        print("status (visible): OK")

        # --- off ----------------------------------------------------
        with _captured() as (out, err):
            rc = await _run(cli.cmd_off, _ns())
        assert rc == 0
        assert "Hidden from nearby devices" in out.getvalue()
        assert service.visible is False
        print("off: OK")

        # --- toggle (hidden -> visible) ----------------------------------
        with _captured() as (out, err):
            rc = await _run(cli.cmd_toggle, _ns())
        assert rc == 0
        assert "Visible to nearby devices" in out.getvalue()
        assert service.visible is True
        print("toggle (-> visible): OK")

        # --- toggle again (visible -> hidden) ------------------------
        with _captured() as (out, err):
            rc = await _run(cli.cmd_toggle, _ns())
        assert rc == 0
        assert "Hidden from nearby devices" in out.getvalue()
        assert service.visible is False
        print("toggle (-> hidden): OK")

        # --- peers: the browser is live, so real LAN devices may appear;
        # assert the command works and both formats agree, not emptiness.
        import json as _json
        with _captured() as (out, err):
            rc = await _run(cli.cmd_peers, _ns(json=True))
        assert rc == 0
        listed = _json.loads(out.getvalue())["peers"]
        with _captured() as (out, err):
            rc = await _run(cli.cmd_peers, _ns())
        assert rc == 0
        if listed:
            for peer in listed:
                assert peer["name"] in out.getvalue()
        else:
            assert "No nearby devices found" in out.getvalue()
        print(f"peers (live browser, {len(listed)} seen): OK")

    finally:
        await service.stop()

    # --- not-running behaviour: no service listening on this socket -----
    with _captured() as (out, err):
        rc = cli.cmd_status(_ns())
    assert rc == 1
    assert cli.NOT_RUNNING_MSG in err.getvalue()
    print("status (not running): OK")

    with _captured() as (out, err):
        rc = cli.cmd_off(_ns())
    assert rc == 1
    assert cli.NOT_RUNNING_MSG in err.getvalue()
    print("off (not running): OK")

    with _captured() as (out, err):
        rc = cli.cmd_peers(_ns())
    assert rc == 1
    assert cli.NOT_RUNNING_MSG in err.getvalue()
    print("peers (not running): OK")

    # on/toggle fall back to launching the GUI instead of failing. We
    # can't actually exercise `python -m nearshare` (no __main__ yet,
    # owned by another agent), so just check the fallback is taken
    # (exit 0, message printed) rather than a hard failure, and that it
    # tried to launch something rather than silently no-op'ing.
    launched: list[list[str]] = []
    real_popen = cli.subprocess.Popen

    def fake_popen(args, **kwargs):
        launched.append(args)

        class _FakeProc:
            pass
        return _FakeProc()

    cli.subprocess.Popen = fake_popen
    try:
        with _captured() as (out, err):
            rc = cli.cmd_on(_ns())
        assert rc == 0, err.getvalue()
        assert "starting it now" in out.getvalue()
        assert launched and launched[0][1:] == ["-m", "nearshare"]
        print("on (not running, launches GUI): OK")

        launched.clear()
        with _captured() as (out, err):
            rc = cli.cmd_toggle(_ns())
        assert rc == 0
        assert launched
        print("toggle (not running, launches GUI): OK")
    finally:
        cli.subprocess.Popen = real_popen

    # --- argparse wiring smoke test ----------------------------------
    parser = cli.build_parser()
    args = parser.parse_args(["status", "--json"])
    assert args.func is cli.cmd_status and args.json is True
    args = parser.parse_args(["send", "a.jpg", "b.pdf", "--to", "Phone"])
    assert args.func is cli.cmd_send
    assert args.files == ["a.jpg", "b.pdf"] and args.to == "Phone"
    args = parser.parse_args(["send-picker", "a.jpg"])
    assert args.func is cli.cmd_send_picker and args.files == ["a.jpg"]
    args = parser.parse_args(["install"])
    assert args.func is cli.cmd_install
    args = parser.parse_args(["uninstall"])
    assert args.func is cli.cmd_uninstall
    print("argparse wiring: OK")

    test_install_uninstall()

    print("\nCLI TEST PASSED")
    return 0


# ------------------------------------------------------- install/uninstall

def test_install_uninstall() -> None:
    """`nearshare install`/`uninstall` against a fake $HOME, so the real
    developer machine's ~/.local/{bin,share} is never touched. HOME and
    XDG_DATA_HOME are monkeypatched just for this block and restored
    afterwards (unlike XDG_RUNTIME_DIR above, HOME affects Path.home()
    calls anywhere in the process, so leaving it patched could leak into
    other tests run later in the same process)."""
    orig_home = os.environ.get("HOME")
    orig_xdg_data = os.environ.get("XDG_DATA_HOME")
    tmp = Path(tempfile.mkdtemp(prefix="qs-install-test-"))
    fake_home = tmp / "home"
    fake_home.mkdir()
    os.environ["HOME"] = str(fake_home)
    os.environ["XDG_DATA_HOME"] = str(fake_home / ".local" / "share")

    try:
        bin_target = fake_home / ".local" / "bin" / "nearshare"
        apps_dir = fake_home / ".local" / "share" / "applications"
        nautilus_script = (fake_home / ".local" / "share" / "nautilus" /
                           "scripts" / "Send with NearShare")

        # --- install ---------------------------------------------------
        with _captured() as (out, err):
            rc = cli.cmd_install(_ns())
        assert rc == 0, err.getvalue()

        assert bin_target.is_symlink(), "launcher symlink not created"
        assert bin_target.resolve() == cli.BIN_SCRIPT.resolve()
        print("install: symlink OK")

        for name in cli._desktop_file_names():
            dest = apps_dir / name
            assert dest.is_file(), f"{dest} missing"
            content = dest.read_text()
            assert f"Exec={bin_target}" in content, content
        print("install: desktop files (Exec rewritten) OK")

        assert nautilus_script.is_file(), "nautilus script missing"
        mode = nautilus_script.stat().st_mode
        assert mode & 0o111, "nautilus script is not executable"
        assert str(bin_target) in nautilus_script.read_text()
        print("install: nautilus script (executable) OK")

        # --- install again: idempotent -----------------------------------
        with _captured() as (out, err):
            rc = cli.cmd_install(_ns())
        assert rc == 0, err.getvalue()
        assert "already in place" in out.getvalue()
        assert "already up to date" in out.getvalue()
        assert bin_target.is_symlink()
        print("install (re-run): idempotent OK")

        # --- uninstall ---------------------------------------------------
        with _captured() as (out, err):
            rc = cli.cmd_uninstall(_ns())
        assert rc == 0, err.getvalue()
        assert not bin_target.exists() and not bin_target.is_symlink()
        for name in cli._desktop_file_names():
            assert not (apps_dir / name).exists()
        assert not nautilus_script.exists()
        print("uninstall: clean removal OK")

        # --- uninstall again: idempotent, no errors -----------------------
        with _captured() as (out, err):
            rc = cli.cmd_uninstall(_ns())
        assert rc == 0, err.getvalue()
        assert "already removed" in out.getvalue()
        print("uninstall (re-run): idempotent OK")
    finally:
        if orig_home is not None:
            os.environ["HOME"] = orig_home
        else:
            os.environ.pop("HOME", None)
        if orig_xdg_data is not None:
            os.environ["XDG_DATA_HOME"] = orig_xdg_data
        else:
            os.environ.pop("XDG_DATA_HOME", None)


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
