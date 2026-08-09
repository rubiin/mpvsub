-- submenu.lua — a VLC-style subtitle downloader for mpv.
--
-- Provides a key binding (default CTRL+s) that opens a full OSD picker
-- listing downloadable subtitles for the current video: language, release
-- name, provider, format and download count. Pick one, press Enter, it is
-- downloaded next to the video and selected.
--
-- (Optional: expose it from your right-click context menu with a
-- `script-binding submenu/open` or `script-message submenu open` entry.)
--
-- Requirements:
--   * mpv >= 0.33 (async subprocess + OSD overlays)
--   * subliminal (pipx install subliminal) — see sub_helper.py
--   * network access to OpenSubtitles.com etc.
--
-- While the picker is open:
--   ↑/↓ or j/k   move selection        Enter/Space    download selected
--   PgUp/PgDn    page                   r              refresh search
--   g / G        first / last           l              cycle language
--   c            cancel search          Esc / q        close
--   mouse: hover to select, double-click to download, right-click to close,
--   wheel to scroll. (Mouse is only captured while the picker is open.)
--
-- Config: script-opts/submenu.conf (see submenu.conf in this folder).

local mp = require 'mp'
local msg = require 'mp.msg'
local utils = require 'mp.utils'
local options = require 'mp.options'

o = {
    key = "CTRL+s",            -- open the subtitle picker
    languages = "en",          -- comma separated IETF codes; 'l' cycles them
    providers = "opensubtitlescom,podnapisi,subtis,tvsubtitles",
    helper = "",               -- absolute path to sub_helper.py (empty = alongside this script)
    download_dir = "",         -- empty = same directory as the video
    encoding = "utf-8",
    debug_osd_file = "",       -- if set, dump the overlay ASS here on every render
    max_results = 100,
    panel_width = 0.64,        -- fraction of screen width
    panel_height = 0.74,       -- fraction of screen height
    accent = "E6A23C",         -- UI accent (RRGGBB)
    bg = "0F1115",             -- panel background (RRGGBB)
}
options.read_options(o, "submenu")

-- ---------------------------------------------------------------------------
-- utilities
-- ---------------------------------------------------------------------------

local function esc(s)
    s = tostring(s or "")
    s = s:gsub("\\", "\\\\")
    s = s:gsub("{", "\\{")
    s = s:gsub("}", "\\}")
    return s
end

local function rgb_to_ass(hex)
    local r, g, b = hex:sub(1, 2), hex:sub(3, 4), hex:sub(5, 6)
    return "&H" .. b .. g .. r
end

local function alpha(a) -- 0..255, 0 = opaque
    return string.format("&H%02X", a)
end

-- truncate a string to roughly maxpx at font size fs, preserving UTF-8
local function fit(s, maxpx, fs)
    s = tostring(s or "")
    if maxpx <= 0 then return "" end
    local width, last_end = 0, 0
    local i, n = 1, #s
    while i <= n do
        local b = s:byte(i)
        local len
        if b < 0x80 then len = 1
        elseif b < 0xE0 then len = 2
        elseif b < 0xF0 then len = 3
        else len = 4 end
        local cw
        if b >= 0xF0 or b >= 0xE0 then cw = fs          -- CJK / full width
        elseif b >= 0x80 then cw = fs * 0.9
        else cw = fs * 0.55 end                          -- latin
        if width + cw > maxpx then
            if last_end == 0 then return s:sub(1, i + len - 1) .. "…" end
            return s:sub(1, last_end) .. "…"
        end
        width = width + cw
        last_end = i + len - 1
        i = i + len
    end
    return s
end

-- ---------------------------------------------------------------------------
-- state
-- ---------------------------------------------------------------------------

local S = {
    mode = "closed",       -- "closed" | "picker"
    subs = {},
    sel = 1,
    top = 1,
    langs = {},
    lang = "en",
    loading = false,
    error = nil,           -- { title = .., hint = .. }
    notice = nil,          -- { text = .., until = clock }
    downloading = nil,     -- index currently downloading
    saved = nil,           -- { idx = .., name = .., file = .. }
    req_id = 0,
    handle = nil,          -- async subprocess handle
    frame = 0,
    spinner_timer = nil,
}

