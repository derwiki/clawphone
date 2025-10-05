import os
import json
import base64
import random
import asyncio
import websockets
from fastapi import FastAPI, WebSocket, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.websockets import WebSocketDisconnect
from twilio.twiml.voice_response import VoiceResponse, Connect, Say, Stream
from dotenv import load_dotenv
from voice_personalities import create_personality_prompt, get_personality

load_dotenv()

# We'll generate greetings dynamically with voice names

# Configuration
OPENAI_API_KEY = os.getenv('OPENAI_API_KEY')
PORT = int(os.getenv('PORT', 5050))
TEMPERATURE = float(os.getenv('TEMPERATURE', 0.8))
def create_system_message(voice_name):
    personality = get_personality(voice_name)
    personality_prompt = create_personality_prompt(voice_name)

    return (
        f"{personality_prompt}\n\n"
        "When you first connect, wait 2 seconds before introducing yourself by saying "
        f"'Hi Elodie, this is {personality['name']}. {personality['personality'].split('.')[0]}.' "
        "Then ask them a specific question like 'What did you eat today?' or, 'did you play with any toys?', or 'did you play with anyone today?'"
        "Keep it brief and friendly, then wait for their response.\n\n"
        "DEVELOPER MODE CONTROL:\n"
        "- Activation pass phrase: if the caller says 'Foxtrot Oscar Tango' (any case), immediately say exactly: 'entering developer mode' and switch to Developer Mode for the remainder of the call.\n"
        "- In Developer Mode: drop toddler constraints and adopt a concise, highly-technical assistant persona similar to a senior ChatGPT developer assistant. Be direct, structured, and solution-oriented; prefer bullet points, actionable steps, and code examples when useful; avoid filler.\n"
        "- While in Developer Mode: assume the caller is the developer of this phone-based chat app; keep responses succinct and high-signal.\n"
        "- When developer mode is active, ignore toddler conversation rules and use adult language and content appropriate for software engineers."
    )
VOICE = 'alloy'
VOICES = ['alloy', 'ash', 'ballad', 'coral', 'echo', 'sage', 'shimmer', 'verse', 'marin', 'cedar']
LOG_EVENT_TYPES = [
    'error', 'response.content.done', 'rate_limits.updated',
    'response.done', 'input_audio_buffer.committed',
    'input_audio_buffer.speech_stopped', 'input_audio_buffer.speech_started',
    'session.created', 'session.updated'
]
SHOW_TIMING_MATH = False

app = FastAPI()

if not OPENAI_API_KEY:
    raise ValueError('Missing the OpenAI API key. Please set it in the .env file.')

@app.get("/", response_class=JSONResponse)
async def index_page():
    return {"message": "Twilio Media Stream Server is running!"}

@app.api_route("/incoming-call", methods=["GET", "POST"])
async def handle_incoming_call(request: Request):
    """Handle incoming call and return TwiML response to connect to Media Stream."""
    response = VoiceResponse()

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
            nonlocal stream_sid, last_assistant_item, response_start_timestamp_twilio, silence_timeout_task
            try:
                async for openai_message in openai_ws:
                    response = json.loads(openai_message)
                    if response['type'] in LOG_EVENT_TYPES:
                        print(f"Received event: {response['type']}", response)

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
                        print("AI finished speaking - restarting silence timeout")
                        # AI finished speaking, restart the timeout to wait for toddler's response
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
            """Handle when the toddler has been silent for too long."""
            nonlocal silence_timeout_task, last_speech_stopped_timestamp
            print("Silence timeout - toddler hasn't spoken for 15 seconds")
            
            # Send a conversation item to fill the silence
            silence_filler_item = {
                "type": "conversation.item.create",
                "item": {
                    "type": "message",
                    "role": "user",
                    "content": [
                        {
                            "type": "input_text",
                            "text": "The caller has been quiet for a while. If you are currently in Developer Mode (activated by the caller saying 'Foxtrot Oscar Tango'), continue in Developer Mode: ask a concise, high-signal, technical clarifying question or propose next actionable steps related to the most recent topic. Keep responses succinct and professional. Otherwise (toddler mode), say something engaging to get a 4-year-old talking again, like asking about their favorite color, toys, or pets. Keep it light and fun!"
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

        async def send_mark(connection, stream_sid):
            if stream_sid:
                mark_event = {
                    "event": "mark",
                    "streamSid": stream_sid,
                    "mark": {"name": "responsePart"}
                }
                await connection.send_json(mark_event)
                mark_queue.append('responsePart')

        await asyncio.gather(receive_from_twilio(), send_to_twilio())

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
                    "text": "Say hello and introduce yourself"
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

    # Create personalized system message for this voice
    system_message = create_system_message(voice)

    session_update = {
        "type": "session.update",
        "session": {
            "type": "realtime",
            "model": "gpt-realtime",
            "output_modalities": ["audio"],
            "audio": {
                "input": {
                    "format": {"type": "audio/pcmu"},
                    "turn_detection": {"type": "server_vad"}
                },
                "output": {
                    "format": {"type": "audio/pcmu"},
                    "voice": voice
                }
            },
            "instructions": system_message,
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
