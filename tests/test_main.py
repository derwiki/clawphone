import os
import sys

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import main


@pytest.mark.parametrize(
    "text,expected",
    [
        ("deploy yourself", True),
        ("Please go ahead and deploy yourself now.", True),
        ("DEPLOY YOURSELF!", True),
        ("deploy,   yourself", True),
        ("bravo zulu", True),
        ("Bravo Zulu, team.", True),
        ("deploying yourself", False),
        ("do not deploy", False),
        ("just bravo", False),
        ("", False),
        (None, False),
    ],
)
def test_matches_restart_code_word_variants(text, expected):
    assert main._matches_restart_code_word(text) is expected


@pytest.mark.parametrize(
    "tool_name,expected",
    [
        ("ToolSearch", None),
        ("tool_search", None),
        ("mcp__google-workspace__send_gmail_message", "Sending the email."),
        ("mcp__google-workspace__draft_gmail_message", "Drafting that email."),
        ("mcp__google-workspace__get_gmail_message_content", "Reading that message."),
        ("mcp__google-workspace__search_gmail_messages", "Searching your inbox."),
        ("mcp__google-workspace__get_events", "Checking your calendar."),
        ("mcp__google-workspace__create_calendar", "Updating your calendar."),
        ("mcp__joplin__search_notes", None),
    ],
)
def test_static_phrase_for_tool(tool_name, expected):
    assert main._static_phrase_for_tool(tool_name) == expected


def test_claude_session_id_roundtrip(tmp_path, monkeypatch):
    session_file = tmp_path / "sid"
    monkeypatch.setattr(main, "CLAUDE_SESSION_FILE", str(session_file))

    assert main._load_claude_session_id() is None

    main._save_claude_session_id("abc-123")
    assert main._load_claude_session_id() == "abc-123"

    session_file.write_text("   \n")
    assert main._load_claude_session_id() is None


async def test_run_claude_query_empty_short_circuits():
    assert await main.run_claude_query("") == "Empty query."
    assert await main.run_claude_query("   ") == "Empty query."


@pytest.fixture
def no_twilio_signature(monkeypatch):
    """Disable Twilio webhook signature validation so route tests can
    exercise the phone-number allowlist without a real signed request."""
    monkeypatch.setattr(main, "_twilio_validator", None)


def _twiml(form_from: str) -> str:
    client = TestClient(main.app)
    resp = client.post("/incoming-call", data={"From": form_from})
    assert resp.status_code == 200
    return resp.text


def test_incoming_call_rejects_unknown_number(no_twilio_signature):
    body = _twiml("+15555550100")
    assert "<Hangup" in body
    assert "<Say" in body
    assert "<Stream" not in body


def test_incoming_call_accepts_allowed_number(no_twilio_signature):
    body = _twiml("+15555551212")
    assert "<Stream" in body
    assert "wss://" in body
    assert "/media-stream" in body
    assert "<Hangup" not in body
