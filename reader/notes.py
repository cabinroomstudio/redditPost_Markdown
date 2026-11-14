"""Library of reddit-md notes plus the reader's personal state.

Notes are the .md files reddit-md writes into its output folder. Older
versions live in <folder>/_versions as <stem>.vN.md. Personal state (read,
starred, own notes, tags, reading position, settings) is kept in
<folder>/.reader-state.json so it travels and backs up with the notes.
"""
from __future__ import annotations

import json
import os
import re
import tempfile
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import markdown as md_lib

STATE_NAME = ".reader-state.json"
VERSIONS_DIR = "_versions"
WORDS_PER_MINUTE = 230

_FRONT_RE = re.compile(r"\A---\r?\n(.*?)\r?\n---\r?\n", re.S)
_VERSION_RE = re.compile(r"\.v(\d+)\.md$")


def default_notes_dir() -> Path:
    env = os.environ.get("REDDIT_NOTES_DIR")
    return Path(env).expanduser() if env else Path.home() / "Documents" / "reddit-notes"


def parse_front_matter(text: str) -> tuple[dict, str]:
    """Split the simple YAML header reddit-md writes from the body."""
    m = _FRONT_RE.match(text)
    if not m:
        return {}, text
    meta: dict = {}
    for line in m.group(1).splitlines():
        key, sep, value = line.partition(":")
        if not sep:
            continue
        key, value = key.strip(), value.strip()
        if len(value) >= 2 and value[0] == value[-1] == '"':
            value = value[1:-1].replace('\\"', '"').replace("\\\\", "\\")
        elif value.startswith("[") and value.endswith("]"):
            value = [v.strip() for v in value[1:-1].split(",") if v.strip()]
        else:
            for cast in (int, float):
                try:
                    value = cast(value)
                    break
                except ValueError:
                    pass
            if value == "true":
                value = True
        meta[key] = value
    return meta, text[m.end():]


def _post_text(body: str) -> str:
    """The post itself: the body before the comments section."""
    return body.split("\n## Comments (", 1)[0]


def _preview(body: str, limit: int = 220) -> str:
    lines = _post_text(body).splitlines()
    # skip the "# title" line and the "[r/sub](...) · u/x" meta line
    text_lines = [l for l in lines[1:] if l.strip() and not l.startswith("[r/")]
    text = " ".join(text_lines)
    text = re.sub(r"!?\[([^\]]*)\]\([^)]*\)", r"\1", text)
    text = re.sub(r"[*_`>#\\]+", "", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:limit] + ("…" if len(text) > limit else "")


def render_html(body: str) -> str:
    """Markdown to HTML. Raw HTML in the source is escaped, not passed
    through, since note text comes from strangers on Reddit; the UI also
    sanitises with an allowlist before inserting."""
    safe = re.sub(r"<(?!https?://[^\s<>]+>)", "&lt;", body)
    html = md_lib.markdown(safe, extensions=["extra", "sane_lists"], output_format="html")
    # inside code spans markdown re-escapes our &lt; to &amp;lt;
    return html.replace("&amp;lt;", "&lt;")


@dataclass
class Note:
    id: str            # post_id, stable across re-captures
    path: Path
    meta: dict
    body: str
    mtime: float

    @property
    def words_total(self) -> int:
        return len(self.body.split())

    def summary(self) -> dict:
        m = self.meta
        post_words = len(_post_text(self.body).split())
        title = m.get("title")
        if not title:  # a hand-written note with no front matter
            first = re.search(r"^#\s+(.+)$", self.body, re.M)
            title = first.group(1).strip() if first else self.path.stem
        captured = m.get("captured") or datetime.fromtimestamp(self.mtime).astimezone().isoformat(timespec="minutes")
        return {
            "id": self.id,
            "file": self.path.name,
            "title": title,
            "subreddit": m.get("subreddit", ""),
            "author": m.get("author", ""),
            "url": m.get("url", ""),
            "posted": str(m.get("posted", "")),
            "captured": str(captured),
            "version": m.get("version", 1),
            "score": m.get("post_score", 0),
            "ratio": m.get("upvote_ratio", 0),
            "comments": m.get("comments_kept", 0),
            "comments_reported": m.get("comments_reported", 0),
            "post_words": post_words,
            "words": self.words_total,
            "minutes": max(1, round(self.words_total / WORDS_PER_MINUTE)),
            "removed": bool(m.get("removed_by_moderator", False)),
            "preview": _preview(self.body),
            "mtime": self.mtime,
        }


