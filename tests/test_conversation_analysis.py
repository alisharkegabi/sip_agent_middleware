"""
Unit tests for the post-call analysis fetcher.

No network: every test replaces AnalysisFetcher.fetch_once (the only method
that talks to ElevenLabs) with a canned sequence of responses.
"""
from __future__ import annotations

import dataclasses
import os
import sys
import threading
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config import Settings  # noqa: E402
from conversation_analysis import AnalysisFetcher, extract_analysis  # noqa: E402


DONE_RECORD = {
    "conversation_id": "conv_test",
    "agent_id": "agent_test",
    "branch_id": "agtbrch_test",
    "version_id": "ver_test",
    "status": "done",
    "metadata": {"call_duration_secs": 42, "termination_reason": "client", "cost": 137},
    "transcript": [{"role": "agent", "message": "..."}],
    "analysis": {
        "call_successful": "success",
        "call_success_score": 0.85,
        "transcript_summary": "Customer promised to pay.",
        "call_summary_title": "Payment promise",
        "evaluation_criteria_results": {
            "identity_verified": {
                "criteria_id": "identity_verified",
                "result": "success",
                "rationale": "Customer confirmed their name.",
                "score": 1,
                "max_score": 1,
            }
        },
        "data_collection_results": {
            "PaymentDate": {
                "data_collection_id": "PaymentDate",
                "value": "2026-08-20",
                "rationale": "Customer said next Thursday.",
                "json_schema": {"type": "string"},
            }
        },
    },
}


def _settings(**overrides) -> Settings:
    base = Settings()
    defaults = {
        "elevenlabs_api_key": "test-key",
        "fetch_conversation_analysis": True,
        "analysis_poll_interval_seconds": 1.0,
        "analysis_max_wait_seconds": 10.0,
        "log_conversation_json": False,
    }
    defaults.update(overrides)
    return dataclasses.replace(base, **defaults)


# ----------------------------------------------------------------- extract
def test_extract_pulls_evaluation_and_data_collection():
    out = extract_analysis(DONE_RECORD)

    assert out["evaluation_criteria_results"]["identity_verified"]["result"] == "success"
    assert out["data_collection_results"]["PaymentDate"]["value"] == "2026-08-20"
    assert out["data_collection_results"]["PaymentDate"]["type"] == "string"
    # Flat convenience map for the .NET client.
    assert out["data_collection"] == {"PaymentDate": "2026-08-20"}
    assert out["call_successful"] == "success"
    assert out["branch_id"] == "agtbrch_test"
    assert out["call_duration_secs"] == 42


def test_extract_omits_the_transcript():
    """The transcript is customer speech; it must not ride along in the
    payload sent to the client or stored on the session."""
    assert "transcript" not in extract_analysis(DONE_RECORD)


def test_extract_tolerates_a_record_with_no_analysis_block():
    out = extract_analysis({"conversation_id": "c", "status": "failed"})
    assert out["evaluation_criteria_results"] == {}
    assert out["data_collection"] == {}
    assert out["call_successful"] is None


# ----------------------------------------------------------------- fetcher
def _wait_for(event: threading.Event, seconds: float = 8.0) -> bool:
    return event.wait(timeout=seconds)


def test_fetch_polls_until_the_record_is_done():
    fetcher = AnalysisFetcher(_settings())
    fetcher._poll_interval = 0.05  # keep the test fast
    responses = [
        {"conversation_id": "conv_test", "status": "processing"},
        {"conversation_id": "conv_test", "status": "processing"},
        DONE_RECORD,
    ]
    calls: list[str] = []

    def fake_fetch_once(conversation_id):
        calls.append(conversation_id)
        return responses[min(len(calls) - 1, len(responses) - 1)]

    fetcher.fetch_once = fake_fetch_once
    got: list[dict] = []
    done = threading.Event()

    fetcher.fetch_async(
        call_id="call1",
        conversation_id="conv_test",
        on_result=lambda a: (got.append(a), done.set()),
    )

    assert _wait_for(done), "callback never fired"
    assert len(calls) == 3
    assert got[0]["data_collection"]["PaymentDate"] == "2026-08-20"
    fetcher.shutdown()


def test_fetch_gives_up_after_max_wait_without_calling_back():
    fetcher = AnalysisFetcher(_settings(analysis_max_wait_seconds=0.3))
    fetcher._poll_interval = 0.05
    fetcher.fetch_once = lambda conversation_id: {"status": "processing"}
    got: list[dict] = []

    fetcher.fetch_async(call_id="call2", conversation_id="conv_test", on_result=got.append)
    time.sleep(1.0)

    assert got == []
    fetcher.shutdown()


def test_a_failed_record_is_still_reported():
    """status="failed" is terminal: report what there is instead of polling
    until the deadline."""
    fetcher = AnalysisFetcher(_settings())
    fetcher._poll_interval = 0.05
    fetcher.fetch_once = lambda conversation_id: {"conversation_id": "c", "status": "failed"}
    done = threading.Event()
    got: list[dict] = []

    fetcher.fetch_async(
        call_id="call3", conversation_id="c", on_result=lambda a: (got.append(a), done.set())
    )

    assert _wait_for(done, 2.0)
    assert got[0]["status"] == "failed"
    fetcher.shutdown()


def test_transient_fetch_errors_are_retried():
    """A 404 right after the call (ElevenLabs still indexing) surfaces as
    None from fetch_once and must not abandon the fetch."""
    fetcher = AnalysisFetcher(_settings())
    fetcher._poll_interval = 0.05
    attempts = {"n": 0}

    def flaky(conversation_id):
        attempts["n"] += 1
        return None if attempts["n"] < 3 else DONE_RECORD

    fetcher.fetch_once = flaky
    done = threading.Event()
    fetcher.fetch_async(call_id="call4", conversation_id="c", on_result=lambda a: done.set())

    assert _wait_for(done, 3.0)
    assert attempts["n"] == 3
    fetcher.shutdown()


@pytest.mark.parametrize(
    "overrides,conversation_id",
    [
        ({"fetch_conversation_analysis": False}, "conv_test"),  # feature off
        ({"elevenlabs_api_key": ""}, "conv_test"),              # no credentials
        ({}, None),                                             # call never reached ElevenLabs
    ],
)
def test_fetch_is_a_no_op_when_it_cannot_or_should_not_run(overrides, conversation_id):
    fetcher = AnalysisFetcher(_settings(**overrides))
    called: list[str] = []
    fetcher.fetch_once = lambda cid: called.append(cid)

    fetcher.fetch_async(call_id="call5", conversation_id=conversation_id, on_result=lambda a: None)
    time.sleep(0.2)

    assert called == []
    fetcher.shutdown()


def test_full_record_is_dumped_when_the_flag_is_on(tmp_path):
    fetcher = AnalysisFetcher(_settings(log_conversation_json=True, log_dir=str(tmp_path)))
    fetcher._poll_interval = 0.05
    fetcher.fetch_once = lambda cid: DONE_RECORD
    done = threading.Event()

    fetcher.fetch_async(call_id="call6", conversation_id="conv_test", on_result=lambda a: done.set())
    assert _wait_for(done, 2.0)

    dumped = list((tmp_path / "conversations").glob("call6_conv_test.json"))
    assert len(dumped) == 1
    assert "PaymentDate" in dumped[0].read_text(encoding="utf-8")
    fetcher.shutdown()
