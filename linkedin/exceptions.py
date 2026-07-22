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
