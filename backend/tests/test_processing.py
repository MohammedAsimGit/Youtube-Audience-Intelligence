"""Unit tests: processing-status state machine (Sprint 4 hand-off contract)."""
import pytest

from app.models.processing import (
    IllegalTransition,
    ProcessingStatus,
    assert_transition,
    can_transition,
    parse_status,
)


class TestParse:
    def test_parses_valid_strings(self):
        assert parse_status("READY_FOR_ANALYSIS") is ProcessingStatus.READY_FOR_ANALYSIS
        assert parse_status(ProcessingStatus.FAILED) is ProcessingStatus.FAILED

    def test_unknown_state_raises(self):
        with pytest.raises(ValueError):
            parse_status("TOTALLY_MADE_UP")


class TestAllowedTransitions:
    def test_spec_pipeline_path(self):
        # ACQUIRED -> VALIDATED -> NORMALIZED -> READY_FOR_ANALYSIS
        path = [
            ProcessingStatus.ACQUIRED,
            ProcessingStatus.VALIDATED,
            ProcessingStatus.NORMALIZED,
            ProcessingStatus.READY_FOR_ANALYSIS,
        ]
        for current, target in zip(path, path[1:]):
            assert can_transition(current, target), f"{current} -> {target}"

    def test_deduplicated_stage_also_reaches_ready(self):
        assert can_transition(
            ProcessingStatus.DEDUPLICATED, ProcessingStatus.READY_FOR_ANALYSIS
        )

    def test_sprint4_path_reserved(self):
        assert can_transition(
            ProcessingStatus.READY_FOR_ANALYSIS, ProcessingStatus.PROCESSING
        )
        assert can_transition(ProcessingStatus.PROCESSING, ProcessingStatus.PROCESSED)

    def test_failed_reachable_and_requeueable(self):
        assert can_transition(ProcessingStatus.READY_FOR_ANALYSIS, ProcessingStatus.FAILED)
        assert can_transition(ProcessingStatus.FAILED, ProcessingStatus.READY_FOR_ANALYSIS)

    def test_illegal_transitions(self):
        assert not can_transition(ProcessingStatus.ACQUIRED, ProcessingStatus.PROCESSED)
        assert not can_transition(ProcessingStatus.READY_FOR_ANALYSIS, ProcessingStatus.ACQUIRED)
        assert not can_transition(ProcessingStatus.READY_FOR_ANALYSIS, ProcessingStatus.PROCESSED)
        assert not can_transition(ProcessingStatus.PROCESSED, ProcessingStatus.PROCESSING)


class TestAssert:
    def test_illegal_transition_raises_with_both_states(self):
        with pytest.raises(IllegalTransition) as excinfo:
            assert_transition(
                ProcessingStatus.READY_FOR_ANALYSIS, ProcessingStatus.ACQUIRED
            )
        message = str(excinfo.value)
        assert "READY_FOR_ANALYSIS" in message and "ACQUIRED" in message

    def test_legal_transition_does_not_raise(self):
        assert_transition(ProcessingStatus.VALIDATED, ProcessingStatus.NORMALIZED)
