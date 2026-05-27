import os
import json
import time
import base64
import random
import asyncio
import shutil
from contextlib import asynccontextmanager
from typing import Any, Awaitable, Callable, Optional
import anthropic
import websockets
from fastapi import FastAPI, WebSocket, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.websockets import WebSocketDisconnect
from twilio.twiml.voice_response import VoiceResponse, Connect, Say, Stream
from twilio.request_validator import RequestValidator
from dotenv import load_dotenv

load_dotenv()

# Configuration
OPENAI_API_KEY = os.getenv('OPENAI_API_KEY')
TWILIO_AUTH_TOKEN = os.getenv('TWILIO_AUTH_TOKEN')
PORT = int(os.getenv('PORT', 5050))
TEMPERATURE = float(os.getenv('TEMPERATURE', 0.8))

if not TWILIO_AUTH_TOKEN:
    raise ValueError('Missing the Twilio auth token. Please set TWILIO_AUTH_TOKEN in the .env file.')

_twilio_validator = RequestValidator(TWILIO_AUTH_TOKEN)
print("[security] Twilio request signature validation enabled")

SYSTEM_MESSAGE = (
    "You are a concise, highly-technical voice assistant similar to a senior developer assistant. "
    "Be direct, structured, and solution-oriented. Prefer bullet points, actionable steps, and code examples when useful. Avoid filler.\n"
    "Keep responses succinct and high-signal.\n"
    "You have an `ask_claude` tool that delegates to a Claude Code agent on the same machine, "
    "which has MCP servers loaded (calendar, email, files, etc.). Use it whenever the caller's "
    "question requires looking up their personal data or running real actions.\n"
    "HARD RULE: if the caller says any variant of 'ask Claude', 'have Claude', 'check with Claude', "
    "'tell Claude', or otherwise explicitly directs you to use Claude, you MUST call `ask_claude` "
    "with the rest of their request as the `query`. Do not answer from your own knowledge in that "
    "case, even if you think you know the answer. If the directive is ambiguous about what to ask, "
    "pass the caller's full utterance as the query.\n"
    "The tool can take several seconds — before invoking it, say one short sentence like "
    "'Let me check' so the caller doesn't hear silence. Phrase the `query` as a complete "
    "standalone question.\n"
    "When you first connect, briefly greet the caller and ask what they're working on."
)

CLAUDE_SESSION_FILE = "/tmp/dialaifriend-claude-session-id"
CLAUDE_PID_FILE = "/tmp/dialaifriend-claude.pid"
CLAUDE_CWD = "/srv/dialaifriend"
CLAUDE_TIMEOUT_SECONDS = 90
CLAUDE_STARTUP_PROBE_SECONDS = 0.5
CLAUDE_BIN = os.getenv("CLAUDE_BIN") or shutil.which("claude") or "claude"
print(f"[claude] resolved CLAUDE_BIN={CLAUDE_BIN}")

TOOLS = [
    {
        "type": "function",
        "name": "ask_claude",
        "description": (
            "Ask the Claude Code agent (with MCP tools: calendar, email, files, etc.) "
            "a question on the caller's behalf. Use for anything requiring the caller's "
            "personal data or real actions. Returns Claude's natural-language answer."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "A complete, standalone question phrased for Claude to answer.",
                }
            },
            "required": ["query"],
        },
    }
]

VOICE = 'alloy'
VOICES = ['alloy', 'ash', 'ballad', 'coral', 'echo', 'sage', 'shimmer', 'verse', 'marin', 'cedar']

def _load_phrases(env_var: str, default: str, label: str) -> list[list[str]]:
    raw = os.getenv(env_var, default)
    phrases = [p.strip().lower().split() for p in raw.split(",") if p.strip()]
    print(f"[{label}] loaded {len(phrases)} phrase(s): {[' '.join(p) for p in phrases]}")
    return phrases

RESTART_PHRASES = _load_phrases("RESTART_PHRASES", "deploy yourself,bravo zulu", "restart")
HANGUP_PHRASES  = _load_phrases("HANGUP_PHRASES",  "hang up on yourself",        "hangup")
RESTART_PENDING = False


