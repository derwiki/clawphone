import os
import sys
from urllib.parse import urlparse

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("OPENAI_API_KEY", "test-openai-key")
os.environ.setdefault("TWILIO_AUTH_TOKEN", "test-twilio-auth-token")

import main


class FakeUrl:
    def __init__(self, raw_url: str):
        self.raw_url = raw_url
        self.hostname = urlparse(raw_url).hostname

    def __str__(self):
        return self.raw_url


class FakeRequest:
    def __init__(self, raw_url: str, form_data: dict, headers: dict):
        self.url = FakeUrl(raw_url)
        self._form_data = form_data
        self.headers = headers
        self.client = "testclient"

    async def form(self):
        return self._form_data


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
    "text,expected",
    [
        ("hang up on yourself", True),
        ("Please hang up on yourself now.", True),
        ("hang yourself up", True),
        ("Hang Yourself Up!", True),
        ("hotel uniform", True),
        ("Hotel Uniform, over.", True),
        ("hang up", False),
        ("hotel california", False),
        ("just hotel", False),
        ("", False),
        (None, False),
    ],
)
def test_matches_hangup_phrase_variants(text, expected):
    assert main._matches_hangup_phrase(text) is expected


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


async def _twiml(form_from: str) -> str:
    form_data = {"From": form_from}
    url = main._twilio_public_url("http://testserver/incoming-call")
    signature = main._twilio_validator.compute_signature(url, form_data)
    resp = await main.handle_incoming_call(
        FakeRequest(
            "http://testserver/incoming-call",
            form_data,
            {"X-Twilio-Signature": signature},
        )
    )
    assert resp.status_code == 200
    return resp.body.decode()


async def test_incoming_call_rejects_invalid_signature():
    resp = await main.handle_incoming_call(
        FakeRequest(
            "http://testserver/incoming-call",
            {"From": "+15555551212"},
            {"X-Twilio-Signature": "invalid"},
        )
    )

    assert resp.status_code == 403


async def test_incoming_call_rejects_unknown_number():
    body = await _twiml("+15555550100")
    assert "<Hangup" in body
    assert "<Say" in body
    assert "<Stream" not in body


async def test_incoming_call_accepts_allowed_number():
    body = await _twiml("+15555551212")
    assert "<Stream" in body
    assert "wss://" in body
    assert "/media-stream" in body
    assert "<Hangup" not in body


def test_media_stream_signature_validation_uses_wss_public_url():
    raw_url = "ws://testserver/media-stream?voice=alloy"
    params = {"voice": "alloy"}
    signature = main._twilio_validator.compute_signature(
        "wss://testserver/media-stream?voice=alloy",
        params,
    )

    assert main._valid_twilio_signature(raw_url, params, signature) is True
    assert main._valid_twilio_signature(raw_url, params, "invalid") is False
