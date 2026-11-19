"""Serve the reader UI in a normal browser against the real Api, for
checking the UI without the app window. Binds to 127.0.0.1 only.

    venv\\Scripts\\python reader\\devserver.py [port] [notes-folder]
    then open http://127.0.0.1:8765/?dev
"""
from __future__ import annotations

import json
import sys
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from reader.app import Api, static_dir  # noqa: E402
from reader.notes import default_notes_dir  # noqa: E402

PUBLIC = {"library", "signature", "open_note", "search", "update_note", "mark_read",
          "update_settings", "export_my_notes", "bulk_update", "bulk_read", "bulk_add_tag",
          "bulk_set"}


def main() -> None:
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8765
    folder = Path(sys.argv[2]) if len(sys.argv) > 2 else default_notes_dir()
    api = Api(folder)

    class Handler(SimpleHTTPRequestHandler):
        def __init__(self, *a, **kw):
            super().__init__(*a, directory=str(static_dir()), **kw)

        def do_POST(self):
            name = self.path.removeprefix("/api/")
            if name not in PUBLIC:
                self.send_error(404)
                return
            args = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"[]")
            out = json.dumps(getattr(api, name)(*args)).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(out)))
            self.end_headers()
            self.wfile.write(out)

        def log_message(self, *a):
            pass

    print(f"serving {folder} on http://127.0.0.1:{port}/?dev", flush=True)
    ThreadingHTTPServer(("127.0.0.1", port), Handler).serve_forever()


if __name__ == "__main__":
    main()
