# LinkedIn post — NearShare

Three drafts at different lengths and angles. Pick one, edit freely — the
voice should be yours. Facts in all three are accurate as of the 1.0.9
release; check the numbers again if you post later.

---

## Draft A — the straightforward launch post

Android phones and Windows PCs can send each other files instantly with
Quick Share. Linux was left out.

So I built **NearShare** — an open-source Quick Share / Nearby Share
client for the Linux desktop.

Drop a file on your laptop from your phone, or send one the other way,
with no cable, no cloud account, and no companion app. It speaks Google's
actual wire protocol, so it works with Android, Windows Quick Share,
macOS (via NearDrop), and other Linux machines running it.

A few things I care about in it:

→ Every transfer is end-to-end encrypted — UKEY2 key exchange, then
  AES-256-CBC with HMAC-SHA256 per frame, and a 4-digit PIN you confirm
  on both screens
→ Discovery over mDNS plus a Bluetooth LE beacon, so your phone finds
  your laptop instead of the other way round
→ Right-click a file in Files and send it
→ Install it in one command

    sudo snap install nearshare

MIT licensed, built on the protocol work published by the NearDrop and
rquickshare projects — this would not exist without them.

Code: github.com/Dhiva-Labs/NearShare
Store: snapcraft.io/nearshare

#Linux #OpenSource #Android #Ubuntu #GNOME

---

## Draft B — the engineering-story angle (usually performs better)

I spent a week implementing a protocol Google never documented, and the
most valuable part wasn't the cryptography.

**NearShare** brings Quick Share / Nearby Share to Linux — send files
between your desktop and any Android phone, Windows PC or Mac, with no
cable and no account.

The hard parts were not where I expected:

**The handshake was the easy bit.** UKEY2 key exchange, AES-256-CBC with
HMAC-SHA256, a PIN derived identically on both devices — all specified
closely enough by prior reverse-engineering work to implement directly.

**Discovery was harder.** mDNS alone doesn't work: an Android phone only
starts looking for receivers after it sees a Bluetooth LE beacon on a
specific service UUID. Until I added that, testers kept reporting "my
phone can't see my laptop" — and they were right.

**Packaging was hardest.** Strict snap confinement blocks a process from
owning a D-Bus name that doesn't match its snap name. My app registered
a reverse-DNS ID, so it exited silently at startup with no error a user
would ever see. I shipped that bug twice — once in the main window, once
in the file picker — because "it works on my machine" and "it works
installed" are different claims.

**The one that mattered most:** a security pass found that a filename
sent by a peer was used unchanged to build the destination path. An
absolute path like `/etc/cron.d/evil` discards the download directory
entirely. Any device you accepted a file from could write anywhere on
your disk. It's fixed, with regression tests — but it's a good reminder
that any input from another device is attacker-controlled until you
prove otherwise.

Free and MIT licensed:

    sudo snap install nearshare

github.com/Dhiva-Labs/NearShare

Built on protocol documentation from the NearDrop and rquickshare
projects.

#Linux #OpenSource #SoftwareEngineering #Security #Android

---

## Draft C — short

Linux couldn't use Quick Share. Now it can.

**NearShare** — send files between Linux, Android, Windows and macOS.
No cable, no account, no companion app. End-to-end encrypted, with a PIN
you confirm on both screens.

    sudo snap install nearshare

Open source (MIT): github.com/Dhiva-Labs/NearShare

#Linux #OpenSource #Android

---

## Notes before posting

* **A screenshot or a short screen recording will roughly double
  engagement.** The strongest one: your phone's share sheet with the
  laptop listed, next to the laptop showing the incoming file. A GIF of
  a file moving phone → laptop is better still.
* **Credit is not optional here.** NearDrop (grishka) and rquickshare
  (Martichou) published the protocol work this is built on. Draft A and
  B both credit them; keep that in whatever you post.
* **Don't call it "Google Quick Share for Linux".** Quick Share and
  Nearby Share are trademarks of Google and Samsung. Describing it as an
  independent client that *works with* Quick Share is accurate and is
  the reason the project is called NearShare rather than what it was
  originally named.
* Post mid-morning on a weekday for a technical audience; put the link
  in the post rather than the first comment — LinkedIn no longer
  penalises this meaningfully, and it reduces friction.
