import os
import json
import base64
import random
import asyncio
import shutil
import websockets
from fastapi import FastAPI, WebSocket, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.websockets import WebSocketDisconnect
from twilio.twiml.voice_response import VoiceResponse, Connect, Say, Stream
from dotenv import load_dotenv

load_dotenv()

# Configuration
OPENAI_API_KEY = os.getenv('OPENAI_API_KEY')
PORT = int(os.getenv('PORT', 5050))
TEMPERATURE = float(os.getenv('TEMPERATURE', 0.8))

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
CLAUDE_CWD = "/srv/dialaifriend"
CLAUDE_TIMEOUT_SECONDS = 90
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

RESTART_CODE_WORD = "deploy yourself"
RESTART_PENDING = False


def _matches_restart_code_word(text: str) -> bool:
    if not text:
        return False
    normalized = "".join(c.lower() if c.isalnum() or c.isspace() else " " for c in text)
    return RESTART_CODE_WORD in " ".join(normalized.split())


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


async def _invoke_claude(query: str, session_id: str | None) -> tuple[int, bytes, bytes]:
    cmd = [
        CLAUDE_BIN, "-p",
        "--output-format", "json",
        "--permission-mode", "bypassPermissions",
    ]
    if session_id:
        cmd += ["--resume", session_id]
    cmd.append(query)
    print(f"[claude] invoking: {cmd}")
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=CLAUDE_CWD,
    )
    try:
        stdout, stderr = await asyncio.wait_for(
            proc.communicate(), timeout=CLAUDE_TIMEOUT_SECONDS
        )
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        print(f"[claude] TIMEOUT after {CLAUDE_TIMEOUT_SECONDS}s")
        return -1, b"", b"timeout"
    print(f"[claude] returncode={proc.returncode} stdout={len(stdout)}B stderr={len(stderr)}B")
    if stderr:
        print(f"[claude] --- full stderr ---\n{stderr.decode(errors='replace')}\n[claude] --- end stderr ---")
    if proc.returncode != 0:
        print(f"[claude] --- full stdout ---\n{stdout.decode(errors='replace')}\n[claude] --- end stdout ---")
    return proc.returncode, stdout, stderr


def _summarize_claude_error(stderr_bytes: bytes, stdout_bytes: bytes) -> str:
    """Pull a meaningful one-liner out of Claude's noisy stderr/stdout for the caller."""
    text = (stderr_bytes + b"\n" + stdout_bytes).decode(errors="replace")
    # graceful-fs and similar bundled noise: drop lines that look like minified source.
    lines = [
        ln.strip() for ln in text.splitlines()
        if ln.strip()
        and not ln.lstrip().startswith("GFS4")
        and "graceful-fs" not in ln
        and "console.error" not in ln
        and not (len(ln) > 200 and ln.count("{") + ln.count(";") > 5)
    ]
    for ln in lines:
        low = ln.lower()
        if "error" in low or "fatal" in low or "exception" in low or "enoent" in low:
            return ln[:400]
    return (lines[-1] if lines else "unknown error")[:400]


async def run_claude_query(query: str) -> str:
    """Send a query to Claude Code in headless mode; resume prior session if any."""
    if not query.strip():
        return "Empty query."

    session_id = _load_claude_session_id()
    returncode, stdout, stderr = await _invoke_claude(query, session_id)

    # If resuming failed (e.g. stale session id), retry without resume.
    if returncode != 0 and session_id:
        print(f"Claude --resume failed (rc={returncode}); retrying without resume")
        returncode, stdout, stderr = await _invoke_claude(query, None)

    if returncode == -1:
        return "The lookup timed out."
    if returncode != 0:
        err = _summarize_claude_error(stderr, stdout)
        print(f"[claude] failure summary returned to model: {err!r}")
        return f"Claude lookup failed: {err}"

    try:
        data = json.loads(stdout)
    except json.JSONDecodeError:
        return stdout.decode(errors="replace")[:500].strip() or "No response from Claude."

    new_session = data.get("session_id")
    if new_session:
        _save_claude_session_id(new_session)

    result = data.get("result")
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

