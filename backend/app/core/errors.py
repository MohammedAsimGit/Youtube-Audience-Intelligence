"""Categorized error taxonomy.

Each AcquisitionError maps to a stable `code` that the Chrome extension turns
into honest user-facing copy. Raw upstream details stay in server logs only.
"""


class AcquisitionError(Exception):
    """Base class for data-acquisition failures."""

    code: str = "acquisition_failed"
    http_status: int = 502

    def __init__(self, message: str = "", **details: object) -> None:
        super().__init__(message or self.code)
        self.message = message or self.code
        self.details = details


class VideoIdInvalid(AcquisitionError):
    code = "invalid_video_id"
    http_status = 422


class VideoNotFound(AcquisitionError):
    code = "video_not_found"
    http_status = 404


class CommentsDisabled(AcquisitionError):
    """Verified YouTube reason: 403 / errors[].reason = commentsDisabled."""

    code = "comments_disabled"
    http_status = 422


class QuotaExceeded(AcquisitionError):
    """Verified YouTube reasons: quotaExceeded / dailyLimitExceeded."""

    code = "quota_exceeded"
    http_status = 429


class UpstreamUnstable(AcquisitionError):
    """Malformed / unexpected YouTube response - never crashes the request."""

    code = "upstream_data_invalid"
    http_status = 502


class UpstreamTimeout(AcquisitionError):
    code = "upstream_timeout"
    http_status = 504


class UpstreamUnavailable(AcquisitionError):
    code = "upstream_unavailable"
    http_status = 502


class MissingApiKey(AcquisitionError):
    code = "server_not_configured"
    http_status = 503


class StorageUnavailable(AcquisitionError):
    """Dataset store failed while serving a read (stats endpoint only).

    Acquisition itself never raises this: a persistence failure degrades to
    serving the acquired data with a PERSISTENCE_FAILED log event.
    """

    code = "storage_unavailable"
    http_status = 503
