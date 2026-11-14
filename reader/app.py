"""Reddit Reader: a desktop reader for the notes reddit-md captures.

Run from source:  venv\\Scripts\\python -m reader.app
Build the exe:    venv\\Scripts\\python reader\\build.py
"""
from __future__ import annotations

import os
import subprocess
import sys
import time
import webbrowser
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from reader.notes import Library, State, default_notes_dir, render_html
else:
    from .notes import Library, State, default_notes_dir, render_html


def static_dir() -> Path:
    base = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
    return base / "static" if (base / "static").is_dir() else base / "reader" / "static"


class Api:
    def __init__(self, folder: Path):
        self.folder = Path(folder)
        self.lib = Library(self.folder)
        self.state = State.load(self.folder)

    # ----- library
    def library(self) -> dict:
        notes, skipped = self.lib.notes()
        items = []
        for n in notes:
            s = n.summary()
            st = self.state.note(n.id)
            s["state"] = st
            s["updated"] = bool(st.get("read_at")) and n.mtime > st["read_at"] + 1
            items.append(s)
        return {
            "folder": str(self.folder),
            "exists": self.folder.is_dir(),
            "notes": items,
            "skipped": skipped,
            "settings": self.state.data["settings"],
        }

    def signature(self) -> str:
        """Cheap change check so the UI can refresh when reddit-md writes."""
        if not self.folder.is_dir():
            return ""
        parts = [f"{p.name}:{p.stat().st_mtime_ns}" for p in sorted(self.folder.glob("*.md"))]
        return str(hash("|".join(parts)))

    def open_note(self, note_id: str, version_file: str | None = None) -> dict:
        note = self.lib.get(note_id)
        if note is None:
            return {"error": "That note is no longer in the folder."}
        body = note.body
        if version_file:
            body = self.lib.version_body(note, version_file)
            if body is None:
                return {"error": "That version could not be found."}
        else:
            self.state.update_note(note_id, {"opened_at": time.time()})
        return {
            "summary": note.summary(),
            "html": render_html(body),
            "versions": self.lib.versions(note),
            "viewing": version_file,
            "state": self.state.note(note_id),
        }

    def search(self, query: str) -> list[str]:
        return self.lib.search(query)

    # ----- personal state
    def update_note(self, note_id: str, changes: dict) -> dict:
        return self.state.update_note(note_id, changes)

    def mark_read(self, note_id: str, read: bool) -> dict:
        return self.state.update_note(note_id, {"read_at": time.time() if read else None})

    def update_settings(self, changes: dict) -> dict:
        return self.state.update_settings(changes)

    def export_my_notes(self) -> str:
        """Write every personal note to one markdown file and return its path."""
        notes = {n.id: n for n in self.lib.notes()[0]}
        lines = ["# My notes on saved Reddit threads", ""]
        count = 0
        for nid, st in self.state.data["notes"].items():
            text = (st.get("my_note") or "").strip()
            tags = st.get("tags") or []
            if not text and not tags:
                continue
            n = notes.get(nid)
            title = n.meta.get("title", nid) if n else nid
            url = n.meta.get("url", "") if n else ""
            lines += [f"## {title}", ""]
            if url:
                lines += [url, ""]
            if tags:
                lines += ["Tags: " + ", ".join(tags), ""]
            if text:
                lines += [text, ""]
            count += 1
        out = self.folder / "_my-notes.md"
        out.write_text("\n".join(lines), encoding="utf-8")
        self.reveal(str(out))
        return f"{count} notes exported to {out}"

    # ----- outside world
    def open_url(self, url: str) -> None:
        if url.startswith(("https://", "http://")):
            webbrowser.open(url)

    def reveal(self, path_or_id: str) -> None:
        note = self.lib.get(path_or_id)
        path = note.path if note else Path(path_or_id)
        if path.exists():
            subprocess.Popen(["explorer", "/select,", str(path)])

    def open_folder(self) -> None:
        if self.folder.is_dir():
            os.startfile(self.folder)


def main() -> None:
    import webview

    folder = default_notes_dir()
    api = Api(folder)
    webview.create_window(
        "Reddit Reader",
        str(static_dir() / "index.html"),
        js_api=api,
        width=1400,
        height=900,
        min_size=(820, 560),
        background_color="#1b1a18",
    )
    webview.start()


if __name__ == "__main__":
    main()
