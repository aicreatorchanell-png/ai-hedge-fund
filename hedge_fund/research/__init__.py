"""Research tooling: AI-assisted proposals and critiques, never trading decisions."""

from hedge_fund.research.crossreview import CrossReview, CrossReviewer, ReviewStep, check_explicit_model
from hedge_fund.research.governance import (
    ExperimentLedger, LedgerTampered, LedgerViolation, Outcome, ResearchManifest, build_manifest,
)

__all__ = ["CrossReview", "CrossReviewer", "ExperimentLedger", "LedgerTampered", "LedgerViolation", "Outcome",
           "ResearchManifest", "ReviewStep", "build_manifest", "check_explicit_model"]
