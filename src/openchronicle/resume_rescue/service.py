"""Local source composition for Résumé Rescue."""

from __future__ import annotations

import hmac
import json
import math
import sqlite3
import uuid
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from typing import Any

from ..config import Config
from ..privacy.egress import model_egress_lock
from ..provenance import store as provenance_store
from ..provenance.models import EvidenceRef
from ..writer import llm as llm_mod
from . import review_store, rewrite_store, store
from .document_extract import (
    DocumentExtractionError,
    DocumentImportReview,
    admit_document_candidates,
    extract_document,
)
from .json_resume import (
    JsonResumeError,
    JsonResumeExport,
    JsonResumeImportReview,
    admit_json_resume_candidates,
    export_projection_json_resume,
    parse_json_resume,
)
from .models import build_exact_artifact
from .native_export import ResumeNativeExport, render_docx_export
from .pdf_export import render_pdf_export
from .render import ResumePreview, build_document_tree, render_preview, render_preview_tree
from .rewrite import ResumeRewriteValidationError
from .rewrite_generation import (
    TEMPLATE_VERSION as REWRITE_TEMPLATE_VERSION,
)
from .rewrite_generation import (
    ResumeRewriteEgressDenied,
    build_rewrite_provider_input,
    generate_rewrite_output,
    rewrite_template_digest,
    validate_rewrite_config,
)
from .rewrite_generation import (
    provider_summary as rewrite_provider_summary,
)


