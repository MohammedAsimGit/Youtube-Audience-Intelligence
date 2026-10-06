"""Job lifecycle model for background analysis jobs (Sprint 4.3).

One small, explicit state machine for the background job - kept separate
from the per-comment processing state machine (`app.models.processing`)
because they describe different things:

    Job:      QUEUED -> ACQUIRING -> ANALYZING -> COMPLETED
                                      failure -> FAILED
                                      switch  -> CANCELLED   (Sprint 4.2
                                                              invalidation)
                                      restart -> STALE        (§32 recovery)

The extension polls these states and stops on any terminal one (§26).
`ACTIVE_JOB_STATUSES` is the single source of truth used by BOTH the
repository guards and the service layer, so a status can never be
classified differently in two places.
"""
from enum import Enum
from typing import Tuple


class JobStatus(str, Enum):
    QUEUED = "QUEUED"
    ACQUIRING = "ACQUIRING"
    ANALYZING = "ANALYZING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
    STALE = "STALE"


class JobPhase(str, Enum):
    NONE = "NONE"
    ACQUISITION = "ACQUISITION"
    SENTIMENT = "SENTIMENT"
    # Sprint 6: topic/discovery warm-up runs after sentiment and before
    # COMPLETE - the UI can show real "discovering discussions" progress.
    TOPIC = "TOPIC"
    # Sprint 7 §25: evidence-based insight warm-up on this thread after
    # topics - the first UI render hits the memo.
    INSIGHT = "INSIGHT"
    COMPLETE = "COMPLETE"


# Non-terminal states: the only ones a live worker may transition FROM and
# the only rows progress/phase writes are allowed to touch.
ACTIVE_JOB_STATUSES: Tuple[str, ...] = (
    JobStatus.QUEUED.value,
    JobStatus.ACQUIRING.value,
    JobStatus.ANALYZING.value,
)

# Terminal states: polling stops here (§26). STALE = interrupted by a
# restart, CANCELLED = superseded by another active video.
TERMINAL_JOB_STATUSES: Tuple[str, ...] = (
    JobStatus.COMPLETED.value,
    JobStatus.FAILED.value,
    JobStatus.CANCELLED.value,
    JobStatus.STALE.value,
)


def is_terminal(status: object) -> bool:
    return str(status) in TERMINAL_JOB_STATUSES
