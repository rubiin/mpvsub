-- mpvsub.lua — CTRL+g (default) opens the subtitle downloader popup for
-- the current file: sets up the IPC socket, waits for it, then launches
-- `main.py --socket <sock> --file <file>` detached. The app talks back
-- over the socket to auto-load subtitles (sub-add) and follow media.
--
-- Config: script-opts/mpvsub.conf (see mpvsub.conf in this repo):
--   key=CTRL+g            open the popup
--   python=python3        interpreter (use your venv python if needed)
--   app=                  absolute path to main.py (default: next to this script)
--   extra_args=           extra CLI flags passed to main.py (e.g. --debug)

local mp = require 'mp'
local utils = require 'mp.utils'
local options = require 'mp.options'

o = {
    key = "CTRL+g",
    python = "python3",
    app = "",
    extra_args = "",
}
options.read_options(o, "mpvsub")

local function script_dir()
    if mp.script_dir and mp.script_dir ~= "" then return mp.script_dir end
    local info = debug.getinfo(1, "S")
    local src = (info and info.source) or ""
    if src:sub(1, 1) == "@" then src = src:sub(2) end
    return src:match("^(.*)[/\\][^/\\]+$")
end

local function app_path()
    if o.app and o.app ~= "" then return o.app end
    local dir = script_dir()
    if dir then
        local p = utils.join_path(dir, "main.py")
        local f = io.open(p, "r")
        if f then f:close() return p end
    end
    return "main.py"
end

-- Reuse --input-ipc-server if set, else create a per-instance socket
-- under the runtime dir.
local function ensure_socket()
    local sock = mp.get_property("input-ipc-server")
    if not sock or sock == "" then
        local base = os.getenv("XDG_RUNTIME_DIR")
        if not base or base == "" then base = "/tmp" end
        local pid = mp.get_property_number("pid") or 0
        sock = utils.join_path(base, "mpvsub-" .. pid .. ".sock")
        mp.set_property("input-ipc-server", sock)
    end
    return sock
end

-- Probe with stat(), not io.open(): opening a unix socket fails/blocks.
local function wait_for_socket(path, cb, tries)
    tries = tries or 0
    if tries > 50 then cb(false) return end
    if utils.file_info(path) then
        cb(true)
        return
    end
    mp.add_timeout(0.05, function() wait_for_socket(path, cb, tries + 1) end)
end

local function start()
    local path = mp.get_property("path") or ""
    local sock = ensure_socket()
    wait_for_socket(sock, function(ok)
        if not ok then
            mp.osd_message("mpvsub: could not start IPC socket", 3)
            return
        end
        local args = { o.python, app_path(), "--socket", sock }
        if path and path ~= "" then
            args[#args + 1] = "--file"
            args[#args + 1] = path
        end
        for a in (o.extra_args .. " "):gmatch("%S+") do
            args[#args + 1] = a
        end
        mp.command_native_async({
            name = "subprocess",
            args = args,
            detach = true,
            capture_stdout = false,
            capture_stderr = false,
            playback_only = false,
        }, function() end)
    end)
end

mp.add_key_binding(o.key, "mpvsub-open", start)
