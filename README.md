# mpv "sub" — VLC-style subtitle downloader UI

An in-player subtitle downloader for mpv: a full OSD dialog, similar to
VLC's "Subtitle downloader", with a search form (language dropdown, title /
season / episode fields), "Search by hash" and "Search by name" actions, a
scrollable results list, a status bar and footer buttons. Pick a subtitle,
download it next to the video, and it is selected automatically.

![Subtitle downloader dialog](screenshot.png)

## Requirements

- mpv >= 0.33 (OSD overlays, async subprocess, `osd-dimensions`)
- Python 3 with [subliminal] installed (e.g. `pipx install subliminal`)
- network access to the subtitle providers

## Files

| File            | Role                                                        |
|-----------------|-------------------------------------------------------------|
| `submenu.lua`   | mpv script: the OSD dialog UI + navigation (CTRL+s)         |
| `sub_helper.py` | Python backend: searches & downloads via [subliminal]       |
| `submenu.conf`  | Example options (see `script-opts/` below)                  |

## Install

1. Copy both `submenu.lua` and `sub_helper.py` into
   `~/.config/mpv/scripts/` (the helper must sit *next to* the script, or
   point to it with `helper=` in `script-opts/submenu.conf`).
2. Install the backend:

   ```sh
   pipx install subliminal
   ```

   Any Python with subliminal on `PATH` works; `sub_helper.py` automatically
   re-executes itself with the interpreter that owns the `subliminal`
   executable, so it also works when the system python3 can't import it.
3. (Optional) Copy `submenu.conf` to `~/.config/mpv/script-opts/` and tweak.

## Usage

**`CTRL+s`** (default) — open / close the subtitle dialog. The dialog opens
with a hash search for the current file already running; the Title field is
pre-filled from the filename (season/episode are detected from `S01E02`-style
names).

### Searching

- **Search by hash** — finds subtitles for the exact file (movie or episode)
  via OpenSubtitles-compatible hashes. No typing needed.
- **Search by name** — fill in the **Title** field (plus optional
  **Season (series)** and **Episode (series)** for TV shows) and press
  Enter or click the button.

### Keys

| Key | Action |
|-----|--------|
| `Tab` / `Shift+Tab` | move focus between controls |
| `↑`/`↓` `←`/`→` | navigate (form rows, list, buttons, dropdown) |
| `Enter` | activate the focused control / download the selection |
| `Space` | activate (types a space inside a text field) |
| letters / digits / symbols | type into the focused text field |
| `Backspace` | delete previous character |
| `r` / `n` | search by hash / by name |
| `l` | cycle language |
| `c` | cancel the running search |
| `g` / `G` | first / last result |
| `PgUp` / `PgDn` | page through results |
| `Esc` | close (first closes popup / help / config) |

Mouse: hover highlights rows and buttons, click focuses or activates,
double-click downloads, wheel scrolls, right-click closes.

The downloaded file is saved next to the video (`download_dir=` to change),
rescanned, and selected — playback continues with the new subtitle active.

## Configuration

All options go in `~/.config/mpv/script-opts/submenu.conf` (see the example
`submenu.conf` in this repo). Highlights:

| Option          | Default                                  | Meaning                        |
|-----------------|------------------------------------------|--------------------------------|
| `key`           | `CTRL+s`                                 | open the dialog                |
| `languages`     | `en`                                     | comma separated IETF codes     |
| `providers`     | `opensubtitlescom,podnapisi,subtis,tvsubtitles` | subliminal providers  |
| `download_dir`  | (video dir)                              | where subtitles are saved      |
| `accent`/`bg`   | `E6A23C` / `0F1115`                      | UI colors (RRGGBB)             |

## Downloading (credentials)

Searching works anonymously. **Downloading from OpenSubtitles.com requires
a free account.** Provide credentials either as env vars:

```sh
export SUBLIMINAL_PROVIDER_OPENSUBTITLESCOM_USERNAME=you@example.com
export SUBLIMINAL_PROVIDER_OPENSUBTITLESCOM_PASSWORD=secret
```

or in `~/.config/subliminal/subliminal.toml`:

```toml
[provider.opensubtitlescom]
username = "you@example.com"
password = "secret"
```

Other providers (`podnapisi`, `subtis`, `tvsubtitles`, …) need no account.

## Notes

- Only local files are searchable (network streams like `ytdl://` are
  skipped).
- Only subtitles are fetched — never the video itself.

[subliminal]: https://github.com/Diaoul/subliminal
