"""Small dependency-free Keccak-256 implementation.

Ethereum uses Keccak-256 (the pre-standardization padding), which is different
from NIST SHA3-256.  Keeping this implementation local makes the offline
registry tests independent of an RPC SDK or a native crypto package.
"""
from __future__ import annotations

_MASK = (1 << 64) - 1
_RATE = 136
_ROUND_CONSTANTS = (
    0x0000000000000001, 0x0000000000008082,
    0x800000000000808A, 0x8000000080008000,
    0x000000000000808B, 0x0000000080000001,
    0x8000000080008081, 0x8000000000008009,
    0x000000000000008A, 0x0000000000000088,
    0x0000000080008009, 0x000000008000000A,
    0x000000008000808B, 0x800000000000008B,
    0x8000000000008089, 0x8000000000008003,
    0x8000000000008002, 0x8000000000000080,
    0x000000000000800A, 0x800000008000000A,
    0x8000000080008081, 0x8000000000008080,
    0x0000000080000001, 0x8000000080008008,
)
_ROTATION = (
    (0, 36, 3, 41, 18),
    (1, 44, 10, 45, 2),
    (62, 6, 43, 15, 61),
    (28, 55, 25, 21, 56),
    (27, 20, 39, 8, 14),
)


def _rotl(value: int, amount: int) -> int:
    if amount == 0:
        return value & _MASK
    return ((value << amount) | (value >> (64 - amount))) & _MASK


def _keccak_f(state: list[int]) -> None:
    for rc in _ROUND_CONSTANTS:
        columns = [state[x] ^ state[x + 5] ^ state[x + 10] ^ state[x + 15] ^ state[x + 20] for x in range(5)]
        correction = [columns[(x - 1) % 5] ^ _rotl(columns[(x + 1) % 5], 1) for x in range(5)]
        for y in range(5):
            for x in range(5):
                state[x + 5 * y] ^= correction[x]

        mixed = [0] * 25
        for y in range(5):
            for x in range(5):
                mixed[y + 5 * ((2 * x + 3 * y) % 5)] = _rotl(state[x + 5 * y], _ROTATION[x][y])
        for y in range(5):
            for x in range(5):
                state[x + 5 * y] = mixed[x + 5 * y] ^ ((~mixed[(x + 1) % 5 + 5 * y]) & mixed[(x + 2) % 5 + 5 * y])
        state[0] ^= rc


def keccak256(data: bytes | bytearray | memoryview) -> bytes:
    """Return Ethereum Keccak-256 for *data*."""
    raw = bytes(data)
    padded = bytearray(raw)
    padded.append(0x01)
    while len(padded) % _RATE != _RATE - 1:
        padded.append(0)
    padded.append(0x80)

    state = [0] * 25
    for offset in range(0, len(padded), _RATE):
        block = padded[offset : offset + _RATE]
        for lane in range(_RATE // 8):
            state[lane] ^= int.from_bytes(block[lane * 8 : lane * 8 + 8], "little")
        _keccak_f(state)
    return b"".join(lane.to_bytes(8, "little") for lane in state)[:32]


__all__ = ["keccak256"]
