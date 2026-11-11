"""Session persistence: one JSON file per conversation, stored under the home dir."""

from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path

SESSION_VERSION = 1


def session_dir(root, base_dir=None) -> Path:
    key = hashlib.sha1(str(Path(root).resolve()).encode("utf-8")).hexdigest()[:12]
    base = Path(base_dir) if base_dir else Path.home() / ".aider-agent"
    return base / "sessions" / key


class SessionStore:
    _counter = 0

    def __init__(self, root, path=None, base_dir=None):
        self.root = str(Path(root).resolve())
        self.directory = session_dir(root, base_dir)
        self.path = Path(path) if path else None

    def save(self, blocks, model=None, extra=None) -> Path:
        self.directory.mkdir(parents=True, exist_ok=True)
        if self.path is None:
            now = time.time_ns()
            stamp = (
                time.strftime("%Y%m%d-%H%M%S", time.localtime(now // 1_000_000_000))
                + f"-{now % 1_000_000_000:09d}"
            )
            self.path = self.directory / f"{stamp}.json"
            while self.path.exists():
                SessionStore._counter += 1
                self.path = self.directory / f"{stamp}-{SessionStore._counter:03d}.json"
        payload = {
            "version": SESSION_VERSION,
            "root": self.root,
            "model": model,
            "saved_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "blocks": blocks,
        }
        if extra:
            payload.update(extra)
        self.path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        return self.path

    def latest(self):
        if not self.directory.exists():
            return None
        files = sorted(self.directory.glob("*.json"), key=lambda p: p.name)
        return files[-1] if files else None

    def load(self, path=None):
        target = Path(path) if path else self.latest()
        if target is None or not target.exists():
            return None
        try:
            payload = json.loads(target.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        blocks = payload.get("blocks")
        if not isinstance(blocks, list) or not _blocks_ok(blocks):
            return None
        payload["_path"] = str(target)
        return payload


def _blocks_ok(blocks) -> bool:
    for block in blocks:
        if not isinstance(block, list) or not block:
            return False
        for message in block:
            if not isinstance(message, dict) or "role" not in message:
                return False
    return True
