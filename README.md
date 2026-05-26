# ClawPhone — A Phone Frontend for Your Personal Claude Code Agent

A voice assistant you call on the phone. The other end of the line is **your own
Claude Code agent**, with your MCP servers (Gmail, Google Calendar, etc.) loaded
and ready. Twilio carries the audio, OpenAI's Realtime API handles the
conversational speech, and Claude Code does the actual work against your
personal data.

The Realtime model is the voice UI. Claude Code is the brain.

## What you can ask it

Anything you'd normally pull out your phone for. The whole point is hands-free
access to your inbox and calendar while driving, walking, cooking, or just
not wanting to look at a screen.

- "How many emails have I exchanged with Landon in the last 6 months?"
- "What's on my calendar for tomorrow?"
- "Ask Claude to draft an email to Sarah about pushing our 3pm to Thursday."
- "Did Roger reply about the offer yet?"
- "Find the last email from my landlord."
- "Block out Friday afternoon for focus time."
- "Read me the latest thread with the design team."
- "When's my next meeting with the design team?"

You can prefix a request with **"ask Claude"**, **"have Claude check"**, or
**"tell Claude to..."** to force the delegation, but usually you don't need to
— the Realtime model defaults to handing anything personal-data-shaped to
Claude on its own.

A real call log from today: *"Ask Claude, how many emails have I exchanged with
Landon in the last six months?"* came back with *"47 messages with
landon@kbadvisors.com since November 26, 2025."* That's the shape of query
this excels at.

## Architecture

```
  caller's phone
       │
       ▼
     Twilio Voice  ──── Media Streams (mu-law 8kHz) ────┐
                                                        ▼
                                              FastAPI (main.py)
                                                ▲          │
                                                │          │ websocket
                                                │          ▼
                                       ask_claude     OpenAI Realtime API
                                                │
                                                ▼
                              persistent `claude -p` subprocess
                                                │
                                                ▼
                                MCP servers: Gmail, Calendar, ...
```

The Realtime model has exactly one tool: `ask_claude(query)`. When it calls
that tool, the FastAPI server pipes the query into a long-lived Claude Code
subprocess and streams the answer back. The Realtime model then speaks the
answer over the phone.

### Persistent Claude subprocess

The server holds a **single persistent Claude Code subprocess** for the
lifetime of the FastAPI process. It's spawned at startup with:

```
claude -p --input-format stream-json --output-format stream-json --verbose \
       --permission-mode bypassPermissions
```

Queries are fed turn-by-turn over stdin, results streamed back over stdout.
The pid is written to `/tmp/dialaifriend-claude.pid` and the session id is
persisted to `/tmp/dialaifriend-claude-session-id` so a restart can
`--resume`. If the subprocess dies mid-call it auto-respawns on the next
query. See the `ClaudeSession` class in `main.py` (around line 231).

This keeps MCP servers warm and shaves ~1–2 seconds off every turn versus
spawning a fresh subprocess each time.

### In-call progress narration

Claude tool calls can take several seconds. While one is in flight, a small
Haiku model summarizes each MCP tool invocation into a brief spoken status
update — "Searching for emails from Roger.", "Checking your calendar for
tomorrow." — so the caller doesn't sit in silence.

### Per-number allowlist

This is a single-user personal assistant, not a multi-tenant product.
`ALLOWED_PHONE_NUMBERS` in `main.py` is the allowlist; any other caller gets
a polite rejection and a hangup.

### "Deploy yourself" hot reload

If you say **"deploy yourself"** during a call, the server cleanly exits at
the end of the turn. The `make server` supervisor loop pulls latest from
`origin/main` and respawns, so an in-call code word is enough to deploy a
new version.

## Prerequisites

- **Python 3.9+** and the **[uv](https://github.com/astral-sh/uv)** package
  manager.
- **A Twilio account** with a Voice-capable phone number. Sign up
  [here](https://www.twilio.com/try-twilio).
- **An OpenAI API key** with Realtime API access.
- **An Anthropic API key** (`ANTHROPIC_API_KEY`) — used by the Haiku
  progress narrator.
- **The `claude` CLI**, installed, authenticated, and configured with the
  MCP servers you want exposed (Gmail, Google Calendar, etc.). See the
  [Claude Code docs](https://docs.claude.com/en/docs/claude-code/overview)
  and the [MCP setup guide](https://docs.claude.com/en/docs/claude-code/mcp).
- **ngrok** (or another tunnel) so Twilio can reach your local server.

## Local setup

1. **Open an ngrok tunnel** to port 5050:

   ```
   ngrok http 5050
   ```

   Copy the `https://...ngrok.app` URL.

2. **Install dependencies:**

   ```
   uv sync
   ```

3. **Configure Twilio.** In the [Twilio Console](https://console.twilio.com/),
   go to **Phone Numbers → Manage → Active Numbers**, pick your number, and
   under **A call comes in** set the webhook to
   `https://<your-ngrok-subdomain>.ngrok.app/incoming-call`.

4. **Add your number to the allowlist.** Edit `ALLOWED_PHONE_NUMBERS` in
   `main.py` to include the phone(s) you'll be calling from.

5. **Set up `.env`:**

   ```
   cp .env.example .env
   ```

   Fill in `OPENAI_API_KEY` and `ANTHROPIC_API_KEY`.

## Run the app

```
make server
```

This runs `main.py` in a supervisor loop that `git pull`s from `origin/main`
and restarts on exit — required for the in-call "deploy yourself" hot
reload. For one-shot local dev you can also just run:

```
uv run python main.py
```

Then call your Twilio number. The assistant greets you briefly and asks what
you're working on.

## Interrupt handling

When you start speaking, the Realtime API emits
`input_audio_buffer.speech_started` and the server clears the Twilio Media
Streams buffer and sends `conversation.item.truncate`, so the assistant
stops talking immediately and listens.

## Credits

Originally forked from Twilio Labs' excellent
[Speech Assistant with Twilio Voice and the OpenAI Realtime API](https://github.com/twilio-labs/speech-assistant-openai-realtime-api-python).
Since then it's been rebuilt around Claude Code and MCP; the Twilio/OpenAI
wiring at the bottom is the part that's still recognizable.
