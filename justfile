# justfile for mpvsub — setup, test and build tasks
#
# Requires: just (https://github.com/casey/just), python3 3.12+ and the
# system GTK4/Libadwaita bindings (see requirements.txt). The venv is
# created with --system-site-packages so `import gi` works.

set quiet := true

venv   := ".venv"
python := venv + "/bin/python"
pip    := venv + "/bin/pip"

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

# Build the PyInstaller bundle into dist/mpvsub
build: setup
    {{pip}} install pyinstaller
    {{python}} -m PyInstaller --noconfirm packaging/mpvsub.spec

# Build + package the bundle as mpvsub-linux-x86_64.zip
bundle: build
    {{python}} -m zipfile -c mpvsub-linux-x86_64.zip dist/mpvsub

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