def _matches_phrases(text: str, phrases: list[list[str]]) -> bool:
    if not text:
        return False
    words = set("".join(c.lower() if c.isalnum() else " " for c in text).split())
    return any(all(w in words for w in phrase) for phrase in phrases)

def _matches_restart_code_word(text: str) -> bool:
    return _matches_phrases(text, RESTART_PHRASES)

def _matches_hangup_phrase(text: str) -> bool:
    return _matches_phrases(text, HANGUP_PHRASES)


def _twilio_public_url(raw_url: str) -> str:
    """Reconstruct the public URL Twilio signed before ngrok/local proxying."""
    if raw_url.startswith("http://"):
        return "https://" + raw_url[len("http://"):]
    if raw_url.startswith("ws://"):
        return "wss://" + raw_url[len("ws://"):]
    return raw_url


def _valid_twilio_signature(raw_url: str, params: dict, signature: str) -> bool:
    return _twilio_validator.validate(_twilio_public_url(raw_url), params, signature)


def _load_claude_session_id() -> str | None:
    try:
        with open(CLAUDE_SESSION_FILE) as f:
            sid = f.read().strip()
            return sid or None
    except FileNotFoundError:
        return None


def _save_claude_session_id(session_id: str) -> None:
    try:
        with open(CLAUDE_SESSION_FILE, "w") as f:
            f.write(session_id)
    except OSError as e:
        print(f"Could not persist Claude session id: {e}")


def _log_claude_event(event: dict) -> None:
    """Render one stream-json event as a concise one-liner."""
    et = event.get("type")
    if et == "system":
        sub = event.get("subtype", "")
        model = event.get("model", "")
        tools = event.get("tools") or []
        mcps = [s.get("name") for s in (event.get("mcp_servers") or [])]
        extras = []
        if model:
            extras.append(f"model={model}")
        if mcps:
            extras.append(f"mcp={mcps}")
        if tools:
            extras.append(f"tools={len(tools)}")
        print(f"[claude:system/{sub}] {' '.join(extras)}")
    elif et == "assistant":
        msg = event.get("message", {}) or {}
        for block in msg.get("content") or []:
            btype = block.get("type")
            if btype == "text":
                text = (block.get("text") or "").strip().replace("\n", " ")
                if text:
                    print(f"[claude:asst] {text[:400]}")
            elif btype == "tool_use":
                name = block.get("name", "?")
                inp = json.dumps(block.get("input") or {}, separators=(",", ":"))
                print(f"[claude:tool→] {name}({inp[:300]})")
            elif btype == "thinking":
                thought = (block.get("thinking") or "").strip().replace("\n", " ")
                if thought:
                    print(f"[claude:think] {thought[:300]}")
    elif et == "user":
        msg = event.get("message", {}) or {}
        for block in msg.get("content") or []:
            if block.get("type") == "tool_result":
                content = block.get("content")
                if isinstance(content, list):
                    content = " ".join(
                        (b.get("text") or "") for b in content if isinstance(b, dict)
                    )
                preview = str(content or "").replace("\n", " ")[:300]
                is_err = " ERR" if block.get("is_error") else ""
                print(f"[claude:tool←{is_err}] {preview}")
    elif et == "result":
        cost = event.get("total_cost_usd", 0) or 0
        dur = event.get("duration_ms", 0) or 0
        turns = event.get("num_turns", 0) or 0
        print(f"[claude:done] turns={turns} dur={dur}ms cost=${cost:.4f}")
    else:
        print(f"[claude:event] {et}")


_anthropic_client: anthropic.AsyncAnthropic | None = None


def _get_anthropic_client() -> anthropic.AsyncAnthropic:
    global _anthropic_client
    if _anthropic_client is None:
        _anthropic_client = anthropic.AsyncAnthropic()
    return _anthropic_client


def _static_phrase_for_tool(name: str) -> str | None:
    """Fallback phrase if the Haiku summarizer fails. None for tools we
    don't want to narrate at all (internal/meta tools)."""
    n = name.lower()
    if "toolsearch" in n.replace("_", ""):
        return None
    if "gmail" in n or "mail" in n:
        if "send" in n:
            return "Sending the email."
        if "draft" in n:
            return "Drafting that email."
        if "get" in n or "content" in n or "read" in n:
            return "Reading that message."
        if "search" in n or "list" in n:
            return "Searching your inbox."
        return "Working on your email."
    if "event" in n or "calendar" in n:
        if any(w in n for w in ("create", "update", "modify", "delete", "move")):
            return "Updating your calendar."
        return "Checking your calendar."
    return None


