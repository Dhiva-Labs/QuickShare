"""A tiny, dependency-free QR Code encoder plus a GTK4 widget to draw it.

We only need to encode short ``WIFI:...`` strings (see hotspot.py) for the
"Direct mode" pairing flow, so this is deliberately not a general-purpose
QR library: it implements byte mode only, error-correction level L only,
and versions 1-6 (up to 136 data bytes), which is enough headroom for any
SSID/password we generate ourselves. Longer input raises ValueError with
a clear message rather than silently reaching into version-7+ territory,
which would additionally require version-information blocks we do not
implement.

Implementation follows ISO/IEC 18004 (the QR Code spec): Reed-Solomon
error correction over GF(256), the standard finder/timing/alignment
function patterns, the 8 documented data-masking formulas scored by the
4 penalty rules, and BCH(15,5) format-information encoding. No qrcode/
pillow/etc. dependency is introduced; only the stdlib and PyGObject
(already required by the rest of the UI) are used.
"""

from __future__ import annotations

import gi

gi.require_version("Gtk", "4.0")
from gi.repository import Gtk  # noqa: E402  (gi.require_version must run first)

# ---------------------------------------------------------------- GF(256)
#
# QR's Reed-Solomon codes work over GF(256) with the primitive polynomial
# x^8 + x^4 + x^3 + x^2 + 1 (0x11D) and generator element 2. We precompute
# exp/log tables once so multiplication is a table lookup.

_EXP = [0] * 512
_LOG = [0] * 256


def _init_gf() -> None:
    x = 1
    for i in range(255):
        _EXP[i] = x
        _LOG[x] = i
        x <<= 1
        if x & 0x100:
            x ^= 0x11D
    for i in range(255, 512):
        _EXP[i] = _EXP[i - 255]


_init_gf()


def _gf_mul(a: int, b: int) -> int:
    if a == 0 or b == 0:
        return 0
    return _EXP[_LOG[a] + _LOG[b]]


def _poly_mul(a: list[int], b: list[int]) -> list[int]:
    """Multiply two GF(256) polynomials (coefficients, highest degree first)."""
    result = [0] * (len(a) + len(b) - 1)
    for i, ca in enumerate(a):
        if ca == 0:
            continue
        for j, cb in enumerate(b):
            result[i + j] ^= _gf_mul(ca, cb)
    return result


def _rs_generator_poly(ecc_len: int) -> list[int]:
    """Generator polynomial with roots alpha^0..alpha^(ecc_len-1)."""
    g = [1]
    for i in range(ecc_len):
        g = _poly_mul(g, [1, _EXP[i]])
    return g


def _rs_encode(data: list[int], ecc_len: int) -> list[int]:
    """Reed-Solomon ECC codewords for one data block (synthetic division)."""
    generator = _rs_generator_poly(ecc_len)
    remainder = list(data) + [0] * ecc_len
    for i in range(len(data)):
        coef = remainder[i]
        if coef == 0:
            continue
        for j, gcoef in enumerate(generator):
            remainder[i + j] ^= _gf_mul(gcoef, coef)
    return remainder[len(data):]


# ---------------------------------------------------------- version tables
#
# (data_codewords, ecc_codewords_per_block, num_blocks) for EC level L,
# versions 1-6. Blocks are always equal-sized in this range (the uneven
# block-group split only starts appearing at higher versions we don't
# support), which keeps interleaving trivial.

_VERSION_INFO = {
    1: (19, 7, 1),
    2: (34, 10, 1),
    3: (55, 15, 1),
    4: (80, 20, 1),
    5: (108, 26, 1),
    6: (136, 18, 2),
}
# Alignment-pattern center coordinate (versions 1-6 have at most one
# alignment pattern, away from the finder corners).
_ALIGNMENT_CENTER = {2: 18, 3: 22, 4: 26, 5: 30, 6: 34}
# Bits appended after the last full byte of interleaved codewords so the
# bitstream fills the matrix exactly (0 for version 1).
_REMAINDER_BITS = {1: 0, 2: 7, 3: 7, 4: 7, 5: 7, 6: 7}

