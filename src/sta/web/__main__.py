"""Module entry point: ``python -m sta.web`` (serves http://127.0.0.1:8000)."""

from __future__ import annotations

import os

from .app import serve

HOST = os.environ.get("STA_HOST", "127.0.0.1")
PORT = int(os.environ.get("STA_PORT", "8000"))

if __name__ == "__main__":
    serve(host=HOST, port=PORT)
