"""Picks each store's backend from a URL: `[storage]` in mzeval.toml, overridden by env vars."""

import logging
import os
import tomllib
from collections.abc import Mapping
from pathlib import Path
from typing import NamedTuple, Self
from urllib.parse import unquote, urlparse

import sqlalchemy as sa
from pydantic import ValidationError

from mozg_eval.spec.models import StrictModel
from mozg_eval.storage.base import ArtifactStore, EventStore, ResultStore
from mozg_eval.storage.disk import DiskArtifactStore, DiskEventStore
from mozg_eval.storage.sql import SqlResultStore

log = logging.getLogger(__name__)

ENV = {"results": "MZEVAL_RESULTS", "events": "MZEVAL_EVENTS", "artifacts": "MZEVAL_ARTIFACTS"}


class StorageConfig(StrictModel):
    results: str
    events: str
    artifacts: str

    @classmethod
    def load(cls, root: Path, env: Mapping[str, str] = os.environ) -> Self:
        """Defaults keep everything under `root/.mzeval`, next to an optional mzeval.toml."""
        data = root / ".mzeval"
        values = {
            "results": f"sqlite:///{data / 'mzeval.db'}",
            "events": (data / "events").as_uri(),
            "artifacts": (data / "artifacts").as_uri(),
        }
        toml = root / "mzeval.toml"
        if toml.exists():
            with toml.open("rb") as file:
                try:
                    values |= tomllib.load(file).get("storage", {})
                except tomllib.TOMLDecodeError as exc:
                    raise ValueError(f"{toml}: {exc}") from exc
        values |= {key: env[var] for key, var in ENV.items() if var in env}
        try:
            return cls.model_validate(values)
        except ValidationError as exc:
            raise ValueError(
                f"bad storage config (from {toml} and {', '.join(ENV.values())}): {exc}"
            ) from exc


class Stores(NamedTuple):
    results: ResultStore
    events: EventStore
    artifacts: ArtifactStore


def _file_root(url: str, store: str) -> Path:
    parsed = urlparse(url)
    if parsed.scheme != "file":
        raise ValueError(f"{store} store URL {url!r}: only file:// is supported so far")
    return Path(unquote(parsed.path))


def open_stores(config: StorageConfig) -> Stores:
    results = sa.make_url(config.results).render_as_string(hide_password=True)
    if not results.startswith("sqlite:"):
        raise ValueError(f"results store URL {results!r}: only sqlite is supported so far")
    log.info("storage: results=%s events=%s artifacts=%s", results, config.events, config.artifacts)
    events = _file_root(config.events, "events")
    artifacts = _file_root(config.artifacts, "artifacts")
    return Stores(
        results=SqlResultStore(config.results),
        events=DiskEventStore(events),
        artifacts=DiskArtifactStore(artifacts),
    )