app = FastAPI()

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

    # Get caller's phone number from Twilio request
    form_data = await request.form()
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
        silence_timeout_task = None
        last_speech_stopped_timestamp = None
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

        async def send_to_twilio():
            """Receive events from the OpenAI Realtime API, send audio back to Twilio."""
            nonlocal stream_sid, last_assistant_item, response_start_timestamp_twilio, silence_timeout_task, assistant_text_buffer, user_transcript_buffer
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
                        user_transcript_buffer = ""

                    if response.get('type') == 'response.output_audio.delta' and 'delta' in response:
                        # Cancel silence timeout when AI starts speaking
                        if silence_timeout_task:
                            silence_timeout_task.cancel()
                            silence_timeout_task = None
                        
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

                    # Handle speech events
                    if response.get('type') == 'input_audio_buffer.speech_started':
                        print("Speech started detected.")
                        if last_assistant_item:
                            print(f"Interrupting response with id: {last_assistant_item}")
                            await handle_speech_started_event()
                    
                    elif response.get('type') == 'input_audio_buffer.speech_stopped':
                        print("Speech stopped detected - starting silence timeout")
                        await start_silence_timeout()
                    
                    elif response.get('type') == 'response.done':
                        if RESTART_PENDING:
                            # Let the "system restarting" audio drain to Twilio, then drop the call
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
                        else:
                            print("AI finished speaking - restarting silence timeout")
                            # AI finished speaking, restart the timeout to wait for caller's response
                            await start_silence_timeout()
            except Exception as e:
                print(f"Error in send_to_twilio: {e}")

        async def handle_speech_started_event():
            """Handle interruption when the caller's speech starts."""
            nonlocal response_start_timestamp_twilio, last_assistant_item, silence_timeout_task, last_speech_stopped_timestamp
            print("Handling speech started event.")
            
            # Cancel any pending silence timeout and clear the timestamp
            if silence_timeout_task:
                silence_timeout_task.cancel()
                silence_timeout_task = None
            last_speech_stopped_timestamp = None
            
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

        async def handle_silence_timeout():
            """Handle when the caller has been silent for too long."""
            nonlocal silence_timeout_task, last_speech_stopped_timestamp
            print("Silence timeout - caller hasn't spoken for 15 seconds")

            # Send a conversation item to fill the silence
            silence_filler_item = {
                "type": "conversation.item.create",
                "item": {
                    "type": "message",
                    "role": "user",
                    "content": [
                        {
                            "type": "input_text",
                            "text": "The caller has been quiet for a while. Ask a concise, high-signal clarifying question or propose next actionable steps related to the most recent topic. Keep it succinct and professional."
                        }
                    ]
                }
            }
            await openai_ws.send(json.dumps(silence_filler_item))
            await openai_ws.send(json.dumps({"type": "response.create"}))
            
            # Clear the timeout task but keep the timestamp - we'll restart timeout after AI finishes speaking
            silence_timeout_task = None

        async def start_silence_timeout():
            """Start a 10-second timeout for silence detection."""
            nonlocal silence_timeout_task, last_speech_stopped_timestamp
            last_speech_stopped_timestamp = latest_media_timestamp
            
            if silence_timeout_task:
                silence_timeout_task.cancel()
            
            async def timeout_wrapper():
                await asyncio.sleep(15)  # Wait 15 seconds
                # Check if we're still in silence (no new speech started)
                if last_speech_stopped_timestamp is not None:
                    await handle_silence_timeout()
            
            silence_timeout_task = asyncio.create_task(timeout_wrapper())

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
                result = await run_claude_query(arguments.get("query", ""))
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
                    "text": "Say exactly: 'I'm ready.' Nothing else."
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
