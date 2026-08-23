"""Record explicit user use of an exact Prompt/Reply Rescue artifact revision."""

from __future__ import annotations

import contextlib
import sqlite3
from datetime import datetime

from ..config import Config
from . import store


class ArtifactAdoptionConflict(RuntimeError):
    """The prepared artifact changed after the user reviewed it."""


class ArtifactAdoptionService:
    def __init__(self, conn: sqlite3.Connection, cfg: Config) -> None:
        self.conn = conn
        self.cfg = cfg
        store.ensure_schema(conn)

    def record_used(
        self,
        *,
        artifact_kind: str,
        artifact_id: str,
        expected_version: int,
        expected_artifact_digest: str,
        adopted_at: datetime | None = None,
    ) -> tuple[store.ArtifactAdoption, bool]:
        if type(expected_version) is not int or expected_version < 1:
            raise ValueError("artifact adoption version is invalid")
        with _atomic(self.conn, "artifact_adoption_record"):
            job = self._current_job(artifact_kind, artifact_id)
            if (
                job is None
                or job.status != "ready"
                or job.output is None
                or job.version != expected_version
                or job.output_digest != expected_artifact_digest
            ):
                raise ArtifactAdoptionConflict("prepared artifact changed")
            return store.record(
                self.conn,
                artifact_kind=artifact_kind,
                artifact_id=job.id,
                artifact_digest=job.output_digest,
                artifact_version=job.version,
                artifact=job.output,
                output_edited=job.output_edited,
                adopted_at=adopted_at,
            )

    def is_current(self, adoption: store.ArtifactAdoption) -> bool:
        job = self._current_job(adoption.artifact_kind, adoption.artifact_id)
        return bool(
            job is not None
            and job.status == "ready"
            and job.output is not None
            and job.output_digest == adoption.artifact_digest
            and job.output == adoption.artifact
        )

    def _current_job(self, artifact_kind: str, artifact_id: str):
        if artifact_kind == "prompt_rescue":
            from ..prompt_rescue.service import PromptRescueService

            return PromptRescueService(self.conn, self.cfg).get(artifact_id)
        if artifact_kind == "reply_rescue":
            from ..reply_rescue.service import ReplyRescueService

            return ReplyRescueService(self.conn, self.cfg).get(artifact_id)
        raise ValueError("unsupported artifact adoption kind")


@contextlib.contextmanager
def _atomic(conn: sqlite3.Connection, name: str):
    if conn.in_transaction:
        conn.execute(f"SAVEPOINT {name}")
        try:
            yield
            conn.execute(f"RELEASE SAVEPOINT {name}")
        except BaseException:
            conn.execute(f"ROLLBACK TO SAVEPOINT {name}")
            conn.execute(f"RELEASE SAVEPOINT {name}")
            raise
        return
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield
        conn.execute("COMMIT")
    except BaseException:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise
