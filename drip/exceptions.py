class DripError(Exception):
    """Base exception for expected drip-domain failures."""


class ManifestValidationError(DripError):
    """A campaign manifest is invalid or cannot be read."""


class LinkAttributionError(DripError):
    """A drip tracked-link reference or destination is invalid."""


class PublicationError(DripError):
    """A validated manifest cannot be published safely."""


class EnrollmentPlanError(DripError):
    """An enrollment plan is invalid, stale, or unsafe to apply."""


class HandoffReviewError(DripError):
    """A current-sequence handoff decision cannot be reviewed safely."""


class ReconciliationBusy(DripError):
    """Another drip reconciliation pass owns the global database lock."""
