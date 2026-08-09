"""Local source composition for Résumé Rescue."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

from ..config import Config
from ..provenance import store as provenance_store
from ..provenance.models import EvidenceRef
from . import store
from .models import build_exact_artifact


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
        snapshot = self._opportunity_payload(
            employer=employer,
            title=title,
            source_text=source_text,
            source_url=source_url,
            priorities=priorities,
            locale=locale,
            captured_at=captured_at,
        )
        return store.create_opportunity(self.conn, snapshot)

    def replace_opportunity(
        self,
        opportunity_id: str,
        *,
        expected_digest: str,
        employer: str,
        title: str,
        source_text: str,
        source_url: str = "",
        priorities: Sequence[str] = (),
        locale: str = "",
        captured_at: str | None = None,
    ) -> tuple[store.OpportunitySnapshot, bool]:
        self._require_enabled()
        snapshot = self._opportunity_payload(
            employer=employer,
            title=title,
            source_text=source_text,
            source_url=source_url,
            priorities=priorities,
            locale=locale,
            captured_at=captured_at,
        )
        return store.replace_opportunity(
            self.conn,
            opportunity_id=opportunity_id,
            expected_digest=expected_digest,
            snapshot=snapshot,
        )

    def get_profile(self, profile_id: str) -> store.ProfileVersion | None:
        return store.get_current_profile(self.conn, profile_id)

    def list_profiles(self, *, limit: int = 50) -> list[store.ProfileVersion]:
        return store.list_current_profiles(self.conn, limit=limit)

    def get_opportunity(self, opportunity_id: str) -> store.OpportunitySnapshot | None:
        return store.get_opportunity(self.conn, opportunity_id)

    def list_opportunities(self, *, limit: int = 50) -> list[store.OpportunitySnapshot]:
        return store.list_opportunities(self.conn, limit=limit)

    def compose_exact(
        self,
        *,
        profile_id: str,
        opportunity_id: str,
        sections: Sequence[dict[str, Any]],
        requirements: Sequence[dict[str, Any]] = (),
    ) -> tuple[store.ResumeProjection, bool]:
        self._require_enabled()
        profile = store.get_current_profile(self.conn, profile_id)
        opportunity = store.get_opportunity(self.conn, opportunity_id)
        if profile is None or opportunity is None:
            raise store.ResumeRescueConflict("resume rescue source changed")
        request = {
            "schema_version": 1,
            "sections": list(sections),
            "requirements": list(requirements),
        }
        return store.create_projection(
            self.conn,
            profile=profile,
            opportunity=opportunity,
            request=request,
        )

    def get_projection(self, projection_id: str) -> store.ResumeProjection | None:
        projection = store.get_projection(self.conn, projection_id)
        return (
            projection if projection is not None and self._projection_current(projection) else None
        )

    def list_projections(self, *, limit: int = 50) -> list[store.ResumeProjection]:
        return [
            projection
            for projection in store.list_projections(self.conn, limit=limit)
            if self._projection_current(projection)
        ]

    def _projection_current(self, projection: store.ResumeProjection) -> bool:
        profile = store.get_current_profile(self.conn, projection.profile_id)
        opportunity = store.get_opportunity(self.conn, projection.opportunity_id)
        if (
            profile is None
            or opportunity is None
            or profile.version != projection.profile_version
            or profile.digest != projection.profile_digest
            or opportunity.digest != projection.opportunity_digest
        ):
            return False
        sources = provenance_store.direct_sources_checked(
            self.conn, EvidenceRef(kind="resume_rescue", id=projection.id)
        )
        if sources != [profile.ref, opportunity.ref]:
            return False
        try:
            rebuilt = build_exact_artifact(
                profile=profile.profile,
                profile_version=profile.version,
                profile_digest_value=profile.digest,
                opportunity=opportunity.snapshot,
                opportunity_id=opportunity.id,
                opportunity_digest_value=opportunity.digest,
                request=projection.request,
            )
        except ValueError:
            return False
        return rebuilt == projection.artifact

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

    def _opportunity_payload(
        self,
        *,
        employer: str,
        title: str,
        source_text: str,
        source_url: str,
        priorities: Sequence[str],
        locale: str,
        captured_at: str | None,
    ) -> dict[str, Any]:
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
        return snapshot

    @staticmethod
    def _bounded_json(value: object, maximum: int, label: str) -> None:
        encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        if len(encoded) > maximum:
            raise ValueError(f"resume rescue {label} exceeds configured limit")