local W, H = 1920, 1080

mp.observe_property("osd-dimensions", "native", function(_, dim)
    if dim and dim.w and dim.h then
        W, H = dim.w, dim.h
        if S.mode ~= "closed" then render() end
    end
end)

local ov = mp.create_osd_overlay("ass-events")
ov.z = 1000

-- shared geometry; used by rendering, navigation and mouse input
local function layout()
    local pw = math.min(W * o.panel_width, W - W * 0.06)
    local ph = math.min(H * o.panel_height, H - H * 0.08)
    local x0, y0 = (W - pw) / 2, (H - ph) / 2
    local fs_title, fs_row, fs_foot = H * 0.030, H * 0.026, H * 0.021
    local row_h = fs_row * 2.15
    local header_h = fs_title * 3.0
    local footer_h = fs_foot * 2.8
    local list_top = y0 + header_h + fs_foot * 1.9
    local list_h = ph - header_h - footer_h - fs_foot * 1.9
    return {
        pw = pw, ph = ph, x0 = x0, y0 = y0,
        fs_title = fs_title, fs_row = fs_row, fs_foot = fs_foot,
        row_h = row_h, header_h = header_h, footer_h = footer_h,
        list_top = list_top,
        nrows = math.max(1, math.floor(list_h / row_h)),
    }
end

-- ---------------------------------------------------------------------------
-- rendering (ASS)
-- ---------------------------------------------------------------------------

local LANG_COLORS = {
    en = "4E9AF1", de = "F1C40F", fr = "58A6FF", es = "E8A33D",
    pt = "7BC96F", it = "E06C75", ja = "C792EA", zh = "56B6C2",
    ru = "61AFEF", pl = "D19A66", ko = "F07178", nl = "FFB86C",
    tr = "A5D6FF", sv = "8FBCBB", ar = "D4A06A", hi = "FF9E64",
}

local function lang_color(lang)
    return rgb_to_ass(LANG_COLORS[lang:lower():sub(1, 2)] or o.accent)
end

local PROVIDER_SHORT = {
    opensubtitlescom = "OSC", opensubtitles = "OS", podnapisi = "podn",
    subtis = "subtis", tvsubtitles = "tvsub", addic7ed = "a7ed",
    napiprojekt = "napi", subtitulamos = "subs", legendastv = "lgd",
    bsplayer = "bspl", gestdown = "gest",
}

local function provider_label(p) return PROVIDER_SHORT[p] or p end

local C = { text = "E6EDF3", dim = "8B949E", faint = "6E7681",
            bad = "E06C75", good = "7BC96F" }

