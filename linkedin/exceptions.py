class AuthenticationError(Exception):
    """Custom exception for 401 Unauthorized errors."""
    pass


class SupervisorControlError(Exception):
    """The local sender cannot be resolved safely for supervisor controls."""


class SupervisorStopped(Exception):
    """An emergency stop prevents the supervisor from launching workers."""


class SupervisorShutdownError(Exception):
    """Owned processes survived the supervisor's terminate/kill deadlines."""


class TerminalStateError(Exception):
    """Profile is already done or dead — caller must skip it"""
    pass


class SkipProfile(Exception):
    """Profile must be skipped."""
    pass


class ReachedConnectionLimit(Exception):
    """LinkedIn reported a connection or personalized-invitation note limit."""
    pass


class InvitationWithdrawalError(Exception):
    """A pending invitation could not be safely confirmed or withdrawn."""

    pass


class InvitationWithdrawalConflictError(InvitationWithdrawalError):
    """A withdrawal run conflicts with another process or a live daemon."""

    pass


class InvitationWithdrawalIdentityError(InvitationWithdrawalError):
    """The authenticated LinkedIn identity is not the requested sender."""

    pass


class SheetsError(Exception):
    """Google Sheets API call failed (network error, auth error, malformed response)."""
    pass


class GeneralICPMessageError(Exception):
    """General ICP message draft or publication data is invalid."""

    pass


class SeedImportError(ValueError):
    """A supplied seed classification cannot be imported without losing intent."""


class MessageRoleError(Exception):
    """A nonblank lead role tag has no approved message wording."""


class EnrichmentError(Exception):
    """A phone-enrichment provider returned a valid-JSON but unexpected
    response (missing required keys). Transport failures use HttpError and
    convert to API_FAILURE instead — this one is a real bug and propagates."""
    pass


class MarketplaceListenerError(Exception):
    """FedRAMP marketplace source fetch or schema validation failed."""

    pass


class GranolaError(Exception):
    """Granola API authentication, transport, or response validation failed."""

    pass


class GranolaAuthenticationError(GranolaError):
    """Granola rejected the configured credentials or their scopes."""


class GranolaNotFoundError(GranolaError):
    """A previously known Granola resource is no longer accessible."""


class GranolaPayloadTooLargeError(GranolaError):
    """Granola could not return the requested payload inline."""


class GranolaRequestError(GranolaError):
    """Granola rejected a non-retryable request other than authentication."""

    def __init__(self, message: str, *, status_code: int):
        super().__init__(message)
        self.status_code = status_code


class GranolaResponseError(GranolaError):
    """Granola returned a response that violates the documented schema."""


class GranolaTransientError(GranolaError):
    """A retryable Granola transport, rate-limit, or server failure persisted."""

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        retry_after_seconds: float | None = None,
    ):
        super().__init__(message)
        self.status_code = status_code
        self.retry_after_seconds = retry_after_seconds


class DiscoveryConfigurationError(Exception):
    """Discovery configuration is missing or malformed."""

    pass


class DiscoverySurfaceError(Exception):
    """A supported LinkedIn discovery surface could not be read safely."""

    pass


class DiscoverySessionConflictError(Exception):
    """A standalone discovery command conflicts with a live sender daemon."""

    pass


class SalesNavigatorSurfaceError(Exception):
    """A supported Sales Navigator search/list surface could not be read safely."""

    pass


class DiscoveryScreeningError(Exception):
    """The discovery ICP screen returned an invalid structured result."""

    pass


class LegacyOutboundCutoverError(Exception):
    """A reviewed legacy-outbound retirement cannot be planned or applied safely."""

    pass


class LinkedInMediaValidationError(Exception):
    """A declared LinkedIn message attachment is missing, unsafe, or invalid."""

    pass


class LinkedInMediaMismatchError(LinkedInMediaValidationError):
    """A LinkedIn attachment no longer matches its frozen metadata."""

    pass


class LinkedInMessageSubmissionUnclearError(Exception):
    """LinkedIn may have accepted a message, so automation must not retry it."""

    pass


class FeedSampleInterruptedError(Exception):
    """A feed sample reservation outlived its process; it is now held for review."""

    pass


class LinkedInRepostError(Exception):
    """Base class for the queued LinkedIn repost ledger and its worker."""


class LinkedInRepostValidationError(LinkedInRepostError, ValueError):
    """A repost request failed strict validation; nothing was written."""


class LinkedInRepostConflictError(LinkedInRepostValidationError):
    """An external_ref already exists with a different payload; nothing changed."""


class LinkedInRepostImmutableError(LinkedInRepostError):
    """A stored repost payload (ref, operator, post, commentary) cannot change."""


class LinkedInRepostStateError(LinkedInRepostError):
    """A repost row is not in a state that allows the requested change."""


class LinkedInRepostWorkerRefused(LinkedInRepostError):
    """The local repost worker cannot run safely for this checkout's sender."""


class RepostSendError(RuntimeError):
    """A repost failed before its final click; LinkedIn was not mutated.

    ``error_type`` is a class name only. Messages never carry page text.
    """

    def __init__(self, message: str = "", *, error_type: str | None = None):
        super().__init__(message or type(self).__name__)
        self.error_type = error_type or type(self).__name__


class RepostUncertainError(RepostSendError):
    """The final repost click was attempted; LinkedIn may have accepted it."""


class RepostBrowserUnavailableError(RepostSendError):
    """The daemon browser could not be attached over loopback CDP."""


class RepostLoginWallError(RepostSendError):
    """LinkedIn showed a login, checkpoint or authwall page."""


class RepostCheckpointError(RepostSendError):
    """LinkedIn showed a captcha or checkpoint form."""


class RepostPostUnavailableError(RepostSendError):
    """The post page redirected away from a LinkedIn post permalink."""


class RepostControlNotFoundError(RepostSendError):
    """No unique Repost menu trigger was found on the post."""


class RepostOptionNotFoundError(RepostSendError):
    """The Repost menu did not show exactly one requested option."""


class RepostComposerError(RepostSendError):
    """The commentary composer, editor or Post button was missing or not empty."""


class RepostCommentaryMismatchError(RepostSendError):
    """The editor text differed from the approved commentary before Post."""


class RepostStateChangedError(RepostSendError):
    """The ledger row changed before the final click, so nothing was clicked."""


class RepostInterruptedError(RepostSendError):
    """The worker process was asked to stop."""


class RepostRejectedError(RepostUncertainError):
    """LinkedIn showed an error after the final click."""


class RepostUnverifiedError(RepostUncertainError):
    """The final click was attempted but no new permalink could be verified."""
