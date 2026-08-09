class AuthenticationError(Exception):
    """Custom exception for 401 Unauthorized errors."""
    pass


class TerminalStateError(Exception):
    """Profile is already done or dead — caller must skip it"""
    pass


class SkipProfile(Exception):
    """Profile must be skipped."""
    pass


class ReachedConnectionLimit(Exception):
    """ Weekly connection limit reached. """
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


class EnrichmentError(Exception):
    """A phone-enrichment provider returned a valid-JSON but unexpected
    response (missing required keys). Transport failures use HttpError and
    convert to API_FAILURE instead — this one is a real bug and propagates."""
    pass


class MarketplaceListenerError(Exception):
    """FedRAMP marketplace source fetch or schema validation failed."""

    pass


class DiscoveryConfigurationError(Exception):
    """Discovery configuration is missing or malformed."""

    pass


class DiscoverySurfaceError(Exception):
    """A supported LinkedIn discovery surface could not be read safely."""

    pass


class DiscoverySessionConflictError(Exception):
    """A standalone discovery command conflicts with a live sender daemon."""

    pass


class DiscoveryScreeningError(Exception):
    """The discovery ICP screen returned an invalid structured result."""

    pass
