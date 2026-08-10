# mpv subtitle downloader — native GTK4 (Libadwaita)

A VLC-style subtitle downloader for **mpv**: a small native GTK4/Libadwaita
popup that searches **OpenSubtitles.com** with their modern REST API (the
same one the official VLSub extension uses), lets you pick a result and
saves it **next to the video file** (so mpv auto-loads it), or loads it
into the running mpv over its JSON IPC socket — no OSD rendering, no web
front-end, full keyboard navigation.

![VLC-style workflow: search → pick → download → auto-loaded](screenshot.png)

## Requirements

- Python 3.12+
- GTK4 + Libadwaita with PyGObject (system packages — see `requirements.txt`)
- mpv with `--input-ipc-server` support (the script sets it up for you)
- `guessit` (pip) — everything else is the Python standard library
- network access to `api.opensubtitles.com` (a free account is required)

## Install

```sh
# 1. Python backend (recommended: a venv, but any python with gi works)
python3 -m venv --system-site-packages .venv
.venv/bin/pip install -r requirements.txt

# 2. mpv script — copy both files to your mpv scripts folder
cp submenu-gtk.lua submenu-gtk.conf ~/.config/mpv/scripts/

# 3. point the script at main.py and the venv python
cat >> ~/.config/mpv/script-opts/submenu-gtk.conf <<EOF
app=/home/you/mpvui/main.py
python=/home/you/mpvui/.venv/bin/python3
EOF
```

Then press **CTRL+g** in mpv (or bind your own key in
`script-opts/submenu-gtk.conf`). The popup opens with a hash search for the
current file already running.

## Usage

The popup is a compact, fixed-size 700×500 window with the classic labelled
form — about five or six result rows are visible at once and the list
scrolls for the rest:

- **Subtitles language** — dropdown with the full OpenSubtitles language
  list (100+ languages, including `pt-br`, `zh-cn`, `zh-tw`, …); the last
  selection is remembered.
- **Search by hash** — finds subtitles for the exact file on disk via the
  OpenSubtitles movie hash; no typing needed. If the hash has no matches it
  automatically falls back to a name search (like VLSub).
- **Title / Search by name** — type a movie or series title (season/episode
  are auto-detected with guessit and can be edited in the Season and
  Episode fields), then press Enter or click **Search by name**.
- **IMDB ID** — search directly by IMDB id (`tt1375666`) instead of a title.
- **Sort by** — the OpenSubtitles API sort options: Best match (client-side
  scoring), Downloads, New downloads, Ratings, Votes, Upload date, Trusted
  uploader, HD, Release — plus an **↑ Asc / ↓ Desc** direction toggle.
  Sorting is applied server-side via `order_by`/`order_direction`.
- **Results list** — a scrollable list showing ~5-6 rows at first, one
  Subtitle Name column spanning the full width (with a **HI** badge for
  hearing-impaired releases). Rows highlight on hover;
  **double-click downloads immediately**.
- **Download selection** — saves the subtitle **next to the video file**
  when searching a local file (`<name>.<lang>.srt`, auto-loaded by mpv);
  name searches without a local file fall back to the download directory
  (`~/.local/share/mpv/subtitles/`). A file of the same name is overwritten
  (atomically — an interrupted download never corrupts an existing
  subtitle). Also loads it into mpv (`sub-add` + `sid`) and shows a
  **Subtitle loaded.** toast.
- **Show help / Show config** — key bindings and current settings.
- **Close** — closes the popup.

### Keys

| Key | Action |
|-----|--------|
| `↑` / `↓` | move through results |
| `Enter` | download the selected row |
| `Double-click` | download that row |
| `Ctrl+F` | focus the search entry |
| `Ctrl+R` | refresh search |
| `Esc` | close |

### Running from the command line

Launch the app directly from a terminal (use the venv python if you created
one during install):

```sh
.venv/bin/python3 main.py                              # search by name only
.venv/bin/python3 main.py /path/to/video.mkv           # hash-search a file
.venv/bin/python3 main.py --socket /tmp/mpv.sock       # connect to a running mpv
.venv/bin/python3 main.py --socket /tmp/mpv.sock --file /path/to/video.mkv
```

