"""Expected failures confined to Slack's private draft background work."""


class DraftRuntimeUnavailable(Exception):
    """Deployment lacks the supported streaming background lifecycle."""


class DraftServiceError(Exception):
    """A provider rejected a draft request or exhausted its bounded retry."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


class DraftViewError(Exception):
    """Slack could not update a modal, including a stale hash/closed view."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code
