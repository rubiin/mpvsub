"""PyInstaller runtime hook for mpvsub.

Runs inside the frozen bundle before the app imports ``gi``. Points the
introspection loader at the bundled typelibs (Windows) and makes the
bundled GTK DLLs, schemas and icon theme discoverable. On Linux the
system GTK4/libadwaita is used, so these lookups are harmless no-ops.
"""

import os
import sys


def _prepend_env(key: str, value: str) -> None:
    parts = [value]
    current = os.environ.get(key)
    if current:
        parts.append(current)
    os.environ[key] = os.pathsep.join(parts)


def _setup() -> None:
    if not getattr(sys, "frozen", False):
        return
    # one-dir bundle: everything lives under _MEIPASS
    base = getattr(sys, "_MEIPASS", os.path.dirname(sys.executable))
    if not os.path.isdir(base):
        return

    if os.name == "nt":
        try:
            os.add_dll_directory(base)  # type: ignore[attr-defined]
        except (AttributeError, OSError):
            pass
        _prepend_env("PATH", base)

    typelibs = os.path.join(base, "gi_typelibs")
    if os.path.isdir(typelibs):
        _prepend_env("GI_TYPELIB_PATH", typelibs)

    # XDG_DATA_DIRS lets GSettings find the compiled schemas and GTK find
    # the icon theme inside the bundle
    if os.path.isdir(os.path.join(base, "share")):
        _prepend_env("XDG_DATA_DIRS", base)

    loaders_root = os.path.join(base, "lib", "gdk-pixbuf-2.0")
    if os.path.isdir(loaders_root):
        _prepend_env("PATH", loaders_root)
        # the loader dir is versioned (e.g. 2.10.0) — pick whatever is bundled
        for name in sorted(os.listdir(loaders_root)):
            cache = os.path.join(loaders_root, name, "loaders", "loaders.cache")
            if os.path.isfile(cache):
                os.environ.setdefault("GDK_PIXBUF_MODULE_FILE", cache)
                break


_setup()
