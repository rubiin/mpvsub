# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for mpvsub — builds the Linux and Windows bundles.

Run from the repo root inside a venv that can import PyGObject + guessit:

    pyinstaller --noconfirm packaging/mpvsub.spec

* Linux:  system GTK4/libadwaita (gi bindings from the distro) is used at
          runtime; the bundle contains Python + the app + gi typelibs.
* Windows: the bundle is self-contained — GTK4, libadwaita, the Adwaita
          icon theme, glib schemas and gdk-pixbuf loaders are copied from
          the MSYS2 MINGW64 prefix (set MSYSTEM_PREFIX or C:\\msys64\\mingw64).
          packaging/rthook_mpvsub.py points gi at the bundled files.
"""

import os
import sys

from PyInstaller.utils.hooks import collect_all

#: repo root (the directory above this spec file)
ROOT = os.path.abspath(os.path.join(SPECPATH, os.pardir))

#: path to the app entry point, relative to the repo root
APP = os.path.join(ROOT, "main.py")

datas = [(os.path.join(ROOT, "assets"), "assets")]
binaries = []
hiddenimports = []

# gi + all gi.repository.* namespaces (Gtk, Adw, Gdk, GdkPixbuf, ...)
_gi_datas, _gi_binaries, _gi_hidden = collect_all("gi")
datas += _gi_datas
binaries += _gi_binaries
hiddenimports += _gi_hidden

if sys.platform == "win32":
    mingw = os.environ.get("MSYSTEM_PREFIX", r"C:\msys64\mingw64")
    bin_dir = os.path.join(mingw, "bin")
    # GTK4 + libadwaita entry points; PyInstaller pulls the rest of the
    # stack (pango, cairo, harfbuzz, glib, ...) via import-table analysis
    # because /mingw64/bin is on PATH while the spec is analysed.
    for dll in ("libgtk-4-1.dll", "libadwaita-1-0.dll", "libgirepository-1_0-1.dll"):
        path = os.path.join(bin_dir, dll)
        if os.path.exists(path):
            binaries.append((path, "."))
    datas += [
        # GObject introspection typelibs
        (os.path.join(mingw, "lib", "girepository-1.0"), "gi_typelibs"),
        # GSettings schemas (libadwaita's org.gtk.libadwaita is in here)
        (os.path.join(mingw, "share", "glib-2.0", "schemas"), "share/glib-2.0/schemas"),
        # icon theme used by the status pages and toolbar icons
        (os.path.join(mingw, "share", "icons", "Adwaita"), "share/icons/Adwaita"),
        # gdk-pixbuf loaders (svg/png icons)
        (os.path.join(mingw, "lib", "gdk-pixbuf-2.0"), "lib/gdk-pixbuf-2.0"),
    ]

a = Analysis(
    [APP],
    pathex=[ROOT],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[os.path.join(SPECPATH, "rthook_mpvsub.py")],
    excludes=[],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="mpvsub",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="mpvsub",
)
