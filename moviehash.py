"""OpenSubtitles movie-hash computation (pure Python, stdlib only).

Implements the well-known OpenSubtitles hash used by the ``moviehash`` /
``moviebytesize`` search parameters of the OpenSubtitles REST API:

* read the first 64 KiB of the file,
* if the file is larger than 128 KiB, also read the last 64 KiB,
* interpret the file size plus every 8-byte little-endian chunk of that data
  as unsigned 64-bit integers and sum them modulo 2**64,
* emit the sum as a 16-digit lowercase hex string.

This is the same algorithm the reference VLSub extension computes in Lua and
that subliminal used to apply via its ``hash_refine`` refiner.
"""

from __future__ import annotations

import os

_CHUNK_SIZE = 64 * 1024  # 64 KiB
_MASK64 = (1 << 64) - 1


def compute_movie_hash(path: str) -> tuple[str, int]:
    """Return ``(hash_hex, file_size)`` for the video file at *path*.

    *path* must exist and be readable.  The hash is 16 lowercase hex
    characters; the size is the raw byte count (used as ``moviebytesize``).
    """
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
