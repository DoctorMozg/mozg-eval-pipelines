import hashlib
import json
import os
from pathlib import Path


class JudgeCache:
    """Accepted verdicts on disk, keyed by the evidence, criterion, model and prompt."""

    def __init__(self, root: Path) -> None:
        self.root = root

    @staticmethod
    def key(evidence: str, criterion: str, model: str, prompt: str) -> str:
        parts = [hashlib.sha256(text.encode()).hexdigest() for text in (evidence, prompt)]
        return hashlib.sha256(json.dumps([*parts, criterion, model]).encode()).hexdigest()

    def get(self, key: str) -> str | None:
        path = self._path(key)
        return path.read_text() if path.exists() else None

    def put(self, key: str, verdict: str) -> None:
        path = self._path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        # Parallel trials can judge the same thing; a rename never leaves a half-written entry.
        partial = path.with_suffix(f".{os.getpid()}.tmp")
        partial.write_text(verdict)
        partial.replace(path)

    def _path(self, key: str) -> Path:
        return self.root / key[:2] / f"{key}.json"