_PROGRESS_SYSTEM_PROMPT = (
    "You translate a single tool call into a brief, casual spoken status "
    "update to play to a phone caller waiting for an answer. "
    "Rules: under 10 words, present tense, first person, one sentence, "
    "ends with a period, no preamble, no quotes, no labels. Be specific "
    "when the input gives you something concrete (a sender name, a date, "
    "a subject). Examples: 'Checking your calendar for tomorrow.' "
    "'Searching for emails from Roger.' 'Reading that message about the offer.'"
)


async def _summarize_tool_use_for_speech(tool_name: str, tool_input: Any) -> str | None:
    """Ask Haiku for a contextual one-liner describing this tool call.
    Returns None on any failure — caller falls back to the static phrase."""
    if _static_phrase_for_tool(tool_name) is None:
        return None
    try:
        payload = json.dumps(
            {"tool": tool_name, "input": tool_input},
            separators=(",", ":"),
            default=str,
        )[:1500]
        client = _get_anthropic_client()
        response = await asyncio.wait_for(
            client.messages.create(
                model="claude-haiku-4-5",
                max_tokens=40,
                system=_PROGRESS_SYSTEM_PROMPT,
                messages=[{"role": "user", "content": payload}],
            ),
            timeout=4.0,
        )
        text = next(
            (b.text for b in response.content if b.type == "text"), ""
        ).strip().strip('"').strip()
        return text[:120] if text else None
    except Exception as e:
        print(f"[progress] haiku summary failed for {tool_name}: {e}")
        return None


