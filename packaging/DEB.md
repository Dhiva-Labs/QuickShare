# Building the `quickshare` .deb

Target: Ubuntu 24.04 (noble), `Architecture: all` (pure Python 3 + GTK4/
libadwaita via PyGObject — nothing to compile).

## Why this layout, not a Python dist-packages install

Upstream ships no `setup.py` or `pyproject.toml` — `quickshare/cli.py`'s
`install`/`uninstall` subcommands are a *from-source* dev-desktop-integration
tool (symlinks `bin/quickshare` into `~/.local/bin`, writes `.desktop` files
into `~/.local/share/applications`, etc.), not a packaging mechanism, and
adding a build-only `pyproject.toml` at the project root was out of scope for
this packaging pass (see the task this package was built under). So instead
of pybuild/dh-python installing into `/usr/lib/python3/dist-packages`, this
package uses the same shape as the BranchPilot `.deb` in this house:

- The whole `quickshare/` Python package tree is installed verbatim under
  `/usr/share/quickshare/quickshare/`.
- `/usr/bin/quickshare` is a thin wrapper (`debian/quickshare.wrapper`) that
  exports `PYTHONPATH=/usr/share/quickshare` and execs
  `/usr/bin/python3 -m quickshare.cli "$@"`.

Because `PYTHONPATH` is an exported environment variable, every place the
app re-launches itself — `quickshare gui` (`os.execv` in `cli.cmd_gui`),
`quickshare on`/`toggle` auto-starting the GUI when nothing is running
(`cli._launch_gui`, a `subprocess.Popen`), and the Nautilus script/extension
calling back into `quickshare send-picker` — inherits it too, so
`python3 -m quickshare...` keeps resolving no matter how deep the call
chain goes. `quickshare/__main__.py`'s own dist-packages fallback for `gi`
becomes dead code on a system python3 (which already sees `python3-gi`
directly) but is harmless.

## Generated protobuf bindings

`quickshare/proto/*_pb2.py` are gitignored upstream — they're tied to the
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

This produces `../quickshare_1.0.0-1_all.deb` (plus a `.changes` and
`.buildinfo` next to it, one directory up from the checkout).

To also build the source package (used for the PPA — see `packaging/PPA.md`):

```bash
dpkg-buildpackage -S -us -uc
```

`debian/source/format` is `3.0 (native)` (single git checkout, no separate
upstream tarball). `debian/source/options` excludes `.git`, the developer's
`.venv/`, `__pycache__/`/`*.pyc`, and the generated `quickshare/proto/*_pb2.py`
from the source tarball — without that, a native-format build happily packs
the entire `.git/` history and the dev virtualenv into the `.tar.xz`.

You'll see one harmless warning: `dpkg-source: warning: native package
version may not have a revision` — this package intentionally uses the
`1.0.0-1` debian-revision style version (per this project's packaging
convention) on top of a native source format; it still builds correctly.

## Verify the file list

```bash
dpkg -c ../quickshare_1.0.0-1_all.deb
```

Expected layout:

```
./usr/bin/quickshare                                                (wrapper, 0755)
./usr/share/applications/dev.dhivalabs.quickshare.desktop
./usr/share/applications/quickshare-send.desktop
./usr/share/applications/quickshare-toggle.desktop
./usr/share/doc/quickshare/changelog.Debian.gz
./usr/share/doc/quickshare/copyright
./usr/share/doc/quickshare/README.md.gz
./usr/share/doc/quickshare/SHORTCUT.md
./usr/share/icons/hicolor/scalable/apps/dev.dhivalabs.quickshare.svg
./usr/share/quickshare/quickshare/...                                (the app; cli.py, core/, ui/, proto/*_pb2.py freshly generated)
```

`lintian ../quickshare_1.0.0-1_all.deb` should report only two informational
warnings: `initial-upload-closes-no-bugs` (irrelevant outside Debian's NEW
queue) and `no-manual-page` (nice-to-have, not added in this pass).

## Test-install without root

To sanity-check the wrapper and Python import paths without `sudo`:

```bash
mkdir -p /tmp/qs-test && dpkg-deb -x ../quickshare_1.0.0-1_all.deb /tmp/qs-test
PYTHONPATH=/tmp/qs-test/usr/share/quickshare python3 -m quickshare.cli --help
```

This runs the exact same code the installed wrapper would run once
`/usr/share/quickshare` exists for real (the wrapper hardcodes that path, so
running it directly out of `/tmp/qs-test` requires either installing the
package or overriding `PYTHONPATH` as above and invoking the module directly
rather than `/tmp/qs-test/usr/bin/quickshare`).

## Install and remove

```bash
sudo apt install ./quickshare_1.0.0-1_all.deb   # pulls in Depends automatically
sudo apt remove quickshare
```

## Known gaps / follow-ups

- No man page for `quickshare` (lintian: `no-manual-page`) — cosmetic, not
  required for the package to work.
- `Homepage:` is left out of `debian/control` — no project URL was found in
  the repo (README/PLAN.md); add one if/when the project gets a public repo
  URL.
