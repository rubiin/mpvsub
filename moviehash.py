"""OpenSubtitles movie hash (pure Python, stdlib only).

First 64 KiB of the file plus the last 64 KiB (if it's bigger than 128
KiB), summed with the file size as little-endian 64-bit chunks mod 2**64,
emitted as 16 lowercase hex digits. The same algorithm the VLSub extension
uses for its ``moviehash``/``moviebytesize`` search parameters.
"""

from __future__ import annotations

import os

_CHUNK_SIZE = 64 * 1024  # 64 KiB
_MASK64 = (1 << 64) - 1


def compute_movie_hash(path: str) -> tuple[str, int]:
    """Return ``(hash_hex, file_size)`` for the video at *path*."""
    size = os.path.getsize(path)
    with open(path, "rb") as fh:
        data = fh.read(_CHUNK_SIZE)
        if size > 2 * _CHUNK_SIZE:
            fh.seek(-_CHUNK_SIZE, os.SEEK_END)
            data += fh.read(_CHUNK_SIZE)

    total = size
    for i in range(0, len(data), 8):
        chunk = data[i : i + 8]
        if len(chunk) < 8:
            chunk = chunk.ljust(8, b"\x00")
        total = (total + int.from_bytes(chunk, "little")) & _MASK64
    return f"{total:016x}", size
