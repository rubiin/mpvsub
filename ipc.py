"""mpv JSON IPC client over its ``--input-ipc-server`` unix socket.

Runs on the asyncio loop thread; the window wraps its callbacks in
``GLib.idle_add`` before touching GTK. Supports property observation
(live media tracking) and ``sub-add`` for downloaded subtitles.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Callable, Optional

log = logging.getLogger(__name__)

PropertyCallback = Callable[[str, Any], None]
Callback = Callable[[], None]


class MpvError(Exception):
    """Raised for protocol / connectivity errors."""


class MpvClient:
    """Reconnecting JSON-RPC-style client for mpv."""

    def __init__(self, socket_path: Optional[str] = None) -> None:
        self.socket_path = socket_path
        self.connected = False

        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._reader: Optional[asyncio.StreamReader] = None
        self._writer: Optional[asyncio.StreamWriter] = None
        self._task: Optional[asyncio.Task[None]] = None
        self._req_id = 0
        self._pending: dict[int, asyncio.Future[Any]] = {}
        self._observers: dict[int, tuple[str, PropertyCallback]] = {}
        self._next_obs = 1000

        #: called (on the loop thread) when a connection is established
        self.on_connect: Optional[Callback] = None
        #: called (on the loop thread) when the connection drops
        self.on_disconnect: Optional[Callback] = None

    # -- lifecycle ----------------------------------------------------------

    def start(self, loop: asyncio.AbstractEventLoop) -> None:
        """Start the reconnect loop on *loop* (thread-safe)."""
        if self._task is not None and not self._task.done():
            return
        self._loop = loop
        # create_task is not thread-safe: schedule it on the loop thread
        loop.call_soon_threadsafe(self._spawn_task)

    def _spawn_task(self) -> None:
        if self._task is None or self._task.done():
            self._task = self._loop.create_task(self._run())  # type: ignore[union-attr]

    def set_socket(self, path: Optional[str]) -> None:
        """Point the client at a new socket and reconnect."""
        self.socket_path = path
        if self._loop is not None:
            self._loop.call_soon_threadsafe(self._stop_task)
        self._on_disconnect()

    def _stop_task(self) -> None:
        if self._task is not None and not self._task.done():
            self._task.cancel()
        self._task = None
        if self._writer is not None:
            try:
                self._writer.close()
            except Exception:
                pass
            self._writer = None

    async def close(self) -> None:
        if self._task is not None and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
        if self._writer is not None:
            try:
                self._writer.close()
            except Exception:
                pass

    # -- connection internals -----------------------------------------------

    async def _run(self) -> None:
        while True:
            try:
                if self.socket_path:
                    await self._connect()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - retry forever
                log.debug("mpv connect attempt failed: %s", exc)
            await asyncio.sleep(2.0)

    async def _connect(self) -> None:
        assert self.socket_path
        log.info("connecting to mpv at %s", self.socket_path)
        reader, writer = await asyncio.open_unix_connection(self.socket_path)
        self._reader, self._writer = reader, writer
        self.connected = True
        # re-register observers on (re)connect
        for obs_id, (name, _cb) in self._observers.items():
            await self._send(
                {"command": ["observe_property", obs_id, name], "request_id": None}
            )
        log.info("connected to mpv")
        if self.on_connect:
            self._safe_call(self.on_connect)
        try:
            await self._read_loop()
        finally:
            self._on_disconnect()

    def _on_disconnect(self) -> None:
        was_connected = self.connected
        self.connected = False
        self._reader = self._writer = None
        for fut in self._pending.values():
            if not fut.done():
                fut.set_exception(MpvError("mpv disconnected"))
        self._pending.clear()
        if was_connected:
            log.info("disconnected from mpv")
            if self.on_disconnect:
                self._safe_call(self.on_disconnect)

    def _safe_call(self, cb: Callback) -> None:
        try:
            cb()
        except Exception:  # noqa: BLE001
            log.exception("callback error")

    async def _send(self, payload: dict) -> None:
        assert self._writer is not None
        self._writer.write(json.dumps(payload).encode("utf-8") + b"\n")
        await self._writer.drain()

    async def _read_loop(self) -> None:
        assert self._reader is not None
        while True:
            line = await self._reader.readline()
            if not line:
                break
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                continue
            if "event" in msg:
                if msg.get("event") == "property-change":
                    entry = self._observers.get(msg.get("id"))
                    if entry:
                        name, cb = entry
                        try:
                            cb(name, msg.get("data"))
                        except Exception:  # noqa: BLE001
                            log.exception("property observer error")
            elif msg.get("request_id") is not None:
                fut = self._pending.pop(msg["request_id"], None)
                if fut is not None and not fut.done():
                    if msg.get("error") == "success":
                        fut.set_result(msg.get("data"))
                    else:
                        fut.set_exception(
                            MpvError(msg.get("error") or "unknown mpv error")
                        )

    # -- public API (coroutines run on the loop thread) ---------------------

    async def command(self, cmd: list, timeout: float = 10.0) -> Any:
        """Send a raw command and await its result data."""
        if self._loop is None or not self.connected:
            raise MpvError("not connected to mpv")
        self._req_id += 1
        rid = self._req_id
        fut: asyncio.Future[Any] = self._loop.create_future()
        self._pending[rid] = fut
        try:
            await self._send({"command": cmd, "request_id": rid})
            return await asyncio.wait_for(fut, timeout)
        finally:
            self._pending.pop(rid, None)

    async def get_property(self, name: str, default: Any = None) -> Any:
        try:
            return await self.command(["get_property", name])
        except MpvError:
            return default

    async def set_property(self, name: str, value: Any) -> Any:
        return await self.command(["set_property", name, value])

    async def sub_add(self, path: str) -> Optional[int]:
        """Add an external subtitle; return its track id when known.

        Careful: on modern mpv the second positional of ``sub-add`` is a
        *flags* string, so no title may be passed.
        """
        data = await self.command(["sub-add", path])
        try:
            return int(data)
        except (TypeError, ValueError):
            return None

    # -- property observation ----------------------------------------------

    def observe(self, name: str, cb: PropertyCallback) -> Optional[int]:
        """Subscribe to property changes (thread-safe). Returns observer id."""
        if self._loop is None:
            return None
        obs_id = self._next_obs
        self._next_obs += 1
        self._observers[obs_id] = (name, cb)
        if self.connected:
            asyncio.run_coroutine_threadsafe(
                self._send(
                    {"command": ["observe_property", obs_id, name], "request_id": None}
                ),
                self._loop,
            )
        return obs_id

    def unobserve(self, obs_id: int) -> None:
        self._observers.pop(obs_id, None)
        if self._loop is not None and self.connected:
            asyncio.run_coroutine_threadsafe(
                self._send(
                    {"command": ["unobserve_property", obs_id], "request_id": None}
                ),
                self._loop,
            )
