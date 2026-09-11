class GmailDeliveryAuthorizationError(Exception):
    """A Gmail submission is not backed by an authorized automation Task."""


class GmailSubmissionDeferred(Exception):
    """A current Gmail Task was safely deferred before provider submission."""
