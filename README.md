# mpv "sub" — VLC-style subtitle downloader UI

An in-player subtitle downloader for mpv: a full OSD picker, similar to
VLC's "Subtitle downloader", listing downloadable subtitles for the
currently playing video — language, release, provider, format and download
count. Pick one, press Enter, it is downloaded next to the video and
selected automatically.

![Subtitle downloader picker](screenshot.png)

## Requirements

- mpv >= 0.33 (OSD overlays, async subprocess, `osd-dimensions`)
- Python 3 with [subliminal] installed (e.g. `pipx install subliminal`)
- network access to the subtitle providers

## Files

| File            | Role                                                        |
|-----------------|-------------------------------------------------------------|
| `submenu.lua`   | mpv script: the OSD picker UI + navigation (CTRL+s)         |
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

- **`CTRL+s`** (default) — open / close the subtitle downloader.
- While open:
  - `↑`/`↓` or `j`/`k` — move selection · `PgUp`/`PgDn` — page
  - `Enter`/`Space` — download the selected subtitle
  - `r` — refresh search · `l` — cycle language · `c` — cancel search
  - `g`/`G` — first/last · `Esc`/`q` — close
  - Mouse: hover to select, double-click to download, wheel to scroll,
    right-click to close.

The downloaded file is saved next to the video (`download_dir=` to change),
rescanned, and selected — playback continues with the new subtitle active.

## Configuration

All options go in `~/.config/mpv/script-opts/submenu.conf` (see the example
`submenu.conf` in this repo). Highlights:

| Option          | Default                                  | Meaning                        |
|-----------------|------------------------------------------|--------------------------------|
| `key`           | `CTRL+s`                                 | open the picker                |
| `languages`     | `en`                                     | comma separated IETF codes     |
| `providers`     | `opensubtitlescom,podnapisi,subtis,tvsubtitles` | subliminal providers |
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