| Option | Meaning |
|--------|---------|
| `<video>` (positional) | video file to hash-search for subtitles |
| `--socket <path>` | mpv JSON IPC unix socket to connect to |
| `--file <path>` | video file (combined with `--socket`) |
| `--query <text>` | search a title by name on startup |
| `--debug` | verbose logging (search/download results) |
| `--width <px>` / `--height <px>` | override the window size |

When connected to mpv, the popup follows media changes automatically and
sub-adds downloads into the player. Without a socket it still works — files
are just saved to disk. For a quick launcher, drop an alias in your shell rc:

```sh
alias subs='/home/you/mpvui/.venv/bin/python3 /home/you/mpvui/main.py'
```

(replace `/home/you/mpvui` with your checkout path, then `subs` opens the
popup, `subs /path/to/video.mkv` hash-searches a file).

## Downloading (credentials)

The OpenSubtitles REST API needs an **Api-Key** on every request; this app
always uses the key shipped in the official VLSub extension. Every request
also needs an account: the app logs in with your **username/password** on
each start. Provide credentials as env vars:

```sh
export OPENSUBTITLES_USERNAME=you@example.com
export OPENSUBTITLES_PASSWORD=secret
```

or in `~/.config/mpvui-subtitles/settings.json`:

```json
{
  "username": "you@example.com",
  "password_obfuscated": "…"
}
```

The password is never written in plaintext: it is obfuscated with a
machine-local key (derived from `/etc/machine-id`) before saving, and
decoded again on load. Because the key is tied to the machine, copying
`settings.json` to another computer won't decode the password — just
re-enter it via the **Account…** button. A legacy plaintext `password`
entry is still read and migrated to `password_obfuscated` on the next save.

Env vars take precedence over the settings file. The **Account…** dialog
(also reachable from the bottom action bar) opens by itself on first run —
when there is no `settings.json` yet — and whenever a search fails because
credentials are missing or wrong. Saving valid credentials then re-runs the
failed search automatically.

## Configuration

The app stores its settings in
`~/.config/mpvui-subtitles/settings.json` (last languages, sort mode +
direction, download dir, encoding, window size, credentials). The download
directory is only used as a fallback for searches without a local video
file — subtitles for a file on disk are saved right next to it. Change it
there if you want the fallback elsewhere.

## Notes

- Only subtitles are ever fetched — never video content.
- Network streams (e.g. `ytdl://`) fall back to a name search using the
  media title.
- Search results are cached in memory for 10 minutes (keyed by query +
  sort mode); the login token is cached for 24 hours and re-fetched
  automatically on `401`.
- Transient download errors (`503` overloaded / `429` rate-limited) are
  retried automatically with a short backoff before an error is shown.
  OpenSubtitles' download service occasionally 503s for everyone — that is
  server-side and usually clears in minutes.

## Project layout

```
main.py                 entry point / CLI parsing
app.py                  Adw.Application (single instance)
models.py               dataclasses + OpenSubtitles language catalog
settings.py             persistent settings (JSON) + sort modes
cache.py                in-memory TTL search cache
search.py               guessit metadata, query building, scoring/sorting
opensubtitles_client.py OpenSubtitles REST API v1 facade (stdlib urllib)
moviehash.py            OpenSubtitles movie-hash computation
ipc.py                  mpv JSON IPC client (unix socket)
ui/                     GTK4 widgets (window, search bar, list, dialogs)
submenu-gtk.lua         mpv script: socket setup + launcher (CTRL+g)
submenu-gtk.conf        mpv script options
assets/                 icons
tests/                  logic + download-flow tests (python3 tests/test_logic.py; python3 tests/test_download.py)
```

The API client is ported from the official VLSub extension
([opensubtitles/vlsub-opensubtitles-com](https://github.com/opensubtitles/vlsub-opensubtitles-com)),
minus its i18n and VLC-UI code.