local function build_ass()
    local L = layout()
    local parts = {}
    local function ev(tags, text)
        parts[#parts + 1] = "{\\" .. tags .. "}" .. text
    end

    local x0, y0, pw, ph = L.x0, L.y0, L.pw, L.ph
    local fs_title, fs_row, fs_foot = L.fs_title, L.fs_row, L.fs_foot
    local hx = x0 + pw * 0.03

    -- dim the video behind the panel
    ev("an7\\pos(0,0)\\p1\\bord0\\shad0\\c&H000000&\\1a" .. alpha(150),
       "m 0 0 l " .. W .. " 0 l " .. W .. " " .. H .. " l 0 " .. H .. " z")
    -- panel background
    ev("an7\\pos(" .. x0 .. "," .. y0 .. ")\\p1\\bord0\\shad0\\c" .. rgb_to_ass(o.bg)
        .. "\\1a" .. alpha(26),
       "m 0 0 l " .. pw .. " 0 l " .. pw .. " " .. ph .. " l 0 " .. ph .. " z")
    -- accent top bar
    ev("an7\\pos(" .. x0 .. "," .. y0 .. ")\\p1\\bord0\\shad0\\c" .. rgb_to_ass(o.accent)
        .. "\\1a&H00&",
       "m 0 0 l " .. pw .. " 0 l " .. pw .. " " .. H * 0.006 .. " l 0 " .. H * 0.006 .. " z")

    -- header
    ev("an7\\pos(" .. hx .. "," .. (y0 + H * 0.022) .. ")\\fs" .. fs_title
        .. "\\b1\\c" .. rgb_to_ass(C.text), esc("Subtitle downloader"))
    local path = mp.get_property("path") or ""
    local fname = path:match("[^/\\]+$") or path
    ev("an3\\pos(" .. (x0 + pw - pw * 0.03) .. "," .. (y0 + H * 0.026) .. ")\\fs" .. fs_row
        .. "\\b0\\c" .. rgb_to_ass(C.dim), esc(fit(fname, pw * 0.42, fs_row)))

    local body_cy = y0 + ph * 0.48

    if S.loading then
        local cx, cy = W / 2, body_cy
        local r = H * 0.020
        local ang = (S.frame % 60) / 60 * math.pi * 2
        local seg = math.pi * 1.7
        local p = {}
        for k = 0, 24 do
            local a = ang + seg * k / 24
            p[#p + 1] = string.format("%.1f %.1f", cx + math.cos(a) * r, cy + math.sin(a) * r)
        end
        ev("an7\\pos(0,0)\\p1\\bord0\\shad0\\c" .. rgb_to_ass(o.accent) .. "\\1a&H00&",
           "m " .. cx .. " " .. cy .. " " .. table.concat(p, " l ") .. " z")
        ev("an5\\pos(" .. cx .. "," .. (cy + r * 2.4) .. ")\\fs" .. fs_row .. "\\b0\\c" .. rgb_to_ass(C.text),
           esc("Searching subtitles…"))
        ev("an5\\pos(" .. cx .. "," .. (cy + r * 2.4 + fs_row * 1.6) .. ")\\fs" .. fs_foot .. "\\c" .. rgb_to_ass(C.dim),
           esc("providers: " .. o.providers .. "   ·   language: " .. S.lang .. "   ·   c to cancel"))
    elseif S.error then
        ev("an5\\pos(" .. (W / 2) .. "," .. body_cy .. ")\\fs" .. fs_title .. "\\b1\\c" .. rgb_to_ass(C.bad),
           esc(S.error.title or "Something went wrong"))
        if S.error.hint and S.error.hint ~= "" then
            ev("an5\\pos(" .. (W / 2) .. "," .. (body_cy + fs_title * 1.9) .. ")\\fs" .. fs_row
                .. "\\b0\\c" .. rgb_to_ass("9AA4AF"),
               esc(fit(S.error.hint, pw * 0.9, fs_row)))
        end
        ev("an5\\pos(" .. (W / 2) .. "," .. (body_cy + fs_title * 3.4) .. ")\\fs" .. fs_foot
            .. "\\c" .. rgb_to_ass(C.dim),
           esc("r refresh    ·    l language (" .. S.lang .. ")    ·    Esc close"))
    elseif #S.subs == 0 then
        ev("an5\\pos(" .. (W / 2) .. "," .. body_cy .. ")\\fs" .. fs_title .. "\\b1\\c" .. rgb_to_ass(C.text),
           esc("No subtitles found for “" .. S.lang .. "”"))
        ev("an5\\pos(" .. (W / 2) .. "," .. (body_cy + fs_title * 1.9) .. ")\\fs" .. fs_row
            .. "\\b0\\c" .. rgb_to_ass(C.dim),
           esc("Try another language (l), refresh (r) or check the provider list."))
    else
        -- results count line
        ev("an7\\pos(" .. hx .. "," .. (L.list_top - fs_foot * 1.6) .. ")\\fs" .. fs_foot
            .. "\\b0\\c" .. rgb_to_ass(C.dim),
           esc(string.format("%d subtitles · %s · %s",
               #S.subs, S.lang, o.providers:gsub(",", " · "))))

        for i = 1, L.nrows do
            local idx = S.top + i - 1
            if idx > #S.subs then break end
            local sub = S.subs[idx]
            local rowy = L.list_top + (i - 1) * L.row_h
            local row_h = L.row_h
            local selected = (idx == S.sel)

            if selected then
                ev("an7\\pos(" .. hx .. "," .. rowy .. ")\\p1\\bord0\\shad0\\c" .. rgb_to_ass(o.accent)
                    .. "\\1a" .. alpha(72),
                   "m 0 0 l " .. (pw * 0.94) .. " 0 l " .. (pw * 0.94) .. " " .. (row_h - H * 0.006)
                   .. " l 0 " .. (row_h - H * 0.006) .. " z")
                ev("an7\\pos(" .. hx .. "," .. rowy .. ")\\p1\\bord0\\shad0\\c" .. rgb_to_ass(o.accent)
                    .. "\\1a&H00&",
                   "m 0 0 l " .. H * 0.005 .. " 0 l " .. H * 0.005 .. " " .. (row_h - H * 0.006)
                   .. " l 0 " .. (row_h - H * 0.006) .. " z")
            end

            -- language badge
            local bx = hx + pw * 0.012
            local by = rowy + row_h * 0.22
            local bw, bh = pw * 0.05, row_h * 0.56
            local lc = lang_color(sub.language)
            ev("an7\\pos(" .. bx .. "," .. by .. ")\\p1\\bord0\\shad0\\c" .. lc .. "\\1a&H00&",
               "m 0 0 l " .. bw .. " 0 l " .. bw .. " " .. bh .. " l 0 " .. bh .. " z")
            ev("an5\\pos(" .. (bx + bw / 2) .. "," .. (by + bh / 2) .. ")\\fs" .. fs_foot
                .. "\\b1\\c&H000000&", esc(sub.language))

            -- title
            local tx = bx + bw + pw * 0.014
            local tw = x0 + pw - pw * 0.03 - tx
            local title = (sub.name and sub.name ~= "" and sub.name)
                or (sub.release_info and sub.release_info or "Subtitle")
            ev("an7\\pos(" .. tx .. "," .. (rowy + row_h * 0.1) .. ")\\fs" .. fs_row
                .. "\\b0\\c" .. rgb_to_ass(C.text), esc(fit(title, tw, fs_row)))

            -- meta (right side)
            local meta = provider_label(sub.provider) .. "  ·  " .. (sub.format or "?")
                .. "  ·  ⬇ " .. (sub.download_count or "?")
            if sub.hash_match then meta = "★ " .. meta end
            ev("an3\\pos(" .. (x0 + pw - pw * 0.03) .. "," .. (rowy + row_h * 0.16) .. ")\\fs" .. fs_foot
                .. "\\b0\\c" .. (sub.hash_match and rgb_to_ass(C.good) or rgb_to_ass(C.dim)),
               esc(fit(meta, pw * 0.34, fs_foot)))

            if S.downloading == idx then
                ev("an5\\pos(" .. (W / 2) .. "," .. (rowy + row_h / 2) .. ")\\fs" .. fs_row
                    .. "\\b1\\c" .. rgb_to_ass(o.accent), esc("Downloading…"))
            elseif S.saved and S.saved.idx == idx then
                ev("an3\\pos(" .. (x0 + pw * 0.5) .. "," .. (rowy + row_h * 0.12) .. ")\\fs" .. fs_foot
                    .. "\\b1\\c" .. rgb_to_ass(C.good), esc("✓ downloaded"))
            end
        end
    end

    -- toast (recent download notification)
    if S.notice and mp.get_time() < S.notice.expires then
        ev("an5\\pos(" .. (W / 2) .. "," .. (y0 + ph - L.footer_h - fs_foot * 1.6) .. ")\\fs" .. fs_foot
            .. "\\b1\\c" .. rgb_to_ass(C.good), esc(S.notice.text))
    end

    -- footer
    local fy = y0 + ph - L.footer_h
    ev("an7\\pos(" .. hx .. "," .. fy .. ")\\fs" .. fs_foot .. "\\b0\\c" .. rgb_to_ass(C.dim),
       esc("↑↓ move   Enter download   r refresh   l language(" .. S.lang
           .. ")   c cancel   Esc close"))
    ev("an3\\pos(" .. (x0 + pw - pw * 0.03) .. "," .. fy .. ")\\fs" .. fs_foot .. "\\c" .. rgb_to_ass(C.faint),
       esc("sub · submenu"))

    return table.concat(parts, "\n")
end

local function render()
    if S.mode == "closed" then
        ov.data = ""
        ov:update()
        return
    end
    ov.res_x, ov.res_y = W, H
    local ass = build_ass()
    ov.data = ass
    ov:update()
    if o.debug_osd_file and o.debug_osd_file ~= "" then
        local f = io.open(o.debug_osd_file, "w")
        if f then
            f:write(ass, "\n")
            f:close()
        end
    end
end

-- ---------------------------------------------------------------------------
-- async helper invocation
-- ---------------------------------------------------------------------------

-- directory of this script; mp.script_dir is nil when loaded via --script
local function script_dir_of()
    if mp.script_dir and mp.script_dir ~= "" then return mp.script_dir end
    local info = debug.getinfo(1, "S")
    local src = (info and info.source) or ""
    if src:sub(1, 1) == "@" then src = src:sub(2) end
    return src:match("^(.*)[/\\][^/\\]+$")
end

local function helper_path()
    if o.helper and o.helper ~= "" then return o.helper end
    local dir = script_dir_of()
    if dir then
        local p = utils.join_path(dir, "sub_helper.py")
        local f = io.open(p, "r")
        if f then f:close() return p end
    end
    return "sub_helper.py"
end

-- locate a runnable binary on PATH (mpv's Lua API has no find_executable)
local function find_executable(name)
    local path = os.getenv("PATH") or ""
    for dir in path:gmatch("[^:]+") do
        local f = utils.join_path(dir, name)
        local q = f:gsub("'", "'\\''")   -- shell-safe single-quote escaping
        local ok = os.execute("test -x '" .. q .. "'")
        if ok == 0 or ok == true then return f end
    end
    return nil
end

local function run_helper(args, cb)
    local py = find_executable("python3") or find_executable("python")
    if not py then
        cb(false, { status = -1, stderr = "python3 not found in PATH" })
        return nil
    end
    local all = { py, helper_path() }
    for _, a in ipairs(args) do all[#all + 1] = a end
    local req = {
        name = "subprocess",
        args = all,
        capture_stdout = true,
        capture_stderr = true,
        playback_only = false,
    }
    local ok, handle = pcall(mp.command_native_async, req, cb)
    return ok and handle or nil
end

local function cancel_job()
    if S.handle then
        pcall(mp.abort_async_command, S.handle)
        S.handle = nil
    end
end

local function set_loading(on)
    S.loading = on
    if on and not S.spinner_timer then
        S.spinner_timer = mp.add_periodic_timer(0.05, function()
            S.frame = S.frame + 1
            if S.loading then render() end
        end)
    elseif not on and S.spinner_timer then
        S.spinner_timer:stop()
        S.spinner_timer = nil
    end
end

local function parse_stdout(result)
    if not result or not result.stdout then return nil end
    local ok, data = pcall(utils.parse_json, (result.stdout or ""):gsub("%s+$", ""))
    if not ok then return nil end
    return data
end

local function do_search()
    if S.loading then return end
    local path = mp.get_property("path")
    if not path or path == "" then
        S.error = { title = "No media loaded", hint = "Open a video first." }
        render()
        return
    end
    if path:find("^ytdl://") or path:find("^https?://") or path:find("^dvd://")
       or path:find("^bd://") or path:find("^lavfi://") then
        S.error = { title = "Local files only",
            hint = "Subtitle downloading needs a real file on disk." }
        render()
        return
    end

    S.req_id = S.req_id + 1
    local rid = S.req_id
    S.error, S.subs, S.saved = nil, {}, nil
    S.sel, S.top = 1, 1
    set_loading(true)
    render()

    local args = { "search", path, "-l", S.lang, "-p", o.providers, "--max", tostring(o.max_results) }
    S.handle = run_helper(args, function(success, result)
        S.handle = nil
        set_loading(false)
        if S.mode ~= "picker" or rid ~= S.req_id then return end
        if not success or not result then
            S.error = { title = "Search failed", hint = "Could not run the helper script." }
            render()
            return
        end
        if result.status ~= 0 then
            local data = parse_stdout(result)
            S.error = {
                title = "Search failed",
                hint = (data and data.error)
                    or (result.stderr or ""):gsub("^%s+", ""):gsub("%s+$", ""),
            }
            render()
            return
        end
        local data = parse_stdout(result)
        if not data or not data.ok then
            S.error = { title = "Search failed",
                hint = (data and data.error) or "Unexpected helper output." }
            render()
            return
        end
        S.subs = data.subs or {}
        S.sel, S.top = 1, 1
        render()
    end)
end

-- ---------------------------------------------------------------------------
-- download
-- ---------------------------------------------------------------------------

local function select_saved_sub(path)
    local _, base = utils.split_path(path)
    local tries = 0
    local check
    check = function()
        tries = tries + 1
        local tl = mp.get_property_native("track-list") or {}
        for _, t in ipairs(tl) do
            if t.type == "sub" and t.external then
                local ef = t["external-filename"] or ""
                if ef:sub(-#base) == base then
                    mp.set_property("sid", tostring(t.id))
                    return
                end
            end
        end
        if tries < 30 then mp.add_timeout(0.1, check) end
    end
    mp.add_timeout(0.1, check)
end

local function do_download()
    if S.loading or S.downloading then return end
    if #S.subs == 0 then return end
    local idx = S.sel
    local sub = S.subs[idx]
    S.downloading = idx
    render()

    local path = mp.get_property("path") or ""
    local args = { "download", path, sub.provider, tostring(sub.id),
                   "-l", sub.language, "-e", o.encoding }
    if o.download_dir and o.download_dir ~= "" then
        args[#args + 1] = "-d"
        args[#args + 1] = o.download_dir
    end
    S.handle = run_helper(args, function(success, result)
        S.handle = nil
        S.downloading = nil
        if S.mode ~= "picker" then render() return end
        local data = parse_stdout(result)
        if not success or not result or result.status ~= 0 or not data or not data.ok then
            S.error = {
                title = "Download failed",
                hint = (data and (data.hint or data.error))
                    or (result and result.stderr or "Unknown error"),
            }
            render()
            return
        end
        S.saved = { idx = idx, name = data.basename or data.file, file = data.file }
        S.notice = { text = "Saved " .. (data.basename or data.file), expires = mp.get_time() + 5 }
        render()
        mp.commandv("rescan_external_files", "reselect")
        select_saved_sub(data.file)
    end)
end

-- ---------------------------------------------------------------------------
-- navigation
-- ---------------------------------------------------------------------------

local function clamp(v, lo, hi) return math.max(lo, math.min(hi, v)) end

local function nav(d)
    if S.mode ~= "picker" or #S.subs == 0 then return end
    S.sel = clamp(S.sel + d, 1, #S.subs)
    local L = layout()
    if S.sel < S.top then S.top = S.sel end
    if S.sel >= S.top + L.nrows then S.top = S.sel - L.nrows + 1 end
    render()
end

local function page(d)
    nav(d * layout().nrows)
end

local function cycle_lang()
    if S.mode ~= "picker" or S.loading then return end
    if #S.langs < 2 then return end
    S.lang_idx = (S.lang_idx % #S.langs) + 1
    S.lang = S.langs[S.lang_idx]
    do_search()
end

-- ---------------------------------------------------------------------------
-- open / close / bindings
-- ---------------------------------------------------------------------------

local bound = {}

local function bind(key, name, fn)
    mp.add_forced_key_binding(key, name, fn)
    bound[name] = true
end

local function install_bindings()
    bind("UP", "submenu_up", function() nav(-1) end)
    bind("DOWN", "submenu_down", function() nav(1) end)
    bind("k", "submenu_up_k", function() nav(-1) end)
    bind("j", "submenu_down_j", function() nav(1) end)
    bind("PGUP", "submenu_pgup", function() page(-1) end)
    bind("PGDN", "submenu_pgdn", function() page(1) end)
    bind("g", "submenu_top", function()
        if #S.subs > 0 then S.sel, S.top = 1, 1 render() end
    end)
    bind("G", "submenu_bottom", function()
        if #S.subs > 0 then
            S.sel = #S.subs
            S.top = math.max(1, #S.subs - layout().nrows + 1)
            render()
        end
    end)
    bind("ENTER", "submenu_enter", do_download)
    bind("KP_ENTER", "submenu_kpenter", do_download)
    bind("SPACE", "submenu_space", do_download)
    bind("ESC", "submenu_esc", close)
    bind("q", "submenu_q", close)
    bind("r", "submenu_r", do_search)
    bind("c", "submenu_c", function()
        if S.loading then
            cancel_job()
            set_loading(false)
            S.error = { title = "Search cancelled" }
            render()
        end
    end)
    bind("l", "submenu_l", cycle_lang)
    bind("MBTN_LEFT", "submenu_mleft", function()
        local pos = mp.get_property_native("mouse-pos") or {}
        if not (pos.x and pos.y) then return end
        local L = layout()
        if pos.x < L.x0 or pos.x > L.x0 + L.pw then return end
        local idx = S.top + math.floor((pos.y - L.list_top) / L.row_h)
        if idx >= 1 and idx <= #S.subs then
            S.sel = idx
            if S.sel < S.top then S.top = S.sel end
            if S.sel >= S.top + L.nrows then S.top = S.sel - L.nrows + 1 end
            render()
        end
    end)
    bind("MBTN_LEFT_DBL", "submenu_mdbl", do_download)
    bind("MBTN_RIGHT", "submenu_mright", close)
    bind("WHEEL_UP", "submenu_wup", function() nav(-1) end)
    bind("WHEEL_DOWN", "submenu_wdown", function() nav(1) end)
end

local function uninstall_bindings()
    for name in pairs(bound) do
        pcall(mp.remove_key_binding, name)
        bound[name] = nil
    end
end

local function open()
    if S.mode == "picker" then close() return end
    local path = mp.get_property("path")
    if not path or path == "" then
        mp.osd_message("Subtitle downloader: no file loaded", 2)
        return
    end
    S.mode = "picker"
    S.langs = {}
    for lang in (o.languages .. ""):gmatch("[%a-_]+") do
        if lang ~= "" then S.langs[#S.langs + 1] = lang end
    end
    if #S.langs == 0 then S.langs = { "en" } end
    S.lang_idx = 1
    S.lang = S.langs[1]
    S.subs, S.error, S.saved, S.downloading = {}, nil, nil, nil
    install_bindings()
    render()
    do_search()
end

local function close()
    if S.mode == "closed" then return end
    cancel_job()
    set_loading(false)
    S.mode = "closed"
    uninstall_bindings()
    render()
end

-- ---------------------------------------------------------------------------
-- entry points
-- ---------------------------------------------------------------------------

mp.add_key_binding(o.key, "open", open)
mp.register_script_message("open", open)
mp.register_script_message("close", close)
mp.register_script_message("toggle", function()
    if S.mode == "picker" then close() else open() end
end)

mp.register_event("end-file", close)
mp.observe_property("idle-active", "bool", function(_, val)
    if val and S.mode == "picker" then close() end
end)

msg.info("submenu loaded — press " .. o.key .. " or use the 'sub' menu entry")
