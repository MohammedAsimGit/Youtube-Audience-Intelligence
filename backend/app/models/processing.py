"""Processing-state model for comments (the Sprint 4 hand-off contract).

At-rest states a comment row can hold:
    READY_FOR_ANALYSIS   stored by Sprint 3 ingestion (clean dataset)
    PROCESSING           reserved: an analysis engine claimed the row
    PROCESSED            reserved: analysis finished
    FAILED               processing failed - needs reprocessing

Pipeline-stage states (ACQUIRED -> VALIDATED -> NORMALIZED -> DEDUPLICATED)
exist so staged/batch workers and tests can express where a record is in the
lifecycle. Sprint 3's synchronous ingestion validates + normalizes + dedups
BEFORE persistence, so rows are written directly at READY_FOR_ANALYSIS
(invalid records never enter the database at all).

Sprint 4 (sentiment/emotion/topic analysis) moves rows through
READY_FOR_ANALYSIS -> PROCESSING -> PROCESSED, or FAILED on error. This
sprint only defines the model, validates transitions, and exposes the
repository update - it does NOT perform analysis.
"""
from enum import Enum
from typing import Set


class ProcessingStatus(str, Enum):
    ACQUIRED = "ACQUIRED"
    VALIDATED = "VALIDATED"
    NORMALIZED = "NORMALIZED"
    DEDUPLICATED = "DEDUPLICATED"
    READY_FOR_ANALYSIS = "READY_FOR_ANALYSIS"
    PROCESSING = "PROCESSING"
    PROCESSED = "PROCESSED"
    FAILED = "FAILED"


# Allowed forward movement through the pipeline. FAILED is reachable from any
# working state and can be requeued to READY_FOR_ANALYSIS; PROCESSED is
# terminal until a future reprocessing policy reopens it explicitly.
_ALLOWED: "dict[ProcessingStatus, Set[ProcessingStatus]]" = {
    ProcessingStatus.ACQUIRED: {ProcessingStatus.VALIDATED, ProcessingStatus.FAILED},
    ProcessingStatus.VALIDATED: {ProcessingStatus.NORMALIZED, ProcessingStatus.FAILED},
    ProcessingStatus.NORMALIZED: {
        ProcessingStatus.DEDUPLICATED,
        ProcessingStatus.READY_FOR_ANALYSIS,
        ProcessingStatus.FAILED,
    },
    ProcessingStatus.DEDUPLICATED: {
        ProcessingStatus.READY_FOR_ANALYSIS,
        ProcessingStatus.FAILED,
    },
    ProcessingStatus.READY_FOR_ANALYSIS: {
        ProcessingStatus.PROCESSING,
        ProcessingStatus.FAILED,
    },
    ProcessingStatus.PROCESSING: {
        ProcessingStatus.PROCESSED,
        ProcessingStatus.FAILED,
    },
    ProcessingStatus.PROCESSED: set(),
    ProcessingStatus.FAILED: {ProcessingStatus.READY_FOR_ANALYSIS},
}


class IllegalTransition(ValueError):
    """Raised when a status change would violate the lifecycle."""


def parse_status(value: object) -> ProcessingStatus:
    """String/enum -> ProcessingStatus (raises on unknown states)."""
    if isinstance(value, ProcessingStatus):
        return value
    try:
        return ProcessingStatus(str(value))
    except ValueError as exc:
        raise ValueError(f"unknown processing status: {value!r}") from exc


def can_transition(current: ProcessingStatus, target: ProcessingStatus) -> bool:
    return target in _ALLOWED[current]


def assert_transition(current: ProcessingStatus, target: ProcessingStatus) -> None:
    if not can_transition(current, target):
        raise IllegalTransition(f"illegal processing transition: {current.value} -> {target.value}")