_FORMAT_MASK = 0b101010000010010
_FORMAT_GENERATOR = 0b10100110111  # degree-10 BCH generator, 0x537
_EC_LEVEL_L_BITS = 0b01

_FINDER = (
    (1, 1, 1, 1, 1, 1, 1),
    (1, 0, 0, 0, 0, 0, 1),
    (1, 0, 1, 1, 1, 0, 1),
    (1, 0, 1, 1, 1, 0, 1),
    (1, 0, 1, 1, 1, 0, 1),
    (1, 0, 0, 0, 0, 0, 1),
    (1, 1, 1, 1, 1, 1, 1),
)
_ALIGNMENT = (
    (1, 1, 1, 1, 1),
    (1, 0, 0, 0, 1),
    (1, 0, 1, 0, 1),
    (1, 0, 0, 0, 1),
    (1, 1, 1, 1, 1),
)

_MASK_FUNCS = [
    lambda r, c: (r + c) % 2 == 0,
    lambda r, c: r % 2 == 0,
    lambda r, c: c % 3 == 0,
    lambda r, c: (r + c) % 3 == 0,
    lambda r, c: (r // 2 + c // 3) % 2 == 0,
    lambda r, c: (r * c) % 2 + (r * c) % 3 == 0,
    lambda r, c: ((r * c) % 2 + (r * c) % 3) % 2 == 0,
    lambda r, c: ((r + c) % 2 + (r * c) % 3) % 2 == 0,
]


def _choose_version(data_len: int) -> int:
    """Smallest version (1-6) whose EC-L capacity fits ``data_len`` bytes
    of byte-mode payload (4-bit mode + 8-bit count + 8 bits/byte)."""
    payload_bits = 4 + 8 + 8 * data_len
    for version, (data_codewords, _, _) in _VERSION_INFO.items():
        if data_codewords * 8 >= payload_bits:
            return version
    max_bytes = (_VERSION_INFO[6][0] * 8 - 12) // 8
    raise ValueError(
        f"text too long for this QR encoder ({data_len} bytes, max "
        f"~{max_bytes}); this encoder only supports versions 1-6")


def _bitstream_to_codewords(data: bytes, version: int) -> list[int]:
    data_codewords = _VERSION_INFO[version][0]
    bits: list[int] = []

    def push(value: int, length: int) -> None:
        for i in range(length - 1, -1, -1):
            bits.append((value >> i) & 1)

    push(0b0100, 4)  # byte mode
    push(len(data), 8)  # count indicator (8 bits: valid for versions 1-9)
    for b in data:
        push(b, 8)

    capacity_bits = data_codewords * 8
    bits.extend([0] * min(4, capacity_bits - len(bits)))  # terminator
    while len(bits) % 8 != 0:
        bits.append(0)

    pad_bytes = (0xEC, 0x11)
    i = 0
    while len(bits) // 8 < data_codewords:
        push(pad_bytes[i % 2], 8)
        i += 1

    return [int("".join(map(str, bits[i:i + 8])), 2)
            for i in range(0, len(bits), 8)]


def _interleave(data_codewords: list[int], version: int) -> list[int]:
    dc_total, ecc_len, num_blocks = _VERSION_INFO[version]
    block_size = dc_total // num_blocks
    blocks = [data_codewords[i * block_size:(i + 1) * block_size]
              for i in range(num_blocks)]
    ecc_blocks = [_rs_encode(block, ecc_len) for block in blocks]

    out: list[int] = []
    for i in range(block_size):
        for block in blocks:
            out.append(block[i])
    for i in range(ecc_len):
        for ecc in ecc_blocks:
            out.append(ecc[i])
    return out


def _codewords_to_bits(codewords: list[int], version: int) -> list[int]:
    bits: list[int] = []
    for cw in codewords:
        bits.extend((cw >> i) & 1 for i in range(7, -1, -1))
    bits.extend([0] * _REMAINDER_BITS[version])
    return bits


def _format_bits(mask: int) -> list[int]:
    data = (_EC_LEVEL_L_BITS << 3) | mask
    rem = data << 10
    for i in range(4, -1, -1):
        if rem & (1 << (10 + i)):
            rem ^= _FORMAT_GENERATOR << i
    combined = (data << 10) | rem
    combined ^= _FORMAT_MASK
    return [(combined >> i) & 1 for i in range(14, -1, -1)]


class _Matrix:
    """Module grid plus a parallel mask of which cells are function
    patterns (finder/timing/alignment/format) that data placement and
    masking must not touch."""

    def __init__(self, version: int) -> None:
        self.version = version
        self.size = version * 4 + 17
        self.dark = [[False] * self.size for _ in range(self.size)]
        self.is_function = [[False] * self.size for _ in range(self.size)]

    def set(self, r: int, c: int, value: bool, function: bool = True) -> None:
        self.dark[r][c] = value
        if function:
            self.is_function[r][c] = True

    def _place_finder(self, top: int, left: int) -> None:
        for dr in range(-1, 8):
            for dc in range(-1, 8):
                r, c = top + dr, left + dc
                if not (0 <= r < self.size and 0 <= c < self.size):
                    continue
                if 0 <= dr < 7 and 0 <= dc < 7:
                    self.set(r, c, bool(_FINDER[dr][dc]))
                else:  # separator ring, always light
                    self.set(r, c, False)

    def draw_function_patterns(self) -> None:
        self._place_finder(0, 0)
        self._place_finder(0, self.size - 7)
        self._place_finder(self.size - 7, 0)

        for i in range(8, self.size - 8):
            dark = i % 2 == 0
            self.set(6, i, dark)
            self.set(i, 6, dark)

        self.set(4 * self.version + 9, 8, True)  # dark module

        if self.version >= 2:
            c = _ALIGNMENT_CENTER[self.version]
            for dr in range(-2, 3):
                for dc in range(-2, 3):
                    self.set(c + dr, c + dc,
                             bool(_ALIGNMENT[dr + 2][dc + 2]))

        # Reserve (but don't yet fill) the format-info strips; placeholder
        # False so they're excluded from data placement, overwritten below
        # once the mask is chosen.
        for i in range(9):
            if i != 6:
                self.set(8, i, False)
                self.set(i, 8, False)
        for i in range(self.size - 8, self.size):
            self.set(8, i, False)
            self.set(i, 8, False)

    def draw_format_bits(self, mask: int) -> None:
        bits = _format_bits(mask)
        for i in range(6):
            self.set(8, i, bool(bits[i]))
        self.set(8, 7, bool(bits[6]))
        self.set(8, 8, bool(bits[7]))
        self.set(7, 8, bool(bits[8]))
        for i in range(9, 15):
            self.set(14 - i, 8, bool(bits[i]))
        for i in range(8):
            self.set(self.size - 1 - i, 8, bool(bits[i]))
        for i in range(8, 15):
            self.set(8, self.size - 15 + i, bool(bits[i]))

    def place_data(self, bits: list[int], mask_id: int) -> None:
        mask_func = _MASK_FUNCS[mask_id]
        bit_iter = iter(bits)
        col = self.size - 1
        upward = True
        while col >= 1:
            if col == 6:  # never place data in the timing column
                col -= 1
            rows = range(self.size - 1, -1, -1) if upward else range(self.size)
            for row in rows:
                for c in (col, col - 1):
                    if self.is_function[row][c]:
                        continue
                    try:
                        bit = next(bit_iter)
                    except StopIteration:
                        bit = 0
                    value = bool(bit)
                    if mask_func(row, c):
                        value = not value
                    self.dark[row][c] = value
            upward = not upward
            col -= 2

    # ------------------------------------------------------------ scoring

    def penalty(self) -> int:
        total = 0
        size = self.size
        # Rule 1: runs of >=5 same-colour modules in a row/column.
        for rows in (self.dark, list(zip(*self.dark))):
            for line in rows:
                run = 1
                for i in range(1, size):
                    if line[i] == line[i - 1]:
                        run += 1
                    else:
                        if run >= 5:
                            total += 3 + (run - 5)
                        run = 1
                if run >= 5:
                    total += 3 + (run - 5)
        # Rule 2: 2x2 blocks of the same colour.
        for r in range(size - 1):
            for c in range(size - 1):
                v = self.dark[r][c]
                if (self.dark[r][c + 1] == v and self.dark[r + 1][c] == v
                        and self.dark[r + 1][c + 1] == v):
                    total += 3
        # Rule 3: finder-like 1:1:3:1:1 pattern with 4-module light run.
        pattern = (True, False, True, True, True, False, True,
                   False, False, False, False)
        for r in range(size):
            for c in range(size - len(pattern) + 1):
                if tuple(self.dark[r][c:c + len(pattern)]) == pattern:
                    total += 40
        for c in range(size):
            for r in range(size - len(pattern) + 1):
                if tuple(self.dark[r + i][c] for i in range(len(pattern))) == pattern:
                    total += 40
        # Rule 4: overall dark/light balance vs 50%.
        dark_count = sum(sum(row) for row in self.dark)
        percent = dark_count * 100 // (size * size)
        total += (abs(percent - 50) // 5) * 10
        return total


def make_qr_matrix(text: str) -> list[list[bool]]:
    """Build a QR code (byte mode, EC level L) for ``text``.

    Returns a square list-of-lists of booleans (True = dark module),
    with no quiet zone included (add >=4 light modules on each side when
    rendering). Raises ValueError if ``text`` doesn't fit in version 6.
    """
    data = text.encode("utf-8")
    version = _choose_version(len(data))
    data_codewords = _bitstream_to_codewords(data, version)
    interleaved = _interleave(data_codewords, version)
    bits = _codewords_to_bits(interleaved, version)

    best_matrix: _Matrix | None = None
    best_penalty = None
    for mask_id in range(8):
        m = _Matrix(version)
        m.draw_function_patterns()
        m.place_data(bits, mask_id)
        m.draw_format_bits(mask_id)
        score = m.penalty()
        if best_penalty is None or score < best_penalty:
            best_penalty, best_matrix = score, m
    assert best_matrix is not None
    return best_matrix.dark


def wifi_qr_payload(ssid: str, password: str) -> str:
    """WIFI: URI format phones' camera apps recognise for auto-join."""

    def esc(s: str) -> str:
        out = []
        for ch in s:
            if ch in '\\;,:"':
                out.append("\\")
            out.append(ch)
        return "".join(out)

    return f"WIFI:T:WPA;S:{esc(ssid)};P:{esc(password)};;"


class QrCodeArea(Gtk.DrawingArea):
    """A square GTK widget that renders a QR code for arbitrary text."""

    def __init__(self, text: str = "", **kwargs) -> None:
        super().__init__(**kwargs)
        self._matrix: list[list[bool]] | None = None
        self.set_content_width(220)
        self.set_content_height(220)
        self.set_draw_func(self._draw)
        if text:
            self.set_text(text)

    def set_text(self, text: str) -> None:
        try:
            self._matrix = make_qr_matrix(text) if text else None
        except ValueError:
            self._matrix = None
        self.queue_draw()

    def _draw(self, area: Gtk.DrawingArea, cr, width: int, height: int) -> None:
        style = self.get_style_context()
        light = style.get_color()
        # Background: use the widget's foreground colour at low opacity
        # so the quiet zone matches light/dark theme without a hardcoded
        # white square that would clash with dark mode.
        cr.set_source_rgba(light.red, light.green, light.blue, 0.05)
        cr.rectangle(0, 0, width, height)
        cr.fill()

        if not self._matrix:
            return
        size = len(self._matrix)
        quiet = 4
        modules = size + quiet * 2
        scale = min(width, height) / modules
        ox = (width - modules * scale) / 2
        oy = (height - modules * scale) / 2

        cr.set_source_rgba(1, 1, 1, 1)
        cr.rectangle(ox, oy, modules * scale, modules * scale)
        cr.fill()

        cr.set_source_rgba(0, 0, 0, 1)
        for r, row in enumerate(self._matrix):
            for c, dark in enumerate(row):
                if dark:
                    cr.rectangle(ox + (quiet + c) * scale,
                                oy + (quiet + r) * scale, scale, scale)
        cr.fill()