class Library:
    def __init__(self, folder: Path):
        self.folder = Path(folder)
        self._cache: dict[Path, tuple[float, Note]] = {}

    def _load(self, path: Path) -> Note | None:
        try:
            mtime = path.stat().st_mtime
            cached = self._cache.get(path)
            if cached and cached[0] == mtime:
                return cached[1]
            meta, body = parse_front_matter(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError):
            return None
        note = Note(str(meta.get("post_id") or path.stem), path, meta, body, mtime)
        self._cache[path] = (mtime, note)
        return note

    def notes(self) -> tuple[list[Note], list[str]]:
        """All current notes, plus the names of files that could not be read."""
        if not self.folder.is_dir():
            return [], []
        out, skipped = [], []
        for path in sorted(self.folder.glob("*.md")):
            note = self._load(path)
            if note is None:
                skipped.append(path.name)
            else:
                out.append(note)
        return out, skipped

    def get(self, note_id: str) -> Note | None:
        for note in self.notes()[0]:
            if note.id == note_id:
                return note
        return None

    def versions(self, note: Note) -> list[dict]:
        vdir = self.folder / VERSIONS_DIR
        found = []
        if vdir.is_dir():
            for path in vdir.glob(f"{note.path.stem}.v*.md"):
                m = _VERSION_RE.search(path.name)
                if m:
                    found.append({"version": int(m.group(1)), "file": path.name})
        found.sort(key=lambda v: v["version"])
        return found

    def version_body(self, note: Note, file_name: str) -> str | None:
        path = (self.folder / VERSIONS_DIR / file_name)
        if path.parent.resolve() != (self.folder / VERSIONS_DIR).resolve():
            return None
        if not path.name.startswith(note.path.stem + ".v") or not path.is_file():
            return None
        return parse_front_matter(path.read_text(encoding="utf-8"))[1]

    def search(self, query: str) -> list[str]:
        terms = [t for t in query.lower().split() if t]
        if not terms:
            return []
        hits = []
        for note in self.notes()[0]:
            hay = (str(note.meta.get("title", "")) + "\n" + note.body).lower()
            if all(t in hay for t in terms):
                hits.append(note.id)
        return hits


DEFAULT_SETTINGS = {
    "theme": "auto",       # auto | light | dark | sepia
    "font": "serif",       # serif | sans
    "size": 18,
    "width": 720,
    "sort": "captured",
    "collapse_replies": False,
}


@dataclass
class State:
    path: Path
    data: dict = field(default_factory=dict)

    @classmethod
    def load(cls, folder: Path) -> "State":
        path = Path(folder) / STATE_NAME
        data: dict = {}
        if path.is_file():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                # keep the unreadable file rather than overwrite it
                path.replace(path.with_suffix(f".broken-{int(time.time())}.json"))
                data = {}
        data.setdefault("notes", {})
        data["settings"] = {**DEFAULT_SETTINGS, **data.get("settings", {})}
        return cls(path, data)

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=self.path.parent, prefix=".reader-", suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(self.data, f, indent=1, ensure_ascii=False)
        os.replace(tmp, self.path)

    def note(self, note_id: str) -> dict:
        return self.data["notes"].get(note_id, {})

    def update_note(self, note_id: str, changes: dict) -> dict:
        allowed = {"read_at", "starred", "my_note", "tags", "pos", "opened_at", "archived"}
        entry = self.data["notes"].setdefault(note_id, {})
        for key, value in changes.items():
            if key not in allowed:
                continue
            if value is None:
                entry.pop(key, None)
            else:
                entry[key] = value
        if not entry:
            self.data["notes"].pop(note_id, None)
        self.save()
        return entry

    def update_settings(self, changes: dict) -> dict:
        s = self.data["settings"]
        for key, value in changes.items():
            if key in DEFAULT_SETTINGS:
                s[key] = value
        self.save()
        return s