class ClaudeSession:
    """One long-lived `claude --print --input-format stream-json` subprocess.
    Queries are serialized through a lock; each query writes a user message
    to stdin and consumes events from a per-query queue until the next
    `result` event."""

    def __init__(self) -> None:
        self.proc: asyncio.subprocess.Process | None = None
        self.session_id: str | None = _load_claude_session_id()
        self.lock = asyncio.Lock()
        self._event_queue: asyncio.Queue[dict | None] | None = None
        self._stdout_task: asyncio.Task | None = None
        self._stderr_task: asyncio.Task | None = None

    def _is_alive(self) -> bool:
        return self.proc is not None and self.proc.returncode is None

    async def start(self) -> None:
        """Spawn the persistent subprocess. If --resume fails fast, retry fresh."""
        attempts = [True, False] if self.session_id else [False]
        last_err: str | None = None
        for use_resume in attempts:
            cmd = [
                CLAUDE_BIN, "-p",
                "--model", "claude-sonnet-4-6",
                "--input-format", "stream-json",
                "--output-format", "stream-json",
                "--verbose",
                "--permission-mode", "bypassPermissions",
            ]
            if use_resume and self.session_id:
                cmd += ["--resume", self.session_id]
            print(f"[claude] spawning persistent session: {cmd}")
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=CLAUDE_CWD,
            )
            # Brief probe: if claude bails immediately (e.g. bad --resume id),
            # returncode will be set within a moment.
            await asyncio.sleep(CLAUDE_STARTUP_PROBE_SECONDS)
            if proc.returncode is not None:
                err = b""
                try:
                    err = await asyncio.wait_for(proc.stderr.read(), timeout=0.5)
                except asyncio.TimeoutError:
                    pass
                last_err = err.decode(errors="replace")[:400]
                print(f"[claude] startup exited rc={proc.returncode} stderr={last_err}")
                if use_resume:
                    print("[claude] dropping stored session id and retrying fresh")
                    self.session_id = None
                continue
            self.proc = proc
            try:
                with open(CLAUDE_PID_FILE, "w") as f:
                    f.write(str(proc.pid))
            except OSError as e:
                print(f"[claude] could not write pid file: {e}")
            print(f"[claude] persistent session pid={proc.pid}")
            self._event_queue = None
            self._stdout_task = asyncio.create_task(self._read_stdout())
            self._stderr_task = asyncio.create_task(self._read_stderr())
            return
        raise RuntimeError(f"Could not start persistent Claude session: {last_err}")

    async def _read_stdout(self) -> None:
        assert self.proc is not None and self.proc.stdout is not None
        async for raw in self.proc.stdout:
            line = raw.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                print(f"[claude:stdout-raw] {line[:300].decode(errors='replace')}")
                continue
            _log_claude_event(event)
            sid = event.get("session_id")
            if sid and sid != self.session_id:
                self.session_id = sid
                _save_claude_session_id(sid)
            q = self._event_queue
            if q is not None:
                await q.put(event)
        print(f"[claude] stdout EOF; subprocess rc={self.proc.returncode}")
        q = self._event_queue
        if q is not None:
            await q.put(None)

    async def _read_stderr(self) -> None:
        assert self.proc is not None and self.proc.stderr is not None
        async for raw in self.proc.stderr:
            text = raw.rstrip().decode(errors="replace")
            if text:
                print(f"[claude:stderr] {text[:400]}")

    async def _kill(self) -> None:
        if self.proc is not None and self.proc.returncode is None:
            try:
                self.proc.kill()
            except ProcessLookupError:
                pass
            try:
                await asyncio.wait_for(self.proc.wait(), timeout=2)
            except asyncio.TimeoutError:
                pass
        for task in (self._stdout_task, self._stderr_task):
            if task is not None and not task.done():
                task.cancel()
        self._stdout_task = None
        self._stderr_task = None

    async def _ensure_alive(self) -> None:
        if not self._is_alive():
            print("[claude] subprocess gone; respawning")
            await self._kill()
            await self.start()

    async def query(
        self,
        prompt: str,
        on_tool_use: Optional[Callable[[str, Any], Awaitable[None]]] = None,
    ) -> dict:
        async with self.lock:
            await self._ensure_alive()
            assert self.proc is not None and self.proc.stdin is not None
            queue: asyncio.Queue[dict | None] = asyncio.Queue()
            self._event_queue = queue
            user_msg = {
                "type": "user",
                "message": {"role": "user", "content": prompt},
            }
            payload = (json.dumps(user_msg) + "\n").encode()
            try:
                self.proc.stdin.write(payload)
                await self.proc.stdin.drain()
            except (BrokenPipeError, ConnectionResetError) as e:
                print(f"[claude] stdin write failed: {e}")
                self._event_queue = None
                return {"is_error": True, "result": f"Claude stdin closed: {e}"}

            final_event: dict | None = None
            try:
                while True:
                    event = await asyncio.wait_for(
                        queue.get(), timeout=CLAUDE_TIMEOUT_SECONDS
                    )
                    if event is None:
                        return {
                            "is_error": True,
                            "result": "Claude session died mid-query.",
                        }
                    if on_tool_use is not None and event.get("type") == "assistant":
                        msg = event.get("message", {}) or {}
                        for block in msg.get("content") or []:
                            if block.get("type") == "tool_use":
                                try:
                                    await on_tool_use(
                                        block.get("name", ""),
                                        block.get("input") or {},
                                    )
                                except Exception as e:
                                    print(f"[claude] on_tool_use failed: {e}")
                    if event.get("type") == "result":
                        final_event = event
                        break
            except asyncio.TimeoutError:
                print(f"[claude] query TIMEOUT after {CLAUDE_TIMEOUT_SECONDS}s")
                final_event = {"is_error": True, "result": "The lookup timed out."}
            finally:
                self._event_queue = None
            return final_event or {
                "is_error": True,
                "result": "No result event from claude.",
            }


claude_session = ClaudeSession()


async def run_claude_query(
    query: str,
    on_tool_use: Optional[Callable[[str, Any], Awaitable[None]]] = None,
) -> str:
    """Send a query to the persistent Claude Code session."""
    if not query.strip():
        return "Empty query."

    event = await claude_session.query(query, on_tool_use)

    if event.get("is_error"):
        return f"Claude lookup failed: {event.get('result') or 'unknown error'}"

    result = event.get("result")
    if isinstance(result, str) and result.strip():
        return result.strip()
    return "Claude returned no text."
