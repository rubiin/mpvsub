# justfile for mpvsub — setup, test and build tasks
#
# Requires: just (https://github.com/casey/just), python3 3.12+ and the
# system GTK4/Libadwaita bindings (see requirements.txt). The venv is
# created with --system-site-packages so `import gi` works.
#
# PyInstaller cannot cross-compile: each platform's bundle must be built on
# that OS. `just build` / `just bundle` build for the current platform;
# the build-<platform> / bundle-<platform> recipes target one explicitly
# (run them on the matching machine). CI (build.yml) builds all three.

set quiet := true

venv     := ".venv"
python   := venv + "/bin/python"
pip      := venv + "/bin/pip"
platform := os()
# uname -m (not arch(), which uses Rust names) so bundle names match CI's
arch     := trim(`uname -m`)

# Show available recipes
default:
    just --list

# Create the venv (with system site packages) and install dependencies
setup:
    test -d {{venv}} || python3 -m venv --system-site-packages {{venv}}
    {{python}} -m pip install --upgrade pip
    {{pip}} install -r requirements.txt

# Run the logic + download-flow test suites
test:
    {{python}} tests/test_logic.py
    {{python}} tests/test_download.py

# Build the PyInstaller bundle (dist/mpvsub) for the current platform
build:
    @just build-{{platform}}

# Build the PyInstaller bundle on Linux (needs system GTK4 bindings)
build-linux: setup
    {{pip}} install pyinstaller
    {{python}} -m PyInstaller --noconfirm packaging/mpvsub.spec

# Build the PyInstaller bundle on macOS (needs `brew install gtk4 libadwaita pygobject3`)
build-macos: setup
    {{pip}} install pyinstaller
    {{python}} -m PyInstaller --noconfirm packaging/mpvsub.spec

# Build the PyInstaller bundle on Windows (MSYS2 MINGW64 python with GTK4)
build-windows:
    python -m pip install pyinstaller guessit
    python -m PyInstaller --noconfirm packaging/mpvsub.spec

# Build + zip the bundle for the current platform as mpvsub-<platform>-<arch>.zip
bundle:
    @just bundle-{{platform}}

# Build + zip on Linux
bundle-linux: build-linux
    {{python}} -m zipfile -c mpvsub-linux-{{arch}}.zip dist/mpvsub

# Build + zip on macOS
bundle-macos: build-macos
    {{python}} -m zipfile -c mpvsub-macos-{{arch}}.zip dist/mpvsub

# Build + zip on Windows
bundle-windows: build-windows
    python -m zipfile -c mpvsub-windows-{{arch}}.zip dist/mpvsub

# Run the app (extra args pass through; quote args with spaces: `just run "My Movie.mkv"`)
run *args:
    {{python}} main.py {{args}}

# Run the full installer (install.sh)
install:
    ./install.sh

# Remove build artifacts (build/, dist/, zips)
clean:
    rm -rf build dist *.zip

# Remove build artifacts and the venv
clean-all: clean
    rm -rf {{venv}}
