# Building the `nearshare` .deb

Target: Ubuntu 24.04 (noble), `Architecture: all` (pure Python 3 + GTK4/
libadwaita via PyGObject — nothing to compile).

## Why this layout, not a Python dist-packages install

Upstream ships no `setup.py` or `pyproject.toml` — `nearshare/cli.py`'s
`install`/`uninstall` subcommands are a *from-source* dev-desktop-integration
tool (symlinks `bin/nearshare` into `~/.local/bin`, writes `.desktop` files
into `~/.local/share/applications`, etc.), not a packaging mechanism, and
adding a build-only `pyproject.toml` at the project root was out of scope for
this packaging pass (see the task this package was built under). So instead
of pybuild/dh-python installing into `/usr/lib/python3/dist-packages`, this
package uses the same shape as the BranchPilot `.deb` in this house:

- The whole `nearshare/` Python package tree is installed verbatim under
  `/usr/share/nearshare/nearshare/`.
- `/usr/bin/nearshare` is a thin wrapper (`debian/nearshare.wrapper`) that
  exports `PYTHONPATH=/usr/share/nearshare` and execs
  `/usr/bin/python3 -m nearshare.cli "$@"`.

Because `PYTHONPATH` is an exported environment variable, every place the
app re-launches itself — `nearshare gui` (`os.execv` in `cli.cmd_gui`),
`nearshare on`/`toggle` auto-starting the GUI when nothing is running
(`cli._launch_gui`, a `subprocess.Popen`), and the Nautilus script/extension
calling back into `nearshare send-picker` — inherits it too, so
`python3 -m nearshare...` keeps resolving no matter how deep the call
chain goes. `nearshare/__main__.py`'s own dist-packages fallback for `gi`
becomes dead code on a system python3 (which already sees `python3-gi`
directly) but is harmless.

## Generated protobuf bindings

`nearshare/proto/*_pb2.py` are gitignored upstream — they're tied to the
exact protobuf runtime version that generated them. `debian/rules`
regenerates them at build time via `tools/genproto.sh`, using whatever
`python3-grpc-tools` (the actual Debian/Ubuntu package name — **not**
`python3-grpcio-tools`, which does not exist in noble) this build
Build-Depends on, so the generated code is always compiled against the same
protobuf runtime this package Depends on (`python3-protobuf`). This was
verified end to end: bindings regenerated with noble's `python3-grpc-tools`
1.14.1 import cleanly against noble's `python3-protobuf` 3.21.12 (old-style,
non-versioned codegen — no `ValidateProtobufRuntimeVersion` guard to trip).

If `python3-grpc-tools` isn't installed, `debian/rules override_dh_auto_build`
fails immediately with a clear message telling you which package to install,
rather than a confusing traceback deep inside `tools/genproto.sh`.

## Build dependencies

```bash
sudo apt install debhelper python3 python3-grpc-tools
# or, from a clean checkout:
sudo apt build-dep .
```

Everything else needed to run the app (`python3-gi`, `gir1.2-gtk-4.0`,
`gir1.2-adw-1`, `python3-zeroconf`, `python3-protobuf`,
`python3-cryptography`) is a runtime Depends, not a build dependency — the
build itself never imports them.

## Build

From a clean checkout of this repository:

```bash
dpkg-buildpackage -us -uc -b
```

This produces `../nearshare_1.0.0-1_all.deb` (plus a `.changes` and
`.buildinfo` next to it, one directory up from the checkout).

To also build the source package (used for the PPA — see `packaging/PPA.md`):

```bash
dpkg-buildpackage -S -us -uc
```

`debian/source/format` is `3.0 (native)` (single git checkout, no separate
upstream tarball). `debian/source/options` excludes `.git`, the developer's
`.venv/`, `__pycache__/`/`*.pyc`, and the generated `nearshare/proto/*_pb2.py`
from the source tarball — without that, a native-format build happily packs
the entire `.git/` history and the dev virtualenv into the `.tar.xz`.

You'll see one harmless warning: `dpkg-source: warning: native package
version may not have a revision` — this package intentionally uses the
`1.0.0-1` debian-revision style version (per this project's packaging
convention) on top of a native source format; it still builds correctly.

## Verify the file list

```bash
dpkg -c ../nearshare_1.0.0-1_all.deb
```

Expected layout:

```
./usr/bin/nearshare                                                (wrapper, 0755)
./usr/share/applications/dev.dhivalabs.nearshare.desktop
./usr/share/applications/nearshare-send.desktop
./usr/share/applications/nearshare-toggle.desktop
./usr/share/doc/nearshare/changelog.Debian.gz
./usr/share/doc/nearshare/copyright
./usr/share/doc/nearshare/README.md.gz
./usr/share/doc/nearshare/SHORTCUT.md
./usr/share/icons/hicolor/scalable/apps/dev.dhivalabs.nearshare.svg
./usr/share/nearshare/nearshare/...                                (the app; cli.py, core/, ui/, proto/*_pb2.py freshly generated)
```

`lintian ../nearshare_1.0.0-1_all.deb` should report only two informational
warnings: `initial-upload-closes-no-bugs` (irrelevant outside Debian's NEW
queue) and `no-manual-page` (nice-to-have, not added in this pass).

## Test-install without root

To sanity-check the wrapper and Python import paths without `sudo`:

```bash
mkdir -p /tmp/qs-test && dpkg-deb -x ../nearshare_1.0.0-1_all.deb /tmp/qs-test
PYTHONPATH=/tmp/qs-test/usr/share/nearshare python3 -m nearshare.cli --help
```

This runs the exact same code the installed wrapper would run once
`/usr/share/nearshare` exists for real (the wrapper hardcodes that path, so
running it directly out of `/tmp/qs-test` requires either installing the
package or overriding `PYTHONPATH` as above and invoking the module directly
rather than `/tmp/qs-test/usr/bin/nearshare`).

## Install and remove

```bash
sudo apt install ./nearshare_1.0.0-1_all.deb   # pulls in Depends automatically
sudo apt remove nearshare
```

## Known gaps / follow-ups

- No man page for `nearshare` (lintian: `no-manual-page`) — cosmetic, not
  required for the package to work.
- `Homepage:` is left out of `debian/control` — no project URL was found in
  the repo (README/PLAN.md); add one if/when the project gets a public repo
  URL.