LOG_EVENT_TYPES = [
    'error', 'response.content.done', 'rate_limits.updated',
    'response.done', 'input_audio_buffer.committed',
    'input_audio_buffer.speech_stopped', 'input_audio_buffer.speech_started',
    'session.created', 'session.updated',
    'conversation.item.input_audio_transcription.completed',
    'conversation.item.input_audio_transcription.failed',
]
SHOW_TIMING_MATH = False

@asynccontextmanager
async def lifespan(app: FastAPI):
    await claude_session.start()
    yield
    await claude_session._kill()


app = FastAPI(lifespan=lifespan)

if not OPENAI_API_KEY:
    raise ValueError('Missing the OpenAI API key. Please set it in the .env file.')

@app.get("/", response_class=JSONResponse)
async def index_page():
    return {"message": "Twilio Media Stream Server is running!"}

ALLOWED_PHONE_NUMBERS = [
    "+15555551212",  # 555-555-1212
]

@app.api_route("/incoming-call", methods=["GET", "POST"])
async def handle_incoming_call(request: Request):
    """Handle incoming call and return TwiML response to connect to Media Stream."""
    response = VoiceResponse()

    form_data = await request.form()

    # Reconstruct the URL as Twilio sees it. Ngrok terminates TLS so the
    # ASGI scope has scheme=http even though Twilio called https://.
    signature = request.headers.get("X-Twilio-Signature", "")
    if not _valid_twilio_signature(str(request.url), dict(form_data), signature):
        print(f"[security] Rejected request with invalid Twilio signature from {request.client}")
        return HTMLResponse(content="Forbidden", status_code=403)
    print(f"[security] Twilio signature valid for incoming call from {form_data.get('From', 'unknown')}")

    # Get caller's phone number from Twilio request
    caller_number = form_data.get('From', '')

    # Restrict to allowed phone numbers only
    if caller_number not in ALLOWED_PHONE_NUMBERS:
        print(f"Rejecting call from unauthorized number: {caller_number}")
        response.say("Sorry, this service is not available for your number. Goodbye.")
        response.hangup()
        return HTMLResponse(content=str(response), media_type="application/xml")

    # Pick a random voice for this call
    greeting_voice = random.choice(VOICES)

    # Just connect to the media stream - let the AI do the greeting
    host = request.url.hostname
    connect = Connect()
    # Pass the chosen voice as a query parameter to the WebSocket
    connect.stream(url=f'wss://{host}/media-stream?voice={greeting_voice}')
    response.append(connect)
    return HTMLResponse(content=str(response), media_type="application/xml")

