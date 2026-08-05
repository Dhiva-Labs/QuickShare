# GNOME keyboard shortcut for visibility toggle

NearShare has no system tray icon (GNOME doesn't provide one), so the
fastest way to flip "visible to nearby devices" on or off is a keyboard
shortcut bound to `nearshare toggle`. It works whether the app is
already running (it just flips visibility and fires a notification) or
not (it starts the app for you) — see the CLI section of the README.

**Shortest path**: run `bin/nearshare install` — as its last step it
prints the exact `gsettings` block from Option B below, already filled
in with your installed launcher's path, ready to paste. `install` never
runs these commands itself (they change your GNOME keybinding settings,
which is outside what an install script should silently do), it only
prints them.

Throughout, `<project>` means the absolute path to this repository, e.g.
`/home/alice/near_share`. If you installed `bin/nearshare` onto your
`PATH` (see README → Install), you can use the bare command `nearshare`
instead of the full path.

## Option A — GNOME Settings (GUI)

1. Open **Settings → Keyboard → Keyboard Shortcuts**.
2. Scroll to the bottom and click **View and Customize Shortcuts**, then
   **Custom Shortcuts**.
3. Click **+** (Add Shortcut).
4. Fill in:
   - **Name**: `NearShare: Toggle Visibility`
   - **Command**: `<project>/bin/nearshare toggle`
   - **Shortcut**: press your desired key combination, e.g.
     **Ctrl+Alt+N**.
5. Click **Add**. Test it: press the shortcut and you should get a
   desktop notification saying either "Visible to nearby devices" or
   "Hidden from nearby devices".

## Option B — one-shot `gsettings` commands

Paste this whole block into a terminal. It creates a new custom
keybinding without touching any existing ones, bound to **Ctrl+Alt+N**.
Replace `<project>` with your actual repository path first (or run the
`sed` line as-is if you `cd` into the repo first — it fills it in from
`pwd`).

```bash
# Run from inside the near_share repository directory.
PROJECT_DIR="$(pwd)"
KEY_BASE="org.gnome.settings-daemon.plugins.media-keys"
KEY_PATH="/org/gnome/settings-daemon/plugins/media-keys/custom-keybindings/nearshare-toggle/"

# 1. Register the new custom keybinding path alongside any existing ones.
existing="$(gsettings get "$KEY_BASE" custom-keybindings)"
if [[ "$existing" == "@as []" || "$existing" == "[]" ]]; then
    new="['$KEY_PATH']"
else
    new="$(echo "$existing" | sed "s/]$/, '$KEY_PATH']/")"
fi
gsettings set "$KEY_BASE" custom-keybindings "$new"

# 2. Set the command, name, and key combination for it.
gsettings set "$KEY_BASE.custom-keybinding:$KEY_PATH" \
    command "$PROJECT_DIR/bin/nearshare toggle"
gsettings set "$KEY_BASE.custom-keybinding:$KEY_PATH" \
    name "NearShare: Toggle Visibility"
gsettings set "$KEY_BASE.custom-keybinding:$KEY_PATH" \
    binding "<Control><Alt>n"

echo "Bound Ctrl+Alt+N to: $PROJECT_DIR/bin/nearshare toggle"
```

To remove it later, delete `$KEY_PATH` from the `custom-keybindings`
list with `gsettings set` (reverse of step 1) — GNOME Settings' Custom
Shortcuts panel also shows and lets you delete it directly.

## Verifying it worked

Run `<project>/bin/nearshare status` in a terminal — it should print
the current visibility, device name, and peer count if the app is
running, or `NearShare app is not running` otherwise. Then press your
shortcut and run `status` again to confirm the state flipped.
