"""Domain exceptions raised by the posting service boundary."""


class PostingError(Exception):
    """Base class for errors safe for posting callers to catch."""


class PostingContractError(PostingError, ValueError):
    """The caller supplied a malformed posting request or journal draft."""


class PostingEngineUnavailable(PostingError):
    """The Day-1 interface exists but persistence has not been enabled yet."""
