"""AES-256 in plain Python, ECB, one block at a time.

This exists for exactly one job: checking whether a candidate key decrypts the
first block of a pak index into something that looks like a mount point. That is
two blocks per candidate, so speed is irrelevant and a dependency would be the
wrong trade - the build should not need a crypto wheel for a 32-byte sanity check.

Standard FIPS-197 implementation; verified against the published test vector in
the selftest.
"""
from __future__ import annotations

# ---------------------------------------------------------------- tables

def _build_tables() -> tuple[list[int], list[int]]:
    sbox = [0] * 256
    p = q = 1
    while True:
        # multiply p by 3
        p = p ^ ((p << 1) & 0xFF) ^ (0x1B if p & 0x80 else 0)
        # divide q by 3
        q ^= q << 1
        q ^= q << 2
        q ^= q << 4
        q &= 0xFF
        if q & 0x80:
            q ^= 0x09
        x = q ^ (q << 1) ^ (q << 2) ^ (q << 3) ^ (q << 4)
        x = (x ^ (x >> 8) ^ 0x63) & 0xFF
        sbox[p] = x
        if p == 1:
            break
    sbox[0] = 0x63
    inv = [0] * 256
    for i, v in enumerate(sbox):
        inv[v] = i
    return sbox, inv


SBOX, INV_SBOX = _build_tables()


def _xtime(a: int) -> int:
    a <<= 1
    return (a ^ 0x1B) & 0xFF if a & 0x100 else a


def _mul(a: int, b: int) -> int:
    out = 0
    while b:
        if b & 1:
            out ^= a
        a = _xtime(a)
        b >>= 1
    return out


# multiplication tables: the inverse round is the hot path when checking keys
M2 = [_mul(i, 2) for i in range(256)]
M3 = [_mul(i, 3) for i in range(256)]
M9 = [_mul(i, 9) for i in range(256)]
M11 = [_mul(i, 11) for i in range(256)]
M13 = [_mul(i, 13) for i in range(256)]
M14 = [_mul(i, 14) for i in range(256)]


# ---------------------------------------------------------------- key schedule

def expand_key(key: bytes) -> list[list[int]]:
    """Round keys for AES-256 (15 x 16 bytes)."""
    if len(key) != 32:
        raise ValueError("AES-256 needs a 32-byte key")
    nk, rounds = 8, 14
    words = [list(key[i:i + 4]) for i in range(0, 32, 4)]
    rcon = 1
    for i in range(nk, 4 * (rounds + 1)):
        temp = list(words[i - 1])
        if i % nk == 0:
            temp = temp[1:] + temp[:1]
            temp = [SBOX[b] for b in temp]
            temp[0] ^= rcon
            rcon = _xtime(rcon)
        elif i % nk == 4:
            temp = [SBOX[b] for b in temp]
        words.append([words[i - nk][j] ^ temp[j] for j in range(4)])
    return [sum(words[4 * r:4 * r + 4], []) for r in range(rounds + 1)]


# ---------------------------------------------------------------- block ops

def _add_round_key(state: list[int], rk: list[int]) -> list[int]:
    return [s ^ k for s, k in zip(state, rk)]


def _shift_rows(s: list[int]) -> list[int]:
    # state is column-major: index = col*4 + row
    out = list(s)
    for row in range(1, 4):
        for col in range(4):
            out[col * 4 + row] = s[((col + row) % 4) * 4 + row]
    return out


def _inv_shift_rows(s: list[int]) -> list[int]:
    out = list(s)
    for row in range(1, 4):
        for col in range(4):
            out[((col + row) % 4) * 4 + row] = s[col * 4 + row]
    return out


def _mix_columns(s: list[int]) -> list[int]:
    out = list(s)
    for c in range(4):
        a0, a1, a2, a3 = s[c * 4:c * 4 + 4]
        out[c * 4 + 0] = M2[a0] ^ M3[a1] ^ a2 ^ a3
        out[c * 4 + 1] = a0 ^ M2[a1] ^ M3[a2] ^ a3
        out[c * 4 + 2] = a0 ^ a1 ^ M2[a2] ^ M3[a3]
        out[c * 4 + 3] = M3[a0] ^ a1 ^ a2 ^ M2[a3]
    return out


def _inv_mix_columns(s: list[int]) -> list[int]:
    out = list(s)
    for c in range(4):
        a0, a1, a2, a3 = s[c * 4:c * 4 + 4]
        out[c * 4 + 0] = M14[a0] ^ M11[a1] ^ M13[a2] ^ M9[a3]
        out[c * 4 + 1] = M9[a0] ^ M14[a1] ^ M11[a2] ^ M13[a3]
        out[c * 4 + 2] = M13[a0] ^ M9[a1] ^ M14[a2] ^ M11[a3]
        out[c * 4 + 3] = M11[a0] ^ M13[a1] ^ M9[a2] ^ M14[a3]
    return out


def encrypt_block(block: bytes, round_keys: list[list[int]]) -> bytes:
    if len(block) != 16:
        raise ValueError("block must be 16 bytes")
    rounds = len(round_keys) - 1
    s = _add_round_key(list(block), round_keys[0])
    for r in range(1, rounds):
        s = [SBOX[b] for b in s]
        s = _shift_rows(s)
        s = _mix_columns(s)
        s = _add_round_key(s, round_keys[r])
    s = [SBOX[b] for b in s]
    s = _shift_rows(s)
    s = _add_round_key(s, round_keys[rounds])
    return bytes(s)


def decrypt_block(block: bytes, round_keys: list[list[int]]) -> bytes:
    if len(block) != 16:
        raise ValueError("block must be 16 bytes")
    rounds = len(round_keys) - 1
    s = _add_round_key(list(block), round_keys[rounds])
    for r in range(rounds - 1, 0, -1):
        s = _inv_shift_rows(s)
        s = [INV_SBOX[b] for b in s]
        s = _add_round_key(s, round_keys[r])
        s = _inv_mix_columns(s)
    s = _inv_shift_rows(s)
    s = [INV_SBOX[b] for b in s]
    s = _add_round_key(s, round_keys[0])
    return bytes(s)


def ecb_decrypt(data: bytes, key: bytes) -> bytes:
    rk = expand_key(key)
    return b"".join(decrypt_block(data[i:i + 16], rk)
                    for i in range(0, len(data) - len(data) % 16, 16))


def ecb_encrypt(data: bytes, key: bytes) -> bytes:
    rk = expand_key(key)
    return b"".join(encrypt_block(data[i:i + 16], rk)
                    for i in range(0, len(data) - len(data) % 16, 16))
