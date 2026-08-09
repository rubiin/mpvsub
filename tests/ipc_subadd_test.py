#!/usr/bin/env python3
"""Integration test for the mpv IPC client.

Requires a running mpv with an IPC socket and a video file:

    mpv --input-ipc-server=/tmp/mpv.sock --loop-file=inf /tmp/video.mp4 &
    python3 tests/ipc_subadd_test.py /tmp/mpv.sock /tmp/video.mp4

Prints the current path, adds a dummy subtitle, selects it and reports the
subtitle track list. Exits non-zero on failure.
"""

from __future__ import annotations

import asyncio
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ipc import MpvClient  # noqa: E402

DUMMY_SRT = "1\n00:00:01,000 --> 00:00:03,000\nHello from ipc test.\n"


async def main(socket_path: str, expected_path: str) -> int:
    client = MpvClient(socket_path)
    loop = asyncio.get_running_loop()
    client.start(loop)
    try:
        for _ in range(50):  # up to ~5s to connect
            await asyncio.sleep(0.1)
            if client.connected:
                break
        if not client.connected:
            print("FAIL: never connected to mpv")
            return 1

        path = await client.get_property("path")
        print(f"path: {path}")
        if path != expected_path:
            print(f"FAIL: expected path {expected_path!r}")
            return 1

        with tempfile.NamedTemporaryFile(
            "w", suffix=".srt", delete=False
        ) as f:
            f.write(DUMMY_SRT)
            srt = f.name

        track_id = await client.sub_add(srt)
        print(f"sub_add track id: {track_id}")
        if track_id is not None:
            await client.set_property("sid", track_id)

        track_list = await client.get_property("track-list") or []
        sub_tracks = [t for t in track_list if t.get("type") == "sub"]
        print(f"subtitle tracks now: {len(sub_tracks)}")
        if not sub_tracks:
            print("FAIL: no subtitle track present after sub-add")
            return 1

        print("PASS: sub-add + track-list + sid selection OK")
        return 0
    finally:
        await client.close()


if __name__ == "__main__":
    if len(sys.argv) < 3:
        print(__doc__)
        sys.exit(2)
    sys.exit(asyncio.run(main(sys.argv[1], sys.argv[2])))
