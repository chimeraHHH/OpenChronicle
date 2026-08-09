"""Evidence-backed, review-only Résumé Rescue workflow."""

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
from .render import ResumePreview
from .rewrite import (
    ResumeRewriteValidationError,
    rewrite_output_digest,
    validate_rewrite_artifact_for_egress,
    validate_rewrite_model_output,
)
from .rewrite_generation import (
    ResumeRewriteEgressDenied,
    build_rewrite_provider_input,
    generate_rewrite_output,
)
from .rewrite_generation import (
    provider_summary as rewrite_provider_summary,
)
from .service import ResumeRescueService
from .store import OpportunitySnapshot, ProfileVersion, ResumeProjection, ResumeRescueConflict

__all__ = [
    "DocumentExtractionError",
    "DocumentImportReview",
    "OpportunitySnapshot",
    "ProfileVersion",
    "JsonResumeError",
    "JsonResumeExport",
    "JsonResumeImportReview",
    "ResumeProjection",
    "ResumePreview",
    "ResumeRescueConflict",
    "ResumeRescueService",
    "ResumeRewriteValidationError",
    "ResumeRewriteEgressDenied",
    "admit_document_candidates",
    "admit_json_resume_candidates",
    "export_projection_json_resume",
    "extract_document",
    "parse_json_resume",
    "build_rewrite_provider_input",
    "generate_rewrite_output",
    "rewrite_output_digest",
    "validate_rewrite_artifact_for_egress",
    "validate_rewrite_model_output",
    "rewrite_provider_summary",
]