class ResumeRescueService:
    """Explicit local operations over reviewed résumé sources.

    Model-backed tailoring intentionally follows the frozen evaluator. Source
    imports admit only exact candidates the caller has reviewed.
    """

    def __init__(
        self,
        conn: sqlite3.Connection,
        cfg: Config,
        *,
        llm_caller: Callable[..., Any] | None = None,
    ) -> None:
        self.conn = conn
        self.cfg = cfg
        self.llm_caller = llm_caller
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

    def review_json_resume(self, source_text: str) -> JsonResumeImportReview:
        """Parse an untrusted JSON Resume source without admitting any fact."""

        self._require_enabled()
        return parse_json_resume(source_text)

    def review_document(self, source: bytes, *, source_format: str) -> DocumentImportReview:
        """Extract an untrusted PDF or DOCX without admitting any fact."""

        self._require_enabled()
        return extract_document(source, source_format=source_format)

    def admit_document(
        self,
        *,
        source: bytes,
        source_format: str,
        expected_review_digest: str,
        profile_id: str,
        display_name: str,
        locale: str,
        selections: Sequence[dict[str, Any]],
        expected_version: int | None = None,
    ) -> tuple[store.ProfileVersion, bool]:
        """Append explicitly reviewed document excerpts to one profile version."""

        self._require_enabled()
        if not self._valid_digest(expected_review_digest):
            raise DocumentExtractionError("document expected review digest is invalid")
        review = extract_document(source, source_format=source_format)
        if review.review_digest != expected_review_digest:
            raise store.ResumeRescueConflict("document review changed")
        facts = admit_document_candidates(
            review,
            source,
            list(selections),
            reviewed_at=datetime.now(UTC).isoformat(timespec="microseconds"),
        )
        current = store.get_current_profile(self.conn, profile_id)
        if current is None:
            existing_facts: list[dict[str, Any]] = []
            conflicts: list[dict[str, Any]] = []
        else:
            if expected_version != current.version:
                raise store.ResumeRescueConflict("resume profile changed")
            if (
                display_name != current.profile["display_name"]
                or locale != current.profile["locale"]
            ):
                raise store.ResumeRescueConflict("resume profile identity changed")
            existing_facts = list(current.profile["facts"])
            conflicts = list(current.profile["conflicts"])
        fact_ids = {fact["id"] for fact in existing_facts}
        if any(fact["id"] in fact_ids for fact in facts):
            raise DocumentExtractionError("document fact id already exists")
        return self.save_profile(
            profile_id=profile_id,
            display_name=display_name,
            locale=locale,
            facts=[*existing_facts, *facts],
            conflicts=conflicts,
            expected_version=expected_version,
        )

    def admit_json_resume(
        self,
        *,
        source_text: str,
        expected_review_digest: str,
        profile_id: str,
        display_name: str,
        locale: str,
        selections: Sequence[dict[str, Any]],
        expected_version: int | None = None,
    ) -> tuple[store.ProfileVersion, bool]:
        """Append explicitly reviewed JSON Resume facts to one profile version."""

        self._require_enabled()
        review = parse_json_resume(source_text)
        if not self._valid_digest(expected_review_digest):
            raise JsonResumeError("JSON Resume expected review digest is invalid")
        if review.review_digest != expected_review_digest:
            raise store.ResumeRescueConflict("JSON Resume review changed")
        facts = admit_json_resume_candidates(
            review,
            list(selections),
            reviewed_at=datetime.now(UTC).isoformat(timespec="microseconds"),
        )
        current = store.get_current_profile(self.conn, profile_id)
        if current is None:
            existing_facts: list[dict[str, Any]] = []
            conflicts: list[dict[str, Any]] = []
        else:
            if expected_version != current.version:
                raise store.ResumeRescueConflict("resume profile changed")
            if (
                display_name != current.profile["display_name"]
                or locale != current.profile["locale"]
            ):
                raise store.ResumeRescueConflict("resume profile identity changed")
            existing_facts = list(current.profile["facts"])
            conflicts = list(current.profile["conflicts"])
        fact_ids = {fact["id"] for fact in existing_facts}
        if any(fact["id"] in fact_ids for fact in facts):
            raise JsonResumeError("JSON Resume fact id already exists")
        return self.save_profile(
            profile_id=profile_id,
            display_name=display_name,
            locale=locale,
            facts=[*existing_facts, *facts],
            conflicts=conflicts,
            expected_version=expected_version,
        )

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

    def rewrite_provider_summary(self) -> dict[str, str]:
        """Return the model identity and location the user must approve."""

        validate_rewrite_config(self.cfg)
        return rewrite_provider_summary(self.cfg)

    def queue_rewrite(
        self,
        projection_id: str,
        *,
        expected_artifact_digest: str,
        expected_model_identity: str,
        expected_provider_location: str,
        remote_egress_authorized: bool,
    ) -> tuple[rewrite_store.ResumeRewriteJob, bool]:
        """Queue one exact reviewed projection for a disclosed provider."""

        self._require_enabled()
        validate_rewrite_config(self.cfg)
        if not self.cfg.resume_rescue.rewrite_enabled:
            raise ResumeRewriteEgressDenied("resume rewrite is disabled")
        if not self._valid_digest(expected_artifact_digest):
            raise store.ResumeRescueConflict("resume rescue projection changed")
        projection = self.get_projection(projection_id)
        if projection is None or not hmac.compare_digest(
            projection.artifact_digest, expected_artifact_digest
        ):
            raise store.ResumeRescueConflict("resume rescue projection changed")
        provider = rewrite_provider_summary(self.cfg)
        if (
            not isinstance(expected_model_identity, str)
            or not isinstance(expected_provider_location, str)
            or not hmac.compare_digest(provider["model"], expected_model_identity)
            or not hmac.compare_digest(provider["location"], expected_provider_location)
        ):
            raise ResumeRewriteEgressDenied("resume rewrite provider disclosure changed")
        if type(remote_egress_authorized) is not bool:
            raise ResumeRewriteEgressDenied("resume rewrite egress authorization is invalid")
        if provider["location"] == "remote_or_unknown" and not remote_egress_authorized:
            raise ResumeRewriteEgressDenied("resume rewrite remote egress is not authorized")
        provider_input = build_rewrite_provider_input(projection.artifact)
        self._bounded_json(
            provider_input,
            self.cfg.resume_rescue.rewrite_max_input_chars,
            "rewrite provider input",
        )
        return rewrite_store.create(
            self.conn,
            projection=projection,
            provider_input=provider_input,
            template_version=REWRITE_TEMPLATE_VERSION,
            template_digest=rewrite_template_digest(),
            model_identity=provider["model"],
            provider_location=provider["location"],
            remote_egress_authorized=remote_egress_authorized,
        )

    def get_rewrite(self, job_id: str) -> rewrite_store.ResumeRewriteJob | None:
        job = rewrite_store.get(self.conn, job_id)
        return job if job is not None and self._rewrite_current(job) else None

    def list_rewrites(self, *, limit: int = 50) -> list[rewrite_store.ResumeRewriteJob]:
        return [
            job
            for job in rewrite_store.list_jobs(self.conn, limit=limit)
            if self._rewrite_current(job)
        ]

    def process_next_rewrite(self) -> rewrite_store.ResumeRewriteJob | None:
        """Generate one proposal set after revalidating every frozen binding."""

        validate_rewrite_config(self.cfg)
        if not self.cfg.resume_rescue.enabled or not self.cfg.resume_rescue.rewrite_enabled:
            return None
        lease_seconds = max(
            self.cfg.resume_rescue.rewrite_lease_seconds,
            math.ceil(llm_mod.call_budget_seconds(self.cfg, "resume_rescue")),
        )
        if lease_seconds > 21_600:
            raise ValueError("resume rewrite provider budget exceeds safe lease")
        lease_token = uuid.uuid4().hex
        claimed = rewrite_store.claim_next(
            self.conn,
            lease_token=lease_token,
            lease_seconds=lease_seconds,
        )
        if claimed is None:
            return None
        try:
            with model_egress_lock():
                current = rewrite_store.get(self.conn, claimed.id)
                if (
                    current is None
                    or current.status != "leased"
                    or current.lease_token != lease_token
                    or not self._rewrite_current(current)
                ):
                    raise _ResumeRewriteInputChanged
                projection = self.get_projection(current.projection_id)
                if projection is None:
                    raise _ResumeRewriteInputChanged
                output = generate_rewrite_output(
                    self.cfg,
                    artifact=projection.artifact,
                    expected_model_identity=current.model_identity,
                    expected_provider_location=current.provider_location,
                    remote_egress_authorized=current.remote_egress_authorized,
                    llm_caller=self.llm_caller,
                )
            return rewrite_store.complete(
                self.conn,
                job_id=claimed.id,
                lease_token=lease_token,
                output=output,
            )
        except llm_mod.ProviderCallCancelledError:
            rewrite_store.release_claim(
                self.conn,
                job_id=claimed.id,
                lease_token=lease_token,
            )
            raise
        except rewrite_store.ResumeRewriteConflict:
            raise
        except (_ResumeRewriteInputChanged, ResumeRewriteEgressDenied):
            return rewrite_store.fail(
                self.conn,
                job_id=claimed.id,
                lease_token=lease_token,
                error_code="input_changed",
            )
        except ResumeRewriteValidationError as exc:
            return rewrite_store.fail(
                self.conn,
                job_id=claimed.id,
                lease_token=lease_token,
                error_code=exc.code,
            )
        except Exception:  # noqa: BLE001 - durable public state is sanitized
            return rewrite_store.fail(
                self.conn,
                job_id=claimed.id,
                lease_token=lease_token,
                error_code="provider_failed",
            )

    def retry_rewrite(
        self, job_id: str, *, expected_version: int
    ) -> rewrite_store.ResumeRewriteJob:
        validate_rewrite_config(self.cfg)
        if not self.cfg.resume_rescue.enabled or not self.cfg.resume_rescue.rewrite_enabled:
            raise ResumeRewriteEgressDenied("resume rewrite is disabled")
        if self.get_rewrite(job_id) is None:
            raise rewrite_store.ResumeRewriteConflict("resume rewrite changed")
        return rewrite_store.retry(
            self.conn,
            job_id=job_id,
            expected_version=expected_version,
        )

    def delete_rewrite(self, job_id: str, *, expected_version: int) -> None:
        rewrite_store.delete(
            self.conn,
            job_id=job_id,
            expected_version=expected_version,
        )

    def decide_rewrite(
        self,
        job_id: str,
        *,
        proposal_id: str,
        expected_proposal_digest: str,
        expected_job_version: int,
        expected_head_id: str,
        expected_artifact_digest: str,
        decision: str,
    ) -> tuple[review_store.ResumeRewriteVersion, bool]:
        self._require_enabled()
        job = self.get_rewrite(job_id)
        if job is None or job.status != "ready":
            raise review_store.ResumeRewriteReviewConflict("resume rewrite output changed")
        return review_store.decide(
            self.conn,
            job_id=job_id,
            proposal_id=proposal_id,
            expected_proposal_digest=expected_proposal_digest,
            expected_job_version=expected_job_version,
            expected_head_id=expected_head_id,
            expected_artifact_digest=expected_artifact_digest,
            decision=decision,
        )

    def get_rewrite_head(self, job_id: str) -> review_store.ResumeRewriteVersion | None:
        version = review_store.get_head(self.conn, job_id)
        return version if version is not None and self._review_current(version) else None

    def list_rewrite_versions(
        self, job_id: str, *, limit: int = 50
    ) -> list[review_store.ResumeRewriteVersion]:
        return [
            version
            for version in review_store.list_versions(self.conn, job_id=job_id, limit=limit)
            if self._review_current(version)
        ]

    def restore_rewrite(
        self,
        *,
        target_version_id: str,
        expected_head_id: str,
        expected_artifact_digest: str,
    ) -> review_store.ResumeRewriteVersion:
        target = review_store.get(self.conn, target_version_id)
        if target is None or not self._review_current(target):
            raise review_store.ResumeRewriteReviewConflict("resume rewrite restore target changed")
        return review_store.restore(
            self.conn,
            target_version_id=target_version_id,
            expected_head_id=expected_head_id,
            expected_artifact_digest=expected_artifact_digest,
        )

    def preview_rewrite(self, version_id: str) -> ResumePreview:
        version, profile = self._review_document_sources(version_id)
        return render_preview(profile=profile, projection=version)

    def export_rewrite_json(self, version_id: str) -> JsonResumeExport:
        version, profile = self._review_document_sources(version_id)
        return export_projection_json_resume(profile=profile, projection=version)

    def export_rewrite_docx(
        self, version_id: str, *, expected_preview_document_digest: str
    ) -> ResumeNativeExport:
        version, profile = self._review_document_sources(version_id)
        tree = build_document_tree(profile=profile, projection=version)
        preview = render_preview_tree(tree)
        if not hmac.compare_digest(preview.document_digest, expected_preview_document_digest):
            raise review_store.ResumeRewriteReviewConflict("resume rewrite preview changed")
        return render_docx_export(tree, preview_document_digest=preview.document_digest)

    def export_rewrite_pdf(
        self, version_id: str, *, expected_preview_document_digest: str
    ) -> ResumeNativeExport:
        version, profile = self._review_document_sources(version_id)
        tree = build_document_tree(profile=profile, projection=version)
        preview = render_preview_tree(tree)
        if not hmac.compare_digest(preview.document_digest, expected_preview_document_digest):
            raise review_store.ResumeRewriteReviewConflict("resume rewrite preview changed")
        return render_pdf_export(tree, preview_document_digest=preview.document_digest)

    def preview(self, projection_id: str) -> ResumePreview:
        projection = self.get_projection(projection_id)
        if projection is None:
            raise store.ResumeRescueConflict("resume rescue projection changed")
        profile = store.get_profile_version(
            self.conn, projection.profile_id, projection.profile_version
        )
        if profile is None:
            raise store.ResumeRescueConflict("resume rescue profile changed")
        return render_preview(profile=profile, projection=projection)

    def export_json_resume(self, projection_id: str) -> JsonResumeExport:
        """Build a loss-explicit JSON Resume export from a current projection."""

        self._require_enabled()
        projection = self.get_projection(projection_id)
        if projection is None:
            raise store.ResumeRescueConflict("resume rescue projection changed")
        profile = store.get_profile_version(
            self.conn, projection.profile_id, projection.profile_version
        )
        if profile is None:
            raise store.ResumeRescueConflict("resume rescue profile changed")
        return export_projection_json_resume(profile=profile, projection=projection)

    def export_docx(
        self, projection_id: str, *, expected_preview_document_digest: str
    ) -> ResumeNativeExport:
        """Build a current DOCX only after binding the reviewed preview digest."""

        self._require_enabled()
        projection = self.get_projection(projection_id)
        if projection is None:
            raise store.ResumeRescueConflict("resume rescue projection changed")
        profile = store.get_profile_version(
            self.conn, projection.profile_id, projection.profile_version
        )
        if profile is None:
            raise store.ResumeRescueConflict("resume rescue profile changed")
        tree = build_document_tree(profile=profile, projection=projection)
        preview = render_preview_tree(tree)
        if not hmac.compare_digest(preview.document_digest, expected_preview_document_digest):
            raise store.ResumeRescueConflict("resume rescue preview changed")
        return render_docx_export(tree, preview_document_digest=preview.document_digest)

    def export_pdf(
        self, projection_id: str, *, expected_preview_document_digest: str
    ) -> ResumeNativeExport:
        """Build a current PDF only through the exact audited development engine."""

        self._require_enabled()
        projection = self.get_projection(projection_id)
        if projection is None:
            raise store.ResumeRescueConflict("resume rescue projection changed")
        profile = store.get_profile_version(
            self.conn, projection.profile_id, projection.profile_version
        )
        if profile is None:
            raise store.ResumeRescueConflict("resume rescue profile changed")
        tree = build_document_tree(profile=profile, projection=projection)
        preview = render_preview_tree(tree)
        if not hmac.compare_digest(preview.document_digest, expected_preview_document_digest):
            raise store.ResumeRescueConflict("resume rescue preview changed")
        return render_pdf_export(tree, preview_document_digest=preview.document_digest)

    def _review_document_sources(
        self, version_id: str
    ) -> tuple[review_store.ResumeRewriteVersion, store.ProfileVersion]:
        version = review_store.get(self.conn, version_id)
        if version is None or not self._review_current(version):
            raise review_store.ResumeRewriteReviewConflict("resume rewrite version changed")
        profile = store.get_profile_version(self.conn, version.profile_id, version.profile_version)
        if profile is None:
            raise review_store.ResumeRewriteReviewConflict("resume rewrite profile changed")
        return version, profile

    def _review_current(self, version: review_store.ResumeRewriteVersion) -> bool:
        base = self.get_projection(version.base_projection_id)
        job = self.get_rewrite(version.rewrite_job_id)
        if (
            base is None
            or job is None
            or job.status != "ready"
            or base.artifact_digest != version.base_artifact_digest
            or job.output_digest != version.rewrite_output_digest
        ):
            return False
        return provenance_store.is_current(self.conn, version.ref)

    def _rewrite_current(self, job: rewrite_store.ResumeRewriteJob) -> bool:
        projection = self.get_projection(job.projection_id)
        if (
            projection is None
            or projection.artifact_digest != job.projection_artifact_digest
            or projection.created_at != job.projection_created_at
        ):
            return False
        subject = EvidenceRef(kind="resume_rewrite", id=job.id)
        if provenance_store.direct_sources_checked(self.conn, subject) != [projection.ref]:
            return False
        if not provenance_store.is_current(self.conn, projection.ref):
            return False
        try:
            provider = rewrite_provider_summary(self.cfg)
            return bool(
                job.template_version == REWRITE_TEMPLATE_VERSION
                and hmac.compare_digest(job.template_digest, rewrite_template_digest())
                and hmac.compare_digest(job.model_identity, provider["model"])
                and hmac.compare_digest(job.provider_location, provider["location"])
                and job.provider_input == build_rewrite_provider_input(projection.artifact)
                and (job.provider_location != "remote_or_unknown" or job.remote_egress_authorized)
            )
        except (ResumeRewriteValidationError, TypeError, ValueError):
            return False

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

    @staticmethod
    def _valid_digest(value: object) -> bool:
        return (
            isinstance(value, str)
            and len(value) == 64
            and all(character in "0123456789abcdef" for character in value)
        )


class _ResumeRewriteInputChanged(RuntimeError):
    """Internal sentinel for a lease whose frozen source is no longer current."""
