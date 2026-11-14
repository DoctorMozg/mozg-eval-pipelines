"""Uploads a stored run to an mzeval server; every call is idempotent, so a push can rerun."""

import logging
from collections.abc import Mapping
from typing import Self

import httpx2

from mozg_eval.grading.models import GradeResult, JudgeSpend
from mozg_eval.runner.suite import TRIAL_ARTIFACTS
from mozg_eval.spec.models import StrictModel
from mozg_eval.storage.base import ArtifactNotFoundError, ArtifactRef, TrialRecord
from mozg_eval.storage.config import Stores

log = logging.getLogger(__name__)

SERVER_ENV, TOKEN_ENV = "MZEVAL_SERVER", "MZEVAL_TOKEN"
TIMEOUT_S = 30.0


class PushError(RuntimeError):
    pass


class TrialUpload(StrictModel):
    """The body of PUT /v1/runs/{run}/trials/{trial}."""

    trial: TrialRecord
    grades: list[GradeResult]
    judge_spend: list[JudgeSpend]


class PushTarget(StrictModel):
    server: str
    token: str

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> Self:
        missing = [name for name in (SERVER_ENV, TOKEN_ENV) if not env.get(name)]
        if missing:
            raise PushError(f"set {' and '.join(missing)} to push results to a server")
        return cls(server=env[SERVER_ENV].rstrip("/"), token=env[TOKEN_ENV])

    def client(self, transport: httpx2.AsyncBaseTransport | None = None) -> httpx2.AsyncClient:
        return httpx2.AsyncClient(
            base_url=self.server,
            headers={"Authorization": f"Bearer {self.token}"},
            timeout=TIMEOUT_S,
            transport=transport,
        )


class RunPusher:
    def __init__(self, stores: Stores, ws: str, client: httpx2.AsyncClient) -> None:
        self.stores = stores
        self.ws = ws
        self.client = client

    async def push(self, run_id: str) -> int:
        """Send the run, then each trial with its grades, events and artifacts; returns trials."""
        results = self.stores.results
        run = results.get_run(self.ws, run_id)
        if run is None:
            raise LookupError(f"no stored run {run_id!r}")
        await self._send("PUT", f"/v1/runs/{run_id}", json=run.model_dump(mode="json"))
        grades = results.list_grades(self.ws, run_id)
        spend = results.list_spend(self.ws, run_id)
        trials = results.list_trials(self.ws, run_id)
        for trial in trials:
            base = f"/v1/runs/{run_id}/trials/{trial.id}"
            upload = TrialUpload(
                trial=trial, grades=grades.get(trial.id, []), judge_spend=spend.get(trial.id, [])
            )
            await self._send("PUT", base, json=upload.model_dump(mode="json"))
            stored = self.stores.events.read(self.ws, run_id, trial.id)
            events = [event.model_dump(mode="json") async for event in stored]
            await self._send("POST", f"{base}/events", json=events)
            for name in TRIAL_ARTIFACTS:
                data = await self._artifact(ArtifactRef(run=run_id, trial=trial.id, name=name))
                if data is not None:
                    await self._send("PUT", f"{base}/artifacts/{name}", content=data)
        log.info("pushed run %s: %d trials", run_id, len(trials))
        return len(trials)

    async def _artifact(self, ref: ArtifactRef) -> bytes | None:
        try:
            return b"".join([chunk async for chunk in self.stores.artifacts.get(self.ws, ref)])
        except ArtifactNotFoundError:
            return None

    async def _send(
        self, method: str, url: str, *, json: object = None, content: bytes | None = None
    ) -> None:
        try:
            response = await self.client.request(method, url, json=json, content=content)
        except httpx2.HTTPError as exc:
            raise PushError(f"{method} {url}: {exc}") from exc
        if response.is_error:
            raise PushError(f"{method} {url}: HTTP {response.status_code} {response.text[:200]}")
