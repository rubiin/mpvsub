#!/usr/bin/env bash
#
# install.sh — one-shot installer for the mpv subtitle downloader.
#
# Remote install:
#
#     curl -fsSL https://raw.githubusercontent.com/rubiin/mpvsub/master/install.sh | bash
#
# Local install (from a git clone / checkout of this repo):
#
#     bash install.sh
#
# What it does:
#   1. fetches the source — clones $REPO_URL, or uses the current directory
#      when run from a checkout (local mode)
#   2. creates a venv with --system-site-packages (PyGObject/GTK4 come from
#      the system), installs the pip dependency (guessit)
#   3. copies mpvsub.lua into ~/.config/mpv/scripts/
#   4. writes ~/.config/mpv/script-opts/mpvsub.conf pointing at the
#      installed main.py + venv python
#
# Environment overrides:
#     MPVSUB_REPO_URL  git URL to clone           (default: REPO_URL below)
#     MPVSUB_DIR       install directory          (default: ~/mpvsub)
#     XDG_CONFIG_HOME mpv config root            (default: ~/.config)
set -euo pipefail

REPO_URL="${MPVSUB_REPO_URL:-https://github.com/rubiin/mpvsub}"
BRANCH="master"
INSTALL_DIR="${MPVSUB_DIR:-$HOME/mpvsub}"
MPV_CONFIG_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/mpv"

say() { printf '\033[1;36m==>\033[0m %s\n' "$*"; }
die() { printf '\033[1;31mError:\033[0m %s\n' "$*" >&2; exit 1; }

distro_hint() {
    case "${1:-}" in
        debian|ubuntu|linuxmint|pop)
            echo "  sudo apt install python3-venv python3-gi python3-gi-cairo gir1.2-gtk-4.0 gir1.2-adw-1" ;;
        fedora|centos|rhel)
            echo "  sudo dnf install python3 python3-gobject gtk4 libadwaita" ;;
        arch|manjaro|endeavouros)
            echo "  sudo pacman -S python python-gobject gtk4 libadwaita" ;;
        *)
            echo "  install Python 3.12+, GTK4 and libadwaita with PyGObject for your distro" ;;
    esac
}

# --- prerequisites ----------------------------------------------------------

PYTHON="$(command -v python3 || true)"
if [ -z "$PYTHON" ]; then
    die "python3 is required (3.12+). Install it, then re-run."
fi
if ! "$PYTHON" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 12) else 1)'; then
    die "python3 3.12+ is required; found $("$PYTHON" --version 2>&1)."
fi

if ! "$PYTHON" - <<'PY' >/dev/null 2>&1
import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gtk
PY
then
    say "Missing the GTK4/Libadwaita Python bindings (gi). Install them first:"
    ID="$(sed -n 's/^ID=//p' /etc/os-release 2>/dev/null | tr -d '"')"
    distro_hint "$ID"
    die "re-run this script after installing the packages above"
fi

# --- source -----------------------------------------------------------------

if [ -f ./main.py ] && [ -f ./mpvsub.lua ] && [ -f ./requirements.txt ]; then
    SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
    say "Installing from local checkout: $SRC_DIR"
elif [ -d "$INSTALL_DIR/.git" ]; then
    say "Updating existing install at $INSTALL_DIR ..."
    git -C "$INSTALL_DIR" pull --ff-only
    SRC_DIR="$INSTALL_DIR"
else
    if ! command -v git >/dev/null 2>&1; then
        die "git is required to clone the repo — install it and re-run."
    fi
    say "Cloning $REPO_URL (branch $BRANCH) into $INSTALL_DIR ..."
    mkdir -p "$(dirname "$INSTALL_DIR")"
    if ! git clone --depth 1 --branch "$BRANCH" "$REPO_URL" "$INSTALL_DIR"; then
        die "git clone failed — is REPO_URL correct? (override it with MPVSUB_REPO_URL)"
    fi
    SRC_DIR="$INSTALL_DIR"
fi

if ! command -v mpv >/dev/null 2>&1; then
    say "WARNING: mpv not found — install it to use the CTRL+g launcher."
fi

# --- venv + dependencies ----------------------------------------------------

say "Creating venv ($INSTALL_DIR/.venv) ..."
"$PYTHON" -m venv --system-site-packages "$INSTALL_DIR/.venv"
VENV_PYTHON="$INSTALL_DIR/.venv/bin/python3"
"$VENV_PYTHON" -m pip install --quiet --upgrade pip
"$VENV_PYTHON" -m pip install --quiet -r "$SRC_DIR/requirements.txt"

# --- mpv script + config ----------------------------------------------------

say "Installing the mpv script ..."
mkdir -p "$MPV_CONFIG_DIR/scripts" "$MPV_CONFIG_DIR/script-opts"
cp "$SRC_DIR/mpvsub.lua" "$MPV_CONFIG_DIR/scripts/mpvsub.lua"

CONF="$MPV_CONFIG_DIR/script-opts/mpvsub.conf"
if [ -f "$CONF" ] && ! grep -q "written by install.sh" "$CONF"; then
    cp "$CONF" "$CONF.bak"
    say "Backed up your existing $CONF to $CONF.bak"
fi
cat > "$CONF" <<EOF
# Written by install.sh — edit key / extra_args to taste.
key=CTRL+g
python=$VENV_PYTHON
app=$SRC_DIR/main.py
EOF

# --- done -------------------------------------------------------------------

cat <<EOF

Installed ✓
  App:         $SRC_DIR
  Venv:        $INSTALL_DIR/.venv
  mpv script:  $MPV_CONFIG_DIR/scripts/mpvsub.lua
  Config:      $CONF

Usage:
  • in mpv, press CTRL+g  (restart mpv first if it was already running)
  • standalone: $VENV_PYTHON $SRC_DIR/main.py

On first use the Account… dialog opens — enter your OpenSubtitles
username/password.
EOF