@app.websocket("/media-stream")
async def handle_media_stream(websocket: WebSocket):
    """Handle WebSocket connections between Twilio and OpenAI."""
    signature = websocket.headers.get("x-twilio-signature", "")
    if not _valid_twilio_signature(str(websocket.url), dict(websocket.query_params), signature):
        print(f"[security] Rejected media stream with invalid Twilio signature from {websocket.client}")
        await websocket.close(code=1008)
        return

    print("Client connected")
    await websocket.accept()

    # Extract the voice parameter from the query string
    session_voice = random.choice(VOICES)  # Default fallback
    if websocket.query_params.get("voice") and websocket.query_params["voice"] in VOICES:
        session_voice = websocket.query_params["voice"]
        print(f"Using voice from greeting: {session_voice}")

    async with websockets.connect(
        f"wss://api.openai.com/v1/realtime?model=gpt-realtime&temperature={TEMPERATURE}",
        additional_headers={
            "Authorization": f"Bearer {OPENAI_API_KEY}"
        }
    ) as openai_ws:
        await initialize_session(openai_ws, session_voice)

        # Connection specific state
        stream_sid = None
        latest_media_timestamp = 0
        last_assistant_item = None
        mark_queue = []
        response_start_timestamp_twilio = None
        assistant_text_buffer = ""
        user_transcript_buffer = ""
        
        async def receive_from_twilio():
            """Receive audio data from Twilio and send it to the OpenAI Realtime API."""
            nonlocal stream_sid, latest_media_timestamp
            try:
                async for message in websocket.iter_text():
                    data = json.loads(message)
                    if data['event'] == 'media' and openai_ws.state.name == 'OPEN':
                        latest_media_timestamp = int(data['media']['timestamp'])
                        audio_append = {
                            "type": "input_audio_buffer.append",
                            "audio": data['media']['payload']
                        }
                        await openai_ws.send(json.dumps(audio_append))
                    elif data['event'] == 'start':
                        stream_sid = data['start']['streamSid']
                        print(f"Incoming stream has started {stream_sid}")
                        response_start_timestamp_twilio = None
                        latest_media_timestamp = 0
                        last_assistant_item = None
                    elif data['event'] == 'mark':
                        if mark_queue:
                            mark_queue.pop(0)
            except WebSocketDisconnect:
                print("Client disconnected.")
                if openai_ws.state.name == 'OPEN':
                    await openai_ws.close()

        hangup_pending = False

        async def send_to_twilio():
            """Receive events from the OpenAI Realtime API, send audio back to Twilio."""
            nonlocal stream_sid, last_assistant_item, response_start_timestamp_twilio, assistant_text_buffer, user_transcript_buffer, hangup_pending
            try:
                async for openai_message in openai_ws:
                    response = json.loads(openai_message)
                    if response['type'] in LOG_EVENT_TYPES:
                        print(f"Received event: {response['type']}", response)

                    # Accumulate assistant text output
                    if response.get('type') == 'response.output_text.delta' and 'delta' in response:
                        assistant_text_buffer += response['delta']

                    if response.get('type') == 'response.output_text.done':
                        if assistant_text_buffer.strip():
                            print(f"Assistant: {assistant_text_buffer.strip()}")
                        assistant_text_buffer = ""

                    # Fallback: if response finishes without explicit output_text.done
                    if response.get('type') == 'response.done' and assistant_text_buffer.strip():
                        print(f"Assistant: {assistant_text_buffer.strip()}")
                        assistant_text_buffer = ""

                    # Accumulate caller transcription
                    if response.get('type') == 'conversation.item.input_audio_transcription.delta' and 'delta' in response:
                        user_transcript_buffer += response['delta']

                    if response.get('type') == 'conversation.item.input_audio_transcription.completed':
                        # `completed` carries the full transcript in `transcript`; deltas may or may not fire.
                        final_transcript = response.get('transcript') or user_transcript_buffer
                        if final_transcript and final_transcript.strip():
                            print(f"Caller: {final_transcript.strip()}")
                        if _matches_restart_code_word(final_transcript):
                            global RESTART_PENDING
                            RESTART_PENDING = True
                            print("Restart code word detected; announcing shutdown")
                            await trigger_restart_announcement(openai_ws)
                        elif _matches_hangup_phrase(final_transcript):
                            hangup_pending = True
                            print("Hang-up phrase detected; ending call")
                            await trigger_hangup_announcement(openai_ws)
                        user_transcript_buffer = ""

                    if response.get('type') == 'response.output_audio.delta' and 'delta' in response:
                        audio_payload = base64.b64encode(base64.b64decode(response['delta'])).decode('utf-8')
                        audio_delta = {
                            "event": "media",
                            "streamSid": stream_sid,
                            "media": {
                                "payload": audio_payload
                            }
                        }
                        await websocket.send_json(audio_delta)


                        if response.get("item_id") and response["item_id"] != last_assistant_item:
                            response_start_timestamp_twilio = latest_media_timestamp
                            last_assistant_item = response["item_id"]
                            if SHOW_TIMING_MATH:
                                print(f"Setting start timestamp for new response: {response_start_timestamp_twilio}ms")

                        await send_mark(websocket, stream_sid)

                    if (response.get('type') == 'error' and
                            response.get('error', {}).get('code') == 'session_expired'):
                        print("[openai] session_expired — closing call")
                        await websocket.send_json({"event": "clear", "streamSid": stream_sid})
                        await openai_ws.close()
                        await websocket.close()
                        return

                    # Handle speech events
                    if response.get('type') == 'input_audio_buffer.speech_started':
                        print("Speech started detected.")
                        if last_assistant_item:
                            print(f"Interrupting response with id: {last_assistant_item}")
                            await handle_speech_started_event()
                    
                    elif response.get('type') == 'response.done':
                        if RESTART_PENDING:
                            await asyncio.sleep(2)
                            await openai_ws.close()
                            await websocket.close()
                            return
                        if hangup_pending:
                            await asyncio.sleep(2)
                            await openai_ws.close()
                            await websocket.close()
                            return
                        output_items = response.get('response', {}).get('output', []) or []
                        function_calls = [it for it in output_items if it.get('type') == 'function_call']
                        if function_calls:
                            # The model invoked a tool; run it and let the follow-up response speak the answer.
                            for call in function_calls:
                                asyncio.create_task(handle_function_call(call))
            except Exception as e:
                print(f"Error in send_to_twilio: {e}")

        async def handle_speech_started_event():
            """Handle interruption when the caller's speech starts."""
            nonlocal response_start_timestamp_twilio, last_assistant_item
            print("Handling speech started event.")

            if mark_queue and response_start_timestamp_twilio is not None:
                elapsed_time = latest_media_timestamp - response_start_timestamp_twilio
                if SHOW_TIMING_MATH:
                    print(f"Calculating elapsed time for truncation: {latest_media_timestamp} - {response_start_timestamp_twilio} = {elapsed_time}ms")

                if last_assistant_item:
                    if SHOW_TIMING_MATH:
                        print(f"Truncating item with ID: {last_assistant_item}, Truncated at: {elapsed_time}ms")

                    truncate_event = {
                        "type": "conversation.item.truncate",
                        "item_id": last_assistant_item,
                        "content_index": 0,
                        "audio_end_ms": elapsed_time
                    }
                    await openai_ws.send(json.dumps(truncate_event))

                await websocket.send_json({
                    "event": "clear",
                    "streamSid": stream_sid
                })

                mark_queue.clear()
                last_assistant_item = None
                response_start_timestamp_twilio = None

        async def handle_function_call(call):
            """Execute a tool call from the Realtime model and feed the result back."""
            call_id = call.get("call_id")
            name = call.get("name")
            arguments_raw = call.get("arguments") or "{}"
            print(f"Function call: {name}({arguments_raw})")
            try:
                arguments = json.loads(arguments_raw)
            except json.JSONDecodeError:
                arguments = {}

            if name == "ask_claude":
                last_progress_ts = 0.0
                last_progress_phrase: str | None = None

                async def speak_progress(tool_name: str, tool_input: Any) -> None:
                    nonlocal last_progress_ts, last_progress_phrase
                    # Throttle BEFORE the Haiku call so rapid-fire tool calls
                    # don't all spawn API requests.
                    now = time.monotonic()
                    if now - last_progress_ts < 5.0:
                        return
                    phrase = await _summarize_tool_use_for_speech(tool_name, tool_input)
                    if not phrase:
                        phrase = _static_phrase_for_tool(tool_name)
                    if not phrase:
                        return
                    # Re-check after the await — another tool may have narrated.
                    now = time.monotonic()
                    if now - last_progress_ts < 5.0:
                        return
                    if phrase == last_progress_phrase and now - last_progress_ts < 15.0:
                        return
                    if openai_ws.state.name != "OPEN":
                        return
                    last_progress_ts = now
                    last_progress_phrase = phrase
                    print(f"[progress] {phrase}")
                    await openai_ws.send(json.dumps({
                        "type": "response.create",
                        "response": {
                            "instructions": f"Say exactly, with no preamble: \"{phrase}\"",
                            "output_modalities": ["audio"],
                            "tool_choice": "none",
                        },
                    }))

                async def keepalive_filler() -> None:
                    """Speak a generic 'still working' phrase if no tool-driven
                    narration has fired for a while."""
                    fillers = [
                        "Still working on it.",
                        "One moment, almost there.",
                        "Hang tight.",
                        "Still digging.",
                    ]
                    await asyncio.sleep(15.0)
                    while True:
                        now = time.monotonic()
                        gap_since_last = now - last_progress_ts
                        if gap_since_last < 12.0:
                            await asyncio.sleep(12.0 - gap_since_last)
                            continue
                        if openai_ws.state.name != "OPEN":
                            return
                        phrase = random.choice(fillers)
                        nonlocal_set_progress(now, phrase)
                        print(f"[progress:filler] {phrase}")
                        await openai_ws.send(json.dumps({
                            "type": "response.create",
                            "response": {
                                "instructions": f"Say exactly, with no preamble: \"{phrase}\"",
                                "output_modalities": ["audio"],
                                "tool_choice": "none",
                            },
                        }))
                        await asyncio.sleep(12.0)

                def nonlocal_set_progress(ts: float, phrase: str) -> None:
                    nonlocal last_progress_ts, last_progress_phrase
                    last_progress_ts = ts
                    last_progress_phrase = phrase

                keepalive_task = asyncio.create_task(keepalive_filler())
                try:
                    result = await run_claude_query(arguments.get("query", ""), speak_progress)
                finally:
                    keepalive_task.cancel()
            else:
                result = f"Unknown tool: {name}"

            print(f"Tool result ({name}): {result[:300]}")

            if openai_ws.state.name != "OPEN":
                return

            await openai_ws.send(json.dumps({
                "type": "conversation.item.create",
                "item": {
                    "type": "function_call_output",
                    "call_id": call_id,
                    "output": result,
                },
            }))
            await openai_ws.send(json.dumps({"type": "response.create"}))

        async def send_mark(connection, stream_sid):
            if stream_sid:
                mark_event = {
                    "event": "mark",
                    "streamSid": stream_sid,
                    "mark": {"name": "responsePart"}
                }
                await connection.send_json(mark_event)
                mark_queue.append('responsePart')

        try:
            await asyncio.gather(receive_from_twilio(), send_to_twilio(), return_exceptions=True)
        finally:
            if RESTART_PENDING:
                print("Restart pending; exiting process for supervisor restart")
                os._exit(0)

