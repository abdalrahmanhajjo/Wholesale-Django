"""Domain exceptions raised by the posting service boundary."""

from enum import StrEnum


class PostingErrorCode(StrEnum):
    """Stable machine-readable codes for views, APIs, logs, and tests."""

    INVALID_REQUEST = "invalid_request"
    INVALID_DRAFT = "invalid_draft"
    INVALID_AMOUNT = "invalid_amount"
    UNSAVED_OBJECT = "unsaved_object"
    UNAUTHENTICATED_ACTOR = "unauthenticated_actor"
    INVALID_BUILDER_RESULT = "invalid_builder_result"
    INVALID_SERVICE_RESULT = "invalid_service_result"
    ENGINE_UNAVAILABLE = "engine_unavailable"


class PostingError(Exception):
    """Base class for errors safe for posting callers to catch."""

    default_code = PostingErrorCode.INVALID_REQUEST

    def __init__(self, message: str, *, code: PostingErrorCode | None = None) -> None:
        super().__init__(message)
        self.code = code or self.default_code


class PostingContractError(PostingError, ValueError):
    """The caller supplied a malformed posting request or journal draft."""


class PostingEngineUnavailable(PostingError):
    """The Day-1 interface exists but persistence has not been enabled yet."""

    default_code = PostingErrorCode.ENGINE_UNAVAILABLE
