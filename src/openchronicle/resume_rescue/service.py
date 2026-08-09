"""Local source composition for Résumé Rescue."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

from ..config import Config
from . import store


class ResumeRescueService:
    """Explicit local operations over reviewed résumé sources.

    Model-backed tailoring and document import intentionally follow the frozen
    evaluator. This first slice admits only objects the caller has reviewed.
    """

    def __init__(self, conn: sqlite3.Connection, cfg: Config) -> None:
        self.conn = conn
        self.cfg = cfg
        store.ensure_schema(conn)

    def save_profile(
        self,
        *,
        profile_id: str,
        display_name: str,
        locale: str = "",
        facts: Sequence[dict[str, Any]],
        conflicts: Sequence[dict[str, Any]] = (),
        expected_version: int | None = None,
    ) -> tuple[store.ProfileVersion, bool]:
        self._require_enabled()
        profile = {
            "schema_version": 1,
            "profile_id": profile_id,
            "display_name": display_name,
            "locale": locale,
            "facts": list(facts),
            "conflicts": list(conflicts),
        }
        self._bounded_json(profile, self.cfg.resume_rescue.max_profile_chars, "profile")
        return store.save_profile(self.conn, profile, expected_version=expected_version)

    def save_opportunity(
        self,
        *,
        employer: str,
        title: str,
        source_text: str,
        source_url: str = "",
        priorities: Sequence[str] = (),
        locale: str = "",
        captured_at: str | None = None,
    ) -> tuple[store.OpportunitySnapshot, bool]:
        self._require_enabled()
        snapshot = {
            "schema_version": 1,
            "employer": employer,
            "title": title,
            "source_url": source_url,
            "source_text": source_text,
            "priorities": list(priorities),
            "locale": locale,
            "captured_at": captured_at or datetime.now(UTC).isoformat(timespec="microseconds"),
        }
        self._bounded_json(snapshot, self.cfg.resume_rescue.max_opportunity_chars, "opportunity")
        return store.create_opportunity(self.conn, snapshot)

    def get_profile(self, profile_id: str) -> store.ProfileVersion | None:
        return store.get_current_profile(self.conn, profile_id)

    def list_profiles(self, *, limit: int = 50) -> list[store.ProfileVersion]:
        return store.list_current_profiles(self.conn, limit=limit)

    def get_opportunity(self, opportunity_id: str) -> store.OpportunitySnapshot | None:
        return store.get_opportunity(self.conn, opportunity_id)

    def list_opportunities(self, *, limit: int = 50) -> list[store.OpportunitySnapshot]:
        return store.list_opportunities(self.conn, limit=limit)

    def _require_enabled(self) -> None:
        resume_cfg = self.cfg.resume_rescue
        if type(resume_cfg.enabled) is not bool:
            raise ValueError("resume_rescue.enabled must be a boolean")
        for name, value, maximum in (
            ("max_profile_chars", resume_cfg.max_profile_chars, 5_000_000),
            ("max_opportunity_chars", resume_cfg.max_opportunity_chars, 1_000_000),
        ):
            if type(value) is not int or not 1 <= value <= maximum:
                raise ValueError(f"resume_rescue.{name} is invalid")
        if not resume_cfg.enabled:
            raise ValueError("resume rescue is disabled")

    @staticmethod
    def _bounded_json(value: object, maximum: int, label: str) -> None:
        encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        if len(encoded) > maximum:
            raise ValueError(f"resume rescue {label} exceeds configured limit")
