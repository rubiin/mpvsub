-- submenu.lua — VLC-style subtitle search & download UI for mpv.
--
-- CTRL+s (default) opens a full OSD dialog with:
--   * a search form: language dropdown, title / season / episode fields
--     and "Search by hash" / "Search by name" actions,
--   * a scrollable results list with hover + selection highlight,
--   * a status bar ("Research complete: N result(s)"),
--   * footer buttons: Show help · Show config · Download selection · Close.
--
-- Requirements:
--   * mpv >= 0.33 (async subprocess + OSD overlays)
--   * subliminal (pipx install subliminal) — see sub_helper.py
--   * network access to OpenSubtitles.com etc.
--
-- Keys while the dialog is open:
--   Tab / Shift+Tab     move focus between controls
--   ↑/↓ ←/→             navigate (form rows, list, buttons, dropdown)
--   Enter               activate the focused control / download selection
--   Space               activate (types a space inside text fields)
--   a-z 0-9 …           type in the focused text field
--   Backspace           delete the previous character
--   r                   search by hash          n   search by name
--   l                   cycle language          c   cancel search
--   g / G               first / last result     PgUp/PgDn   page
--   Esc                 close (first closes popup / help / config)
--
--   Mouse: hover highlights rows and buttons, click focuses / activates,
--   double-click downloads, wheel scrolls, right-click closes.
--
-- Config: script-opts/submenu.conf (see submenu.conf in this folder).

local mp = require 'mp'
local msg = require 'mp.msg'
local utils = require 'mp.utils'
local options = require 'mp.options'

o = {
    key = "CTRL+s",            -- open the subtitle dialog
    languages = "en",          -- comma separated IETF codes
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

local function clamp(v, lo, hi) return math.max(lo, math.min(hi, v)) end

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

local function backspace_str(s)
    local i = #s
    if i == 0 then return s end
    local b = s:byte(i)
    if b < 0x80 then return s:sub(1, i - 1) end
    local j = i
    while j > 1 do
        j = j - 1
        local c = s:byte(j)
        if c < 0x80 or c >= 0xC0 then break end
    end
    if j < 1 then j = 1 end
    return s:sub(1, j - 1)
end

-- ---------------------------------------------------------------------------
-- state
-- ---------------------------------------------------------------------------

local S = {
    mode = "closed",       -- "closed" | "picker"
    focus = "list",        -- focused control id
    dropdown = false,      -- language dropdown popup open
    ddl_idx = 1,
    fields = { title = "", season = "", episode = "" },
    lang = "en",
    langs = { "en" },
    lang_idx = 1,
    subs = {},
    sel = 1,
    top = 1,
    search_mode = "hash",  -- last search that ran: "hash" | "name"
    status = "",
    status_color = nil,
    loading = false,
    error = nil,           -- { title = .., hint = .. }
    notice = nil,          -- { text = .., expires = clock }
    downloading = nil,     -- index currently downloading
    help = false,          -- modal overlays
    config = false,
    hover_row = nil,
    hover_btn = nil,
    hover_field = nil,
    req_id = 0,
    handle = nil,          -- async subprocess handle
    frame = 0,
    timer = nil,
}

local W, H = 1920, 1080

-- forward declarations: these functions are referenced (inside closures)
-- before their definitions appear below, so the references must resolve to
-- a local declared here rather than a global.
local render
local close
local on_mouse_left
local row_at

-- osd-dimensions can briefly report 0x0, or collapse to a 20x20 fallback
-- when the video is paused / not actively rendering. Only trust sane values
-- so the dialog never renders at a tiny size.
local function sane_dims(dim)
    return dim and dim.w and dim.h and dim.w >= 100 and dim.h >= 100
end

mp.observe_property("osd-dimensions", "native", function(_, dim)
    if sane_dims(dim) then
        W, H = dim.w, dim.h
        if S.mode ~= "closed" then render() end
    end
end)

local ov = mp.create_osd_overlay("ass-events")
ov.z = 1000

local C = { text = "E8EDF3", dim = "A9B2BC", faint = "8E97A4",
            bad = "E06C75", good = "7BC96F" }
local U = { border = "2A2D35", fill = "14161B", fill2 = "1A1D24",
            hover = "23262E", panel_in = "101216" }

local PROVIDER_SHORT = {
    opensubtitlescom = "OSC", opensubtitles = "OS", podnapisi = "podn",
    subtis = "subtis", tvsubtitles = "tvsub", addic7ed = "a7ed",
    napiprojekt = "napi", subtitulamos = "subs", legendastv = "lgd",
    bsplayer = "bspl", gestdown = "gest",
}

local function provider_label(p) return PROVIDER_SHORT[p] or p end

local FOOTER_DEFS = {
    { id = "btn_help",     label = "Show help" },
    { id = "btn_config",   label = "Show config" },
    { id = "btn_download", label = "Download selection" },
    { id = "btn_close",    label = "Close" },
}

local FOCUS_ORDER = { "lang", "title", "season", "episode",
                      "btn_hash", "btn_name", "list",
                      "btn_help", "btn_config", "btn_download", "btn_close" }

-- row (1..4) and column ("input"|"btn") of form controls
local ROW_IDS = {
    { "lang", "btn_hash" },
    { "title", "btn_name" },
    { "season" },
    { "episode" },
}

local function is_text_field(id)
    return id == "title" or id == "season" or id == "episode"
end

local function in_rect(px, py, r)
    return r and px >= r.x and px <= r.x + r.w and py >= r.y and py <= r.y + r.h
end

-- ---------------------------------------------------------------------------
-- layout
-- ---------------------------------------------------------------------------

local function layout()
    local pw = math.min(W * o.panel_width, W - W * 0.06)
    local ph = math.min(H * o.panel_height, H - H * 0.08)
    local x0, y0 = (W - pw) / 2, (H - ph) / 2
    local fs = {
        label  = H * 0.019,
        input  = H * 0.022,
        row    = H * 0.021,
        button = H * 0.018,
        status = H * 0.017,
        title  = H * 0.024,
    }
    local gap = pw * 0.03
    local col_label_w = pw * 0.24
    local col_input_w = pw * 0.40
    local col_btn_w   = pw * 0.245
    local col_input_x = x0 + gap + col_label_w + pw * 0.012
    local col_btn_x   = x0 + pw - gap - col_btn_w

    local form_top = y0 + H * 0.014
    local row_h = fs.input * 2.6
    local rows = {}
    for i = 1, 4 do rows[i] = form_top + (i - 1) * row_h end
    local fh = row_h * 0.62                       -- field box height

    local fields = {
        lang    = { x = col_input_x, y = rows[1] + (row_h - fh) / 2, w = col_input_w, h = fh },
        title   = { x = col_input_x, y = rows[2] + (row_h - fh) / 2, w = col_input_w, h = fh },
        season  = { x = col_input_x, y = rows[3] + (row_h - fh) / 2, w = col_input_w * 0.55, h = fh },
        episode = { x = col_input_x, y = rows[4] + (row_h - fh) / 2, w = col_input_w * 0.55, h = fh },
    }
    local btn_hash = { x = col_btn_x, y = rows[1] + (row_h - fh) / 2, w = col_btn_w, h = fh }
    local btn_name = { x = col_btn_x, y = rows[2] + (row_h - fh) / 2, w = col_btn_w, h = fh }

    local form_bottom = rows[4] + row_h + H * 0.010
    local status_h = fs.status * 2.8
    local footer_h = fs.button * 3.4
    local res_bottom = y0 + ph - status_h - footer_h - H * 0.008
    local res_top = form_bottom
    local head_h = fs.title * 2.6
    local list_top = res_top + head_h
    local lh = fs.row * 1.9
    local nrows = math.max(1, math.floor((res_bottom - list_top - H * 0.010) / lh))

    local btn_gap = pw * 0.012
    local bw = (pw - 2 * gap - 3 * btn_gap) / 4
    local fy = y0 + ph - footer_h + footer_h * 0.16
    local fh2 = footer_h * 0.62
    local footer_btns = {}
    for i, def in ipairs(FOOTER_DEFS) do
        footer_btns[i] = {
            id = def.id, label = def.label,
            x = x0 + gap + (i - 1) * (bw + btn_gap),
            y = fy, w = bw, h = fh2,
        }
    end

    return {
        pw = pw, ph = ph, x0 = x0, y0 = y0, fs = fs,
        gap = gap, col_label_w = col_label_w,
        rows = rows, row_h = row_h, fields = fields,
        btn_hash = btn_hash, btn_name = btn_name,
        form_bottom = form_bottom, res_top = res_top, res_bottom = res_bottom,
        list_top = list_top, lh = lh, nrows = nrows,
        footer_btns = footer_btns, status_h = status_h, footer_h = footer_h,
    }
end

-- ---------------------------------------------------------------------------
-- rendering (ASS)
-- ---------------------------------------------------------------------------

local function draw_box(ev, r, fill, border_color, border_w)
    if border_color and border_w and border_w > 0 then
        ev("an7\\pos(" .. r.x .. "," .. r.y .. ")\\p1\\bord0\\shad0\\c" .. border_color .. "\\1a&H00&",
           "m 0 0 l " .. r.w .. " 0 l " .. r.w .. " " .. r.h .. " l 0 " .. r.h .. " z")
        local inner = { x = r.x + border_w, y = r.y + border_w,
                        w = r.w - 2 * border_w, h = r.h - 2 * border_w }
        if inner.w > 0 and inner.h > 0 then
            ev("an7\\pos(" .. inner.x .. "," .. inner.y .. ")\\p1\\bord0\\shad0\\c" .. fill .. "\\1a&H00&",
               "m 0 0 l " .. inner.w .. " 0 l " .. inner.w .. " " .. inner.h .. " l 0 " .. inner.h .. " z")
        end
    else
        ev("an7\\pos(" .. r.x .. "," .. r.y .. ")\\p1\\bord0\\shad0\\c" .. fill .. "\\1a&H00&",
           "m 0 0 l " .. r.w .. " 0 l " .. r.w .. " " .. r.h .. " l 0 " .. r.h .. " z")
    end
end

local function caret_on()
    return (math.floor(S.frame / 5) % 2) == 0
end

local function build_ass()
    local L = layout()
    local parts = {}
    local function ev(tags, text)
        -- bord0/shad0: strip the default OSD outline/shadow (mpv.conf
        -- osd-outline-size) so text stays clean and light on the panel.
        parts[#parts + 1] = "{\\" .. tags .. "\\bord0\\shad0}" .. text
    end

    local x0, y0, pw, ph = L.x0, L.y0, L.pw, L.ph
    local fs = L.fs
    local hx = x0 + pw * 0.03

    -- dim the video behind the panel
    ev("an7\\pos(0,0)\\p1\\bord0\\shad0\\c&H000000&\\1a" .. alpha(120),
       "m 0 0 l " .. W .. " 0 l " .. W .. " " .. H .. " l 0 " .. H .. " z")
    -- panel background
    ev("an7\\pos(" .. x0 .. "," .. y0 .. ")\\p1\\bord0\\shad0\\c" .. rgb_to_ass(o.bg)
        .. "\\1a" .. alpha(26),
       "m 0 0 l " .. pw .. " 0 l " .. pw .. " " .. ph .. " l 0 " .. ph .. " z")
    -- accent top bar
    ev("an7\\pos(" .. x0 .. "," .. y0 .. ")\\p1\\bord0\\shad0\\c" .. rgb_to_ass(o.accent)
        .. "\\1a&H00&",
       "m 0 0 l " .. pw .. " 0 l " .. pw .. " " .. H * 0.006 .. " l 0 " .. H * 0.006 .. " z")

    -- ---------------------------------------------------------------------
    -- form section
    -- ---------------------------------------------------------------------
    local labels = {
        [1] = "Subtitle language",
        [2] = "Title",
        [3] = "Season (series)",
        [4] = "Episode (series)",
    }
    local placeholders = {
        lang = "", title = "Movie title…", season = "e.g. 1", episode = "e.g. 1",
    }
    local label_x = x0 + L.gap + L.col_label_w
    for row = 1, 4 do
        local ly = L.rows[row] + L.row_h * 0.18
        ev("an3\\pos(" .. (label_x - pw * 0.012) .. "," .. ly .. ")\\fs" .. fs.label
            .. "\\b0\\c" .. rgb_to_ass(C.dim), esc(labels[row]))
    end

    -- language dropdown
    local fl = L.fields.lang
    local focus = S.focus
    local border, border_c = H * 0.002, rgb_to_ass(U.border)
    if focus == "lang" then border_c = rgb_to_ass(o.accent) end
    if S.hover_field == "lang" and focus ~= "lang" then border_c = rgb_to_ass(C.dim) end
    draw_box(ev, fl, rgb_to_ass(U.fill), border_c, border)
    local lang_text = S.lang
    ev("an7\\pos(" .. (fl.x + pw * 0.012) .. "," .. (fl.y + fl.h * 0.1) .. ")\\fs" .. fs.input
        .. "\\b0\\c" .. rgb_to_ass(C.text), esc(fit(lang_text, fl.w - pw * 0.06, fs.input)))
    ev("an5\\pos(" .. (fl.x + fl.w - pw * 0.02) .. "," .. (fl.y + fl.h / 2) .. ")\\fs" .. fs.input
        .. "\\b0\\c" .. rgb_to_ass(focus == "lang" and o.accent or C.dim),
       esc(S.dropdown and "▲" or "▼"))

    -- title / season / episode text fields
    for _, id in ipairs({ "title", "season", "episode" }) do
        local f = L.fields[id]
        local b2, bc2 = H * 0.002, rgb_to_ass(U.border)
        if focus == id then bc2 = rgb_to_ass(o.accent) end
        if S.hover_field == id and focus ~= id then bc2 = rgb_to_ass(C.dim) end
        draw_box(ev, f, rgb_to_ass(U.fill), bc2, b2)
        local txt = S.fields[id]
        if txt ~= "" then
            local disp = txt
            if focus == id and caret_on() then disp = disp .. "|" end
            ev("an7\\pos(" .. (f.x + pw * 0.012) .. "," .. (f.y + f.h * 0.1) .. ")\\fs" .. fs.input
                .. "\\b0\\c" .. rgb_to_ass(C.text), esc(fit(disp, f.w - pw * 0.024, fs.input)))
        else
            local disp = placeholders[id]
            if focus == id and caret_on() then disp = disp .. "|" end
            ev("an7\\pos(" .. (f.x + pw * 0.012) .. "," .. (f.y + f.h * 0.1) .. ")\\fs" .. fs.input
                .. "\\b0\\c" .. rgb_to_ass(C.faint), esc(fit(disp, f.w - pw * 0.024, fs.input)))
        end
    end

    -- search buttons
    local function btn(rect, id, label, primary)
        local bc, bcw = rgb_to_ass(U.border), H * 0.002
        local fill = rgb_to_ass(primary and o.accent or U.fill2)
        local tcol = rgb_to_ass(primary and "111111" or C.text)
        if focus == id then bc, bcw = rgb_to_ass(o.accent), H * 0.003 end
        if S.hover_btn == id and focus ~= id then fill = rgb_to_ass(primary and o.accent or U.hover) end
        draw_box(ev, rect, fill, bc, bcw)
        ev("an5\\pos(" .. (rect.x + rect.w / 2) .. "," .. (rect.y + rect.h / 2) .. ")\\fs" .. fs.button
            .. "\\b0\\c" .. tcol, esc(label))
    end
    btn(L.btn_hash, "btn_hash", "Search by hash", false)
    btn(L.btn_name, "btn_name", "Search by name", false)

    -- divider
    ev("an7\\pos(" .. hx .. "," .. L.form_bottom .. ")\\p1\\bord0\\shad0\\c" .. rgb_to_ass(U.border)
        .. "\\1a&H00&", "m 0 0 l " .. (pw - 2 * L.gap) .. " 0 l " .. (pw - 2 * L.gap) .. " "
        .. H * 0.002 .. " l 0 " .. H * 0.002 .. " z")

    -- ---------------------------------------------------------------------
    -- results panel
    -- ---------------------------------------------------------------------
    local rp = { x = hx, y = L.res_top, w = pw - 2 * L.gap, h = L.res_bottom - L.res_top }
    draw_box(ev, rp, rgb_to_ass(U.panel_in), rgb_to_ass(U.border), H * 0.002)

    -- header
    ev("an7\\pos(" .. (hx + pw * 0.014) .. "," .. (L.res_top + fs.title * 0.55) .. ")\\fs" .. fs.title
        .. "\\b0\\c" .. rgb_to_ass(C.text), esc("Subtitle search results"))
    ev("an3\\pos(" .. (x0 + pw - L.gap - pw * 0.014) .. "," .. (L.res_top + fs.title * 0.75) .. ")\\fs" .. fs.status
        .. "\\b0\\c" .. rgb_to_ass(C.faint),
       esc(S.loading and "searching…" or (S.search_mode == "name" and "by name" or "by hash")))

    if S.loading then
        local cx, cy = W / 2, (L.res_top + L.res_bottom) / 2
        local r = H * 0.018
        local ang = (S.frame % 60) / 60 * math.pi * 2
        local seg = math.pi * 1.7
        local p = {}
        for k = 0, 24 do
            local a = ang + seg * k / 24
            p[#p + 1] = string.format("%.1f %.1f", cx + math.cos(a) * r, cy + math.sin(a) * r)
        end
        ev("an7\\pos(0,0)\\p1\\bord0\\shad0\\c" .. rgb_to_ass(o.accent) .. "\\1a&H00&",
           "m " .. cx .. " " .. cy .. " " .. table.concat(p, " l ") .. " z")
        ev("an5\\pos(" .. cx .. "," .. (cy + r * 2.4) .. ")\\fs" .. fs.row .. "\\b0\\c" .. rgb_to_ass(C.text),
           esc("Searching subtitles…"))
    elseif S.error then
        ev("an5\\pos(" .. (W / 2) .. "," .. ((L.res_top + L.res_bottom) / 2 - fs.row) .. ")\\fs" .. fs.title
            .. "\\b0\\c" .. rgb_to_ass(C.bad), esc(S.error.title or "Something went wrong"))
        if S.error.hint and S.error.hint ~= "" then
            ev("an5\\pos(" .. (W / 2) .. "," .. ((L.res_top + L.res_bottom) / 2 + fs.row * 1.1) .. ")\\fs" .. fs.row
                .. "\\b0\\c" .. rgb_to_ass("9AA4AF"),
               esc(fit(S.error.hint, rp.w - pw * 0.1, fs.row)))
        end
    elseif #S.subs == 0 then
        ev("an5\\pos(" .. (W / 2) .. "," .. ((L.res_top + L.res_bottom) / 2 - fs.row) .. ")\\fs" .. fs.title
            .. "\\b0\\c" .. rgb_to_ass(C.text), esc("No subtitles found for “" .. S.lang .. "”"))
        ev("an5\\pos(" .. (W / 2) .. "," .. ((L.res_top + L.res_bottom) / 2 + fs.row * 1.1) .. ")\\fs" .. fs.row
            .. "\\b0\\c" .. rgb_to_ass(C.dim),
           esc("Try another language (l), search by name, or check the providers."))
    else
        local padx = pw * 0.014
        for i = 1, L.nrows do
            local idx = S.top + i - 1
            if idx > #S.subs then break end
            local sub = S.subs[idx]
            local rowy = L.list_top + (i - 1) * L.lh
            local rh2 = L.lh - H * 0.004
            local selected = (idx == S.sel)
            local hovered = (S.hover_row == idx and not selected)

            if selected then
                ev("an7\\pos(" .. (hx + padx) .. "," .. rowy .. ")\\p1\\bord0\\shad0\\c" .. rgb_to_ass(o.accent)
                    .. "\\1a" .. alpha(72),
                   "m 0 0 l " .. (rp.w - 2 * padx) .. " 0 l " .. (rp.w - 2 * padx) .. " " .. rh2
                   .. " l 0 " .. rh2 .. " z")
                ev("an7\\pos(" .. (hx + padx) .. "," .. rowy .. ")\\p1\\bord0\\shad0\\c" .. rgb_to_ass(o.accent)
                    .. "\\1a&H00&",
                   "m 0 0 l " .. H * 0.004 .. " 0 l " .. H * 0.004 .. " " .. rh2 .. " l 0 " .. rh2 .. " z")
            elseif hovered then
                ev("an7\\pos(" .. (hx + padx) .. "," .. rowy .. ")\\p1\\bord0\\shad0\\c" .. rgb_to_ass(U.hover)
                    .. "\\1a&H00&",
                   "m 0 0 l " .. (rp.w - 2 * padx) .. " 0 l " .. (rp.w - 2 * padx) .. " " .. rh2
                   .. " l 0 " .. rh2 .. " z")
            end

            local name = (sub.name and sub.name ~= "" and sub.name)
                or (sub.release_info or "Subtitle")
            local extra = "(" .. provider_label(sub.provider)
                .. (sub.download_count and (", " .. sub.download_count .. " dl") or "") .. ")"
            local line = name .. "  [" .. sub.language .. "]  " .. extra
            ev("an7\\pos(" .. (hx + padx + pw * 0.012) .. "," .. (rowy + L.lh * 0.22) .. ")\\fs" .. fs.row
                .. "\\b0\\c" .. rgb_to_ass(C.text),
               esc(fit(line, rp.w - 2 * padx - pw * 0.024, fs.row)))

            if S.downloading == idx then
                ev("an5\\pos(" .. (W / 2) .. "," .. (rowy + L.lh * 0.45) .. ")\\fs" .. fs.row
                    .. "\\b0\\c" .. rgb_to_ass(o.accent), esc("Downloading…"))
            end
        end

        -- scrollbar
        if #S.subs > L.nrows then
            local sx = x0 + pw - L.gap - pw * 0.008
            local st, sh = L.list_top, L.res_bottom - L.list_top - H * 0.010
            local thumb_h = math.max(H * 0.02, sh * L.nrows / #S.subs)
            local max_top = math.max(1, #S.subs - L.nrows + 1)
            local frac = (S.top - 1) / math.max(1, max_top - 1)
            local ty = st + frac * (sh - thumb_h)
            ev("an7\\pos(" .. sx .. "," .. st .. ")\\p1\\bord0\\shad0\\c" .. rgb_to_ass(U.border)
                .. "\\1a&H00&", "m 0 0 l " .. H * 0.003 .. " 0 l " .. H * 0.003 .. " " .. sh .. " z")
            ev("an7\\pos(" .. sx .. "," .. ty .. ")\\p1\\bord0\\shad0\\c" .. rgb_to_ass(C.dim)
                .. "\\1a&H00&", "m 0 0 l " .. H * 0.003 .. " 0 l " .. H * 0.003 .. " " .. thumb_h .. " z")
        end
    end

    -- ---------------------------------------------------------------------
    -- status bar
    -- ---------------------------------------------------------------------
    local status_text = S.status
    if status_text == "" then
        if S.error then status_text = S.error.title or "Error"
        elseif S.loading then status_text = "Searching…"
        elseif #S.subs > 0 then
            status_text = "Research complete: " .. #S.subs .. " result" .. (#S.subs == 1 and "" or "s")
        end
    end
    local scol = S.error and C.bad or (S.status_color or C.dim)
    ev("an7\\pos(" .. hx .. "," .. (L.res_bottom + fs.status * 0.75) .. ")\\fs" .. fs.status
        .. "\\b0\\c" .. rgb_to_ass(scol), esc(fit(status_text, pw * 0.7, fs.status)))

    -- ---------------------------------------------------------------------
    -- footer buttons
    -- ---------------------------------------------------------------------
    for _, b in ipairs(L.footer_btns) do
        local primary = (b.id == "btn_download")
        local bc, bcw = rgb_to_ass(U.border), H * 0.002
        local fill = rgb_to_ass(primary and o.accent or U.fill2)
        local tcol = rgb_to_ass(primary and "111111" or C.text)
        if focus == b.id then bc, bcw = rgb_to_ass(o.accent), H * 0.003 end
        if S.hover_btn == b.id and focus ~= b.id then fill = rgb_to_ass(primary and o.accent or U.hover) end
        draw_box(ev, b, fill, bc, bcw)
        ev("an5\\pos(" .. (b.x + b.w / 2) .. "," .. (b.y + b.h / 2) .. ")\\fs" .. fs.button
            .. "\\b0\\c" .. tcol, esc(b.label))
    end

    -- toast (recent download notification)
    if S.notice and mp.get_time() < S.notice.expires then
        ev("an5\\pos(" .. (W / 2) .. "," .. (y0 + ph - L.footer_h - fs.status * 2.2) .. ")\\fs" .. fs.status
            .. "\\b0\\c" .. rgb_to_ass(C.good), esc(S.notice.text))
    end

    -- ---------------------------------------------------------------------
    -- language dropdown popup
    -- ---------------------------------------------------------------------
    if S.dropdown and #S.langs > 0 then
        local f = L.fields.lang
        local nv = math.min(#S.langs, 6)
        local ih = fs.row * 1.9
        local pw2 = f.w
        local ph2 = nv * ih + H * 0.008
        local px, py = f.x, f.y + f.h + H * 0.004
        draw_box(ev, { x = px, y = py, w = pw2, h = ph2 }, rgb_to_ass("121318"),
                 rgb_to_ass(o.accent), H * 0.002)
        -- window around the selection so it stays visible with many languages
        local ddl_top = clamp(S.ddl_idx - 2, 1, math.max(1, #S.langs - nv + 1))
        for i = 1, nv do
            local gi = ddl_top + i - 1
            if gi > #S.langs then break end
            local ly = py + H * 0.004 + (i - 1) * ih
            if gi == S.ddl_idx then
                ev("an7\\pos(" .. (px + H * 0.002) .. "," .. ly .. ")\\p1\\bord0\\shad0\\c" .. rgb_to_ass(o.accent)
                    .. "\\1a" .. alpha(72),
                   "m 0 0 l " .. (pw2 - H * 0.004) .. " 0 l " .. (pw2 - H * 0.004) .. " " .. (ih - H * 0.004)
                   .. " l 0 " .. (ih - H * 0.004) .. " z")
            end
            ev("an7\\pos(" .. (px + pw * 0.015) .. "," .. (ly + ih * 0.22) .. ")\\fs" .. fs.row
                .. "\\b0\\c" .. (gi == S.ddl_idx and rgb_to_ass(o.accent) or rgb_to_ass(C.text)),
               esc(S.langs[gi]))
        end
    end

    -- ---------------------------------------------------------------------
    -- help / config modals
    -- ---------------------------------------------------------------------
    local function modal(title, pairs)
        local mw, mh = pw * 0.72, ph * 0.74
        local mx, my = x0 + (pw - mw) / 2, y0 + (ph - mh) / 2
        draw_box(ev, { x = mx, y = my, w = mw, h = mh }, rgb_to_ass("121318"),
                 rgb_to_ass(o.accent), H * 0.002)
        ev("an7\\pos(" .. (mx + mw * 0.05) .. "," .. (my + mh * 0.06) .. ")\\fs" .. fs.title
            .. "\\b0\\c" .. rgb_to_ass(o.accent), esc(title))
        local lh2 = fs.row * 1.8
        local ty = my + mh * 0.16
        for i, pair in ipairs(pairs) do
            local kpart, vpart = pair[1], pair[2]
            local y = ty + (i - 1) * lh2
            if y + lh2 > my + mh - mh * 0.05 then break end
            ev("an7\\pos(" .. (mx + mw * 0.05) .. "," .. y .. ")\\fs" .. fs.row
                .. "\\b0\\c" .. rgb_to_ass(C.dim), esc(fit(kpart, mw * 0.3, fs.row)))
            ev("an7\\pos(" .. (mx + mw * 0.38) .. "," .. y .. ")\\fs" .. fs.row
                .. "\\b0\\c" .. rgb_to_ass(C.text), esc(fit(vpart, mw * 0.56, fs.row)))
        end
        ev("an5\\pos(" .. (mx + mw / 2) .. "," .. (my + mh - mh * 0.04) .. ")\\fs" .. fs.status
            .. "\\b0\\c" .. rgb_to_ass(C.faint), esc("Esc to close"))
    end

    if S.help then
        modal("Help — keys", {
            { "Tab / Shift+Tab", "move focus" },
            { "↑ ↓ ← →", "navigate form, list, buttons" },
            { "Enter", "activate / download selection" },
            { "Space", "activate (or type a space)" },
            { "letters / digits", "type in a text field" },
            { "Backspace", "delete previous character" },
            { "r / n", "search by hash / by name" },
            { "l", "cycle language" },
            { "c", "cancel running search" },
            { "g / G", "first / last result" },
            { "PgUp / PgDn", "page through results" },
            { "Esc", "close dialog" },
            { "mouse", "hover, click, double-click, wheel, right-click" },
        })
    elseif S.config then
        local dir = (o.download_dir and o.download_dir ~= "") and o.download_dir or "(video folder)"
        modal("Configuration", {
            { "languages", o.languages },
            { "providers", o.providers },
            { "download_dir", dir },
            { "encoding", o.encoding },
            { "max_results", tostring(o.max_results) },
            { "panel_width", tostring(o.panel_width) },
            { "panel_height", tostring(o.panel_height) },
            { "accent", o.accent },
            { "bg", o.bg },
            { "helper", (o.helper ~= "") and o.helper or "(next to script)" },
        })
    end

    return table.concat(parts, "\n")
end

render = function()
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

local function find_executable(name)
    local path = os.getenv("PATH") or ""
    for dir in path:gmatch("[^:]+") do
        local f = utils.join_path(dir, name)
        local q = f:gsub("'", "'\\''")
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
end

local function parse_stdout(result)
    if not result or not result.stdout then return nil end
    local ok, data = pcall(utils.parse_json, (result.stdout or ""):gsub("%s+$", ""))
    if not ok then return nil end
    return data
end

local function start_search(mode)
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

    local args = { "search", path, "-l", S.lang, "-p", o.providers, "--max", tostring(o.max_results) }
    if mode == "name" then
        local title = S.fields.title:match("^%s*(.-)%s*$") or ""
        if title == "" then
            S.error = { title = "Enter a title", hint = "Type a movie or series name in the Title field." }
            render()
            return
        end
        args[#args + 1] = "--query"
        args[#args + 1] = title
        local season = tonumber(S.fields.season:match("^%s*(%d+)%s*$"))
        local episode = tonumber(S.fields.episode:match("^%s*(%d+)%s*$"))
        if season and episode then
            args[#args + 1] = "--season"
            args[#args + 1] = tostring(season)
            args[#args + 1] = "--episode"
            args[#args + 1] = tostring(episode)
        end
    end

    S.search_mode = mode
    S.req_id = S.req_id + 1
    local rid = S.req_id
    S.error, S.subs, S.downloading = nil, {}, nil
    S.sel, S.top = 1, 1
    S.hover_row, S.hover_btn, S.hover_field = nil, nil, nil
    S.status, S.status_color = "", nil
    set_loading(true)
    render()

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

local function do_hash_search() start_search("hash") end
local function do_name_search() start_search("name") end

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
    S.status, S.status_color = "Downloading…", C.dim
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
        S.notice = { text = "Saved " .. (data.basename or data.file), expires = mp.get_time() + 5 }
        S.status, S.status_color = "Downloaded " .. (data.basename or data.file), C.good
        render()
        mp.commandv("rescan_external_files", "reselect")
        select_saved_sub(data.file)
    end)
end

-- ---------------------------------------------------------------------------
-- focus / navigation
-- ---------------------------------------------------------------------------

local function focus_index(id)
    for i, v in ipairs(FOCUS_ORDER) do if v == id then return i end end
    return 1
end

local function set_focus(id)
    S.focus = id
    S.dropdown = false
    render()
end

local function focus_step(d)
    local n = #FOCUS_ORDER
    -- wrap around so Tab never dead-ends at the last control
    local i = ((focus_index(S.focus) - 1 + d) % n) + 1
    local id = FOCUS_ORDER[i]
    -- Tab from the results list jumps straight to the title field
    if d > 0 and S.focus == "list" then id = "title" end
    set_focus(id)
end

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

local function footer_index(id)
    for i, def in ipairs(FOOTER_DEFS) do if def.id == id then return i end end
    return nil
end

-- move within the 2-column form grid
local function form_move(dx, dy)
    local f = S.focus
    local row, col
    for r, ids in ipairs(ROW_IDS) do
        for c, id in ipairs(ids) do
            if id == f then row, col = r, c end
        end
    end
    if not row then return end
    if dy ~= 0 then
        local nr = clamp(row + dy, 1, #ROW_IDS)
        local ids = ROW_IDS[nr]
        local ncol = clamp(col or 1, 1, #ids)
        set_focus(ids[ncol])
    elseif dx ~= 0 then
        local ids = ROW_IDS[row]
        if #ids > 1 then
            local ncol = clamp((col or 1) + dx, 1, #ids)
            set_focus(ids[ncol])
        end
    end
end

local function arrow(dx, dy)
    if S.mode ~= "picker" then return end
    if S.help or S.config then return end
    if S.dropdown then
        if dy ~= 0 then
            S.ddl_idx = clamp(S.ddl_idx + dy, 1, #S.langs)
            render()
        end
        return
    end
    local f = S.focus
    if f == "list" then
        if dy ~= 0 then nav(dy) else set_focus(dx > 0 and "btn_download" or "episode") end
        return
    end
    local fi = footer_index(f)
    if fi then
        if dx ~= 0 then
            set_focus(FOOTER_DEFS[clamp(fi + dx, 1, #FOOTER_DEFS)].id)
        elseif dy < 0 then
            set_focus("list")
        end
        return
    end
    form_move(dx, dy)
end

local function activate()
    if S.mode ~= "picker" then return end
    if S.dropdown then
        S.lang = S.langs[S.ddl_idx]
        S.lang_idx = S.ddl_idx
        S.dropdown = false
        render()
        return
    end
    if S.help then S.help = false render() return end
    if S.config then S.config = false render() return end
    local f = S.focus
    if f == "lang" then
        S.dropdown = true
        for i, l in ipairs(S.langs) do if l == S.lang then S.ddl_idx = i end end
        render()
    elseif is_text_field(f) then
        do_name_search()
    elseif f == "btn_hash" then
        do_hash_search()
    elseif f == "btn_name" then
        do_name_search()
    elseif f == "list" then
        do_download()
    elseif f == "btn_help" then
        S.help = not S.help
        render()
    elseif f == "btn_config" then
        S.config = not S.config
        render()
    elseif f == "btn_download" then
        do_download()
    elseif f == "btn_close" then
        close()
    end
end

local function type_char(ch)
    local f = S.focus
    if not is_text_field(f) then return end
    if #S.fields[f] >= 80 then return end
    S.fields[f] = S.fields[f] .. ch
    render()
end

local function backspace()
    local f = S.focus
    if not is_text_field(f) then return end
    S.fields[f] = backspace_str(S.fields[f])
    render()
end

local function cycle_lang()
    if S.mode ~= "picker" or S.loading then return end
    if #S.langs < 2 then return end
    S.lang_idx = (S.lang_idx % #S.langs) + 1
    S.lang = S.langs[S.lang_idx]
    S.ddl_idx = S.lang_idx
    start_search(S.search_mode == "name" and "name" or "hash")
end

-- ---------------------------------------------------------------------------
-- open / close / bindings
-- ---------------------------------------------------------------------------

local bound = {}

local function bind(key, name, fn)
    mp.add_forced_key_binding(key, name, fn)
    bound[name] = true
end

-- printable keys: key name -> character (SPACE handled separately)
local PRINTABLE = {}
do
    for c in ("abcdefghijklmnopqrstuvwxyz"):gmatch(".") do PRINTABLE[c] = c end
    for c in ("ABCDEFGHIJKLMNOPQRSTUVWXYZ"):gmatch(".") do PRINTABLE[c] = c end
    for c in ("0123456789"):gmatch(".") do PRINTABLE[c] = c end
    for c in ("-_. ,;:'\"!?()[]{}<>/\\|@#$%^&*+=`~"):gmatch(".") do
        if c ~= " " then PRINTABLE[c] = c end  -- SPACE is bound explicitly
    end
end

local function install_bindings()
    -- text input
    local i = 0
    for key, ch in pairs(PRINTABLE) do
        i = i + 1
        bind(key, "submenu_ch_" .. i, function() type_char(ch) end)
    end
    bind("SPACE", "submenu_space", function()
        if is_text_field(S.focus) then type_char(" ") else activate() end
    end)
    -- this mpv build knows BS/DEL/NEXT (BACKSPACE/DELETE/PGDN are rejected)
    bind("BS", "submenu_backspace", backspace)
    bind("DEL", "submenu_delete", backspace)

    -- focus + activation
    bind("TAB", "submenu_tab", function() focus_step(1) end)
    bind("SHIFT+TAB", "submenu_shift_tab", function() focus_step(-1) end)
    bind("ENTER", "submenu_enter", activate)
    bind("KP_ENTER", "submenu_kpenter", activate)

    -- arrows
    bind("UP", "submenu_up", function() arrow(0, -1) end)
    bind("DOWN", "submenu_down", function() arrow(0, 1) end)
    bind("LEFT", "submenu_left", function() arrow(-1, 0) end)
    bind("RIGHT", "submenu_right", function() arrow(1, 0) end)

    -- shortcuts (letters also type when a text field is focused)
    local function sc(shortcut)
        return function()
            if is_text_field(S.focus) then
                type_char(shortcut)
                return
            end
            if S.help or S.config or S.dropdown then return end
            if shortcut == "j" then nav(1)
            elseif shortcut == "k" then nav(-1)
            elseif shortcut == "r" then do_hash_search()
            elseif shortcut == "n" then do_name_search()
            elseif shortcut == "l" then cycle_lang()
            elseif shortcut == "c" then
                if S.loading then
                    cancel_job()
                    set_loading(false)
                    S.error = { title = "Search cancelled" }
                    render()
                end
            end
        end
    end
    bind("j", "submenu_j", sc("j"))
    bind("k", "submenu_k", sc("k"))
    bind("r", "submenu_r", sc("r"))
    bind("n", "submenu_n", sc("n"))
    bind("l", "submenu_l", sc("l"))
    bind("c", "submenu_c", sc("c"))

    bind("g", "submenu_g", function()
        if is_text_field(S.focus) then type_char("g") return end
        if S.focus == "list" and #S.subs > 0 then S.sel, S.top = 1, 1 render() end
    end)
    bind("G", "submenu_G", function()
        if is_text_field(S.focus) then type_char("G") return end
        if S.focus == "list" and #S.subs > 0 then
            S.sel = #S.subs
            S.top = math.max(1, #S.subs - layout().nrows + 1)
            render()
        end
    end)
    bind("PGUP", "submenu_pgup", function()
        if is_text_field(S.focus) then return end
        if S.focus == "list" then page(-1) end
    end)
    bind("NEXT", "submenu_pgdn", function()
        if is_text_field(S.focus) then return end
        if S.focus == "list" then page(1) end
    end)

    bind("ESC", "submenu_esc", function()
        if S.dropdown then S.dropdown = false render()
        elseif S.help then S.help = false render()
        elseif S.config then S.config = false render()
        else close() end
    end)
    bind("q", "submenu_q", function()
        if is_text_field(S.focus) then type_char("q") return end
        close()
    end)

    -- mouse
    bind("MBTN_LEFT", "submenu_mleft", on_mouse_left)
    bind("MBTN_LEFT_DBL", "submenu_mdbl", function()
        local pos = mp.get_property_native("mouse-pos") or {}
        if pos.x and pos.y and row_at(pos.x, pos.y) then
            if S.focus ~= "list" then S.focus = "list" end
            do_download()
        end
    end)
    bind("MBTN_RIGHT", "submenu_mright", close)
    bind("WHEEL_UP", "submenu_wup", function() arrow(0, -1) end)
    bind("WHEEL_DOWN", "submenu_wdown", function() arrow(0, 1) end)
end

local function uninstall_bindings()
    for name in pairs(bound) do
        pcall(mp.remove_key_binding, name)
        bound[name] = nil
    end
end

row_at = function(px, py)
    local L = layout()
    if py < L.list_top or py > L.res_bottom - H * 0.010 then return nil end
    if px < L.x0 + L.gap or px > L.x0 + L.pw - L.gap then return nil end
    local idx = S.top + math.floor((py - L.list_top) / L.lh)
    if idx >= 1 and idx <= #S.subs then return idx end
    return nil
end

local function element_at(px, py)
    local L = layout()
    if not (px >= L.x0 and px <= L.x0 + L.pw and py >= L.y0 and py <= L.y0 + L.ph) then
        return nil
    end
    for _, b in ipairs(L.footer_btns) do
        if in_rect(px, py, b) then return b.id end
    end
    if in_rect(px, py, L.btn_hash) then return "btn_hash" end
    if in_rect(px, py, L.btn_name) then return "btn_name" end
    for _, id in ipairs({ "lang", "title", "season", "episode" }) do
        if in_rect(px, py, L.fields[id]) then return id end
    end
    if row_at(px, py) then return "list" end
    return nil
end

on_mouse_left = function()
    local pos = mp.get_property_native("mouse-pos") or {}
    if not (pos.x and pos.y) then return end
    local px, py = pos.x, pos.y

    if S.help or S.config then
        -- clicking outside the modal closes it
        local L = layout()
        local mw, mh = L.pw * 0.72, L.ph * 0.74
        local mx, my = L.x0 + (L.pw - mw) / 2, L.y0 + (L.ph - mh) / 2
        if not in_rect(px, py, { x = mx, y = my, w = mw, h = mh }) then
            S.help, S.config = false, false
            render()
        end
        return
    end

    if S.dropdown then
        local L = layout()
        local f = L.fields.lang
        local nv = math.min(#S.langs, 6)
        local ih = L.fs.row * 1.9
        if in_rect(px, py, { x = f.x, y = f.y + f.h + H * 0.004, w = f.w, h = nv * ih + H * 0.008 }) then
            local i = math.floor((py - (f.y + f.h + H * 0.004)) / ih) + 1
            if i >= 1 and i <= #S.langs then
                S.lang = S.langs[i]
                S.lang_idx = i
                S.ddl_idx = i
                S.dropdown = false
                render()
            end
        else
            S.dropdown = false
            render()
        end
        return
    end

    local id, inside = element_at(px, py)
    if not id then
        return -- click outside panel: ignore
    end
    if id == "lang" then
        if S.focus == "lang" then
            S.dropdown = true
            for i, l in ipairs(S.langs) do if l == S.lang then S.ddl_idx = i end end
        else
            set_focus("lang")
            return
        end
    elseif id == "list" then
        local idx = row_at(px, py)
        if idx then
            S.sel = idx
            S.focus = "list"
            local L = layout()
            if S.sel < S.top then S.top = S.sel end
            if S.sel >= S.top + L.nrows then S.top = S.sel - L.nrows + 1 end
            render()
        end
        return
    else
        set_focus(id)
        if id == "btn_hash" then do_hash_search()
        elseif id == "btn_name" then do_name_search()
        elseif id == "btn_help" then S.help = true render()
        elseif id == "btn_config" then S.config = true render()
        elseif id == "btn_download" then do_download()
        elseif id == "btn_close" then close() end
    end
    render()
end

-- hover highlight (fires on mouse movement)
mp.observe_property("mouse-pos", "native", function(_, pos)
    if S.mode ~= "picker" then return end
    if S.help or S.config or S.dropdown then return end
    if not (pos and pos.x and pos.y) then
        S.hover_row, S.hover_btn, S.hover_field = nil, nil, nil
        return
    end
    local L = layout()
    local hb, hf, hr = nil, nil, nil
    for _, b in ipairs(L.footer_btns) do
        if in_rect(pos.x, pos.y, b) then hb = b.id break end
    end
    if not hb then
        if in_rect(pos.x, pos.y, L.btn_hash) then hb = "btn_hash"
        elseif in_rect(pos.x, pos.y, L.btn_name) then hb = "btn_name" end
    end
    for _, id in ipairs({ "lang", "title", "season", "episode" }) do
        if in_rect(pos.x, pos.y, L.fields[id]) then hf = id break end
    end
    hr = row_at(pos.x, pos.y)
    if hb ~= S.hover_btn or hf ~= S.hover_field or hr ~= S.hover_row then
        S.hover_btn, S.hover_field, S.hover_row = hb, hf, hr
        if hr then S.sel = hr end
        render()
    end
end)

local function clean_title_from_path()
    local path = mp.get_property("path") or ""
    local base = path:match("[^/\\]+$") or path
    local stem = base:gsub("%.[^.]*$", "")
    local title, season, episode = stem, nil, nil
    local s, e = stem:match("[Ss](%d%d?)[Ee](%d%d?)")
    if not s then s, e = stem:match("(%d%d?)x(%d%d)") end
    if s and e then
        season, episode = s, e
        title = stem:gsub("[Ss]%d%d?[Ee]%d%d?", ""):gsub("(%d%d?)x%d%d", "")
    end
    title = title:gsub("[%._-]+", " "):gsub("%s+", " "):gsub("^%s+", ""):gsub("%s+$", "")
    title = title:gsub("^%[.-%]%s*", "")  -- drop leading [GROUP] tags
    return title, season, episode
end

local function open()
    if S.mode == "picker" then close() return end
    local path = mp.get_property("path")
    if not path or path == "" then
        mp.osd_message("Subtitle downloader: no file loaded", 2)
        return
    end
    -- fallback when osd-dimensions has not reported a sane size yet
    if not sane_dims({ w = W, h = H }) then
        local ws = mp.get_property_native("window-size")
        if sane_dims(ws) then
            W, H = ws.w, ws.h
        end
    end
    S.mode = "picker"
    S.langs = {}
    for lang in (o.languages .. ""):gmatch("[%a-_]+") do
        if lang ~= "" then S.langs[#S.langs + 1] = lang end
    end
    if #S.langs == 0 then S.langs = { "en" } end
    S.lang_idx = 1
    S.lang = S.langs[1]
    S.ddl_idx = 1
    local title, season, episode = clean_title_from_path()
    S.fields = { title = title, season = season or "", episode = episode or "" }
    S.subs, S.error, S.downloading = {}, nil, nil
    S.help, S.config = false, false
    S.hover_row, S.hover_btn, S.hover_field = nil, nil, nil
    S.focus = "list"
    S.status, S.status_color = "Ready — press Tab to edit the form", C.faint
    S.frame = 0
    install_bindings()
    if not S.timer then
        S.timer = mp.add_periodic_timer(0.1, function()
            S.frame = S.frame + 1
            if S.mode == "closed" then return end
            if S.loading or S.dropdown then
                render()
            elseif is_text_field(S.focus) then
                render() -- caret blink
            end
        end)
    end
    render()
    start_search("hash")
end

close = function()
    if S.mode == "closed" then return end
    cancel_job()
    set_loading(false)
    S.mode = "closed"
    uninstall_bindings()
    if S.timer then
        S.timer:stop()
        S.timer = nil
    end
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

msg.info("submenu loaded — press " .. o.key .. " to open the subtitle dialog")
