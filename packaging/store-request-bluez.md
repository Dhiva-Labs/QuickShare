# Snap Store auto-connect request: `bluez` for `nearshare`

Post this in the **store-requests** category at <https://forum.snapcraft.io>
(you need to be signed in with the Ubuntu SSO account that publishes the
snap — `dhivakar1010`). Canonical's store team reviews it; a decision
usually takes a few days.

**Title:**

```
Request auto-connect bluez for nearshare
```

**Body:**

---

Hello,

I would like to request auto-connection of the `bluez` interface for the
`nearshare` snap: <https://snapcraft.io/nearshare>

**What the snap does**

NearShare is an independent client for the Quick Share / Nearby Share
wire protocol, so a Linux desktop can exchange files with Android
phones, Windows PCs and macOS (via NearDrop). Source is MIT licensed at
<https://github.com/Dhiva-Labs/NearShare>.

**Why `bluez` is required rather than optional**

Discovery in this protocol has two halves. File transfer itself runs
over mDNS + TCP on the local network, which the already auto-connected
`network` and `network-bind` interfaces cover. However, an Android
device only begins actively looking for receivers once it observes a
Bluetooth LE advertisement on the 16-bit service UUID `0xFE2C` — the
"trigger" beacon defined by the protocol. Without it:

* the phone does not list the machine in its Quick Share sheet unless
  the user first opens the phone's own receive screen, and
* the app cannot notice that a nearby device is trying to share.

This is the same beacon the existing `rquickshare` implementation
broadcasts, and it carries no identity of its own — it is a fixed
payload that says "something here speaks Quick Share". The snap uses
BlueZ's `LEAdvertisingManager1` to advertise it and `StartDiscovery`
(filtered to that UUID, LE transport) to observe it.

The practical effect is that without `bluez` connected, the snap's
primary feature appears broken to a normal user: the phone simply does
not see their computer. Every reported "it does not find my laptop"
issue so far has traced back to `bluez` being unconnected after a fresh
install from the Snap Store, where nothing prompts the user to connect
it.

**Scope of use**

The snap uses `bluez` solely to advertise and scan for that one service
UUID. It does not pair, does not connect to devices, does not read
device names or addresses for any purpose beyond presence detection, and
does not transfer any file data over Bluetooth — all payload transfer
happens over the network interfaces. The relevant code is a single
module: `nearshare/core/ble.py`.

The snap is strictly confined and degrades gracefully when the interface
is absent: `BleAdvertiser.start()` raises, the failure is reported in the
UI as "Bluetooth discovery unavailable", and the app continues in
mDNS-only mode rather than failing.

Thank you for considering the request.

---

## After it is granted

Nothing needs rebuilding — auto-connection is a store-side assertion. It
applies to new installs immediately; existing installations keep whatever
connection state they already have, so a user who installed before the
grant may still need `snap connect nearshare:bluez` once.

`network-manager` is deliberately **not** requested here: it is only used
by the experimental Direct-mode hotspot, which is a secondary feature
that genuinely warrants an explicit user decision.