async def trigger_restart_announcement(openai_ws):
    """Have the model say 'system restarting' so the caller hears it before we exit."""
    # Server VAD auto-creates a response on the caller's "deploy yourself" utterance;
    # cancel it so our announcement isn't rejected with `conversation_already_has_active_response`.
    await openai_ws.send(json.dumps({"type": "response.cancel"}))
    item = {
        "type": "conversation.item.create",
        "item": {
            "type": "message",
            "role": "user",
            "content": [
                {
                    "type": "input_text",
                    "text": "Reply with exactly: 'System restarting.' Do not say anything else.",
                }
            ],
        },
    }
    await openai_ws.send(json.dumps(item))
    await openai_ws.send(json.dumps({"type": "response.create"}))

async def trigger_hangup_announcement(openai_ws):
    """Have the model say goodbye before hanging up."""
    await openai_ws.send(json.dumps({"type": "response.cancel"}))
    item = {
        "type": "conversation.item.create",
        "item": {
            "type": "message",
            "role": "user",
            "content": [
                {
                    "type": "input_text",
                    "text": "Reply with exactly: 'Goodbye.' Do not say anything else.",
                }
            ],
        },
    }
    await openai_ws.send(json.dumps(item))
    await openai_ws.send(json.dumps({"type": "response.create"}))

