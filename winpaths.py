"""Where Windows really keeps the user's Documents folder.

`~/Documents` is wrong once OneDrive backup (or a manual move) relocates
Documents: the bot then wrote new notes into a fresh, empty folder and the
reader lost sight of every older note. Ask the shell for the known folder.
"""
from __future__ import annotations

import sys
from pathlib import Path

# FOLDERID_Documents {FDD39AD0-238F-46AF-ADB4-6C85480369C7}
_DOCUMENTS_GUID = (0xFDD39AD0, 0x238F, 0x46AF, (0xAD, 0xB4, 0x6C, 0x85, 0x48, 0x03, 0x69, 0xC7))


def _known_documents() -> Path | None:
    if sys.platform != "win32":
        return None
    import ctypes
    from ctypes import wintypes

    class GUID(ctypes.Structure):
        _fields_ = [("Data1", wintypes.DWORD), ("Data2", wintypes.WORD),
                    ("Data3", wintypes.WORD), ("Data4", ctypes.c_ubyte * 8)]

    d1, d2, d3, d4 = _DOCUMENTS_GUID
    guid = GUID(d1, d2, d3, (ctypes.c_ubyte * 8)(*d4))
    out = ctypes.c_wchar_p()
    try:
        hr = ctypes.windll.shell32.SHGetKnownFolderPath(ctypes.byref(guid), 0, None, ctypes.byref(out))
        if hr != 0 or not out.value:
            return None
        return Path(out.value)
    except OSError:
        return None
    finally:
        if out:
            ctypes.windll.ole32.CoTaskMemFree(out)


def documents_dir() -> Path:
    return _known_documents() or Path.home() / "Documents"


def default_notes_dir() -> Path:
    return documents_dir() / "reddit-notes"