async def send_initial_conversation_item(openai_ws):
    """Send initial conversation item if AI talks first."""
    initial_conversation_item = {
        "type": "conversation.item.create",
        "item": {
            "type": "message",
            "role": "user",
            "content": [
                {
                    "type": "input_text",
                    "text": "Say exactly: 'Ready to go.' Nothing else."
                }
            ]
        }
    }
    await openai_ws.send(json.dumps(initial_conversation_item))
    await openai_ws.send(json.dumps({"type": "response.create"}))


async def initialize_session(openai_ws, voice=None):
    """Control initial session with OpenAI."""
    if voice is None:
        voice = random.choice(VOICES)

    session_update = {
        "type": "session.update",
        "session": {
            "type": "realtime",
            "model": "gpt-realtime",
            "output_modalities": ["audio"],
            "audio": {
                "input": {
                    "format": {"type": "audio/pcmu"},
                    "turn_detection": {"type": "server_vad"},
                    "transcription": {"model": "whisper-1"}
                },
                "output": {
                    "format": {"type": "audio/pcmu"},
                    "voice": voice
                }
            },
            "instructions": SYSTEM_MESSAGE,
            "tools": TOOLS,
            "tool_choice": "auto",
        }
    }
    print('Sending session update:', json.dumps(session_update))
    await openai_ws.send(json.dumps(session_update))

    # Wait for connection to be fully established before AI speaks
    await asyncio.sleep(2)

    # Have the AI speak first to introduce itself
    await send_initial_conversation_item(openai_ws)

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=PORT)
