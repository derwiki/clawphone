# ClawPhone — Your Agent on Speed Dial

<img width="1024" height="506" alt="image" src="https://github.com/user-attachments/assets/208d64fe-de54-472a-a33b-48052e173410" />

Call a phone number. Talk to your Claude Code agent. It has your Gmail, Google
Calendar, and any other MCP servers you've configured, live on the call.

Twilio carries the audio. OpenAI's Realtime API handles the conversation.
Claude Code does the actual work. The phone is just the interface.

One more thing: **say "deploy yourself" mid-call and it ships itself** — the
server exits, `make server` pulls from `origin/main`, and it respawns. No
terminal required.

## What you can ask

- "How many emails have I exchanged with Landon in the last 6 months?"
- "What's on my calendar for tomorrow?"
- "Draft an email to Sarah about pushing our 3pm to Thursday."
- "Did Roger reply about the offer yet?"
- "Block out Friday afternoon for focus time."
- "Read me the latest thread with the design team."

Prefix anything with **"ask Claude"** or **"have Claude check"** to force
delegation, but usually you don't need to — the Realtime model hands off
anything personal-data-shaped automatically.

A real example: *"Ask Claude, how many emails have I exchanged with Landon in
the last six months?"* → *"47 messages with landon@gmail.com since November
26, 2025."*

## Demo

**Calendar lookup:**

https://github.com/user-attachments/assets/3394c5e3-3946-4ea4-a35d-320346b4057f

**Shipping itself mid-call:**

https://github.com/user-attachments/assets/4ac3d1c4-ba62-449b-8906-50c91bed05e3

## Architecture

```
  ┌──────────────┐
  │ caller phone │
  └──────┬───────┘
         │ PSTN
         ▼
  ┌──────────────┐  WebSocket           ┌──────────────────────────────────────────────────┐
  │    Twilio    │  mu-law 8 kHz audio  │               main.py  (FastAPI)                 │
  │    Voice     │◄────────────────────►│                                                  │
  └──────────────┘                      │  ┌───────────────────────────────────────────┐   │
                                        │  │         OpenAI Realtime API               │   │
  ┌──────────────┐  WebSocket           │  │         GPT-4o-realtime · voice           │   │
  │    OpenAI    │◄────────────────────►│  │                                           │   │
  │    Realtime  │                      │  │  one tool:  ask_claude(query)             │   │
  └──────────────┘                      │  └───────────────────────┬───────────────────┘   │
                                        │                          │                        │
                                        │                          ▼                        │
                                        │  ┌───────────────────────────────────────────┐   │
                                        │  │         ClaudeSession                     │   │
                                        │  │         claude -p  --model sonnet-4-6     │   │
                                        │  │         persistent subprocess             │   │
                                        │  └───────────────────────┬───────────────────┘   │
                                        │                          │ MCP tool calls         │
                                        │                          ▼                        │
                                        │  ┌───────────────────────────────────────────┐   │
                                        │  │         MCP servers                       │   │
                                        │  │         Gmail  ·  Google Calendar  ·  …   │   │
                                        │  └───────────────────────────────────────────┘   │
                                        │                                                  │
                                        │  ┌───────────────────────────────────────────┐   │
                                        │  │  Haiku (Anthropic API)                    │   │
                                        │  │  narrates tool progress during long calls  │   │
                                        │  └───────────────────────────────────────────┘   │
                                        └──────────────────────────────────────────────────┘
```

The Realtime model has exactly one tool: `ask_claude(query)`. When it calls
that tool, the FastAPI server pipes the query into a long-lived Claude Code
subprocess and streams the answer back. The Realtime model speaks the answer
over the phone.

### Persistent Claude subprocess

The server holds a single persistent Claude Code subprocess for the lifetime
of the FastAPI process, spawned at startup with:

```
claude -p --model claude-sonnet-4-6 \
       --input-format stream-json --output-format stream-json --verbose \
       --permission-mode bypassPermissions
```

Queries are fed turn-by-turn over stdin, results streamed back over stdout.
The session id is persisted to `/tmp/dialaifriend-claude-session-id` so a
restart can `--resume`. If the subprocess dies mid-call it auto-respawns on
the next query.

This keeps MCP servers warm and shaves ~1–2 seconds off every turn versus
spawning a fresh subprocess each time.

### In-call progress narration

Claude tool calls can take several seconds. While one is in flight, a Haiku
model summarizes each MCP tool invocation into a brief spoken status update —
"Searching for emails from Roger.", "Checking your calendar for tomorrow." —
so the caller doesn't sit in silence.

### Per-number allowlist

This is a single-user personal assistant, not a multi-tenant product. Set
`ALLOWED_PHONE_NUMBERS` in `.env` (comma-separated E.164 format); any other
caller gets a polite rejection and a hangup.

### Self-deploying via voice

Say **"deploy yourself"** (or **"bravo zulu"** if you're somewhere noisy)
during a call and the server announces the restart, exits cleanly, and comes
back up on the latest `origin/main`. `make server` runs a supervisor loop
that handles the pull and respawn automatically. It's the only deployment
method you need once it's running.

### Webhook security

Every Twilio webhook is validated against `TWILIO_AUTH_TOKEN` before
processing. Requests with an invalid or missing signature are rejected with
403. This closes the obvious hole where anyone who knows your ngrok URL could
forge a call.

## Prerequisites

- **Python 3.9+** and **[uv](https://github.com/astral-sh/uv)**
- **A Twilio account** with a Voice-capable phone number ([sign up](https://www.twilio.com/try-twilio))
- **An OpenAI API key** with Realtime API access
- **An Anthropic API key** — used by the Haiku progress narrator
- **The `claude` CLI**, installed, authenticated, and configured with your MCP servers. See the [Claude Code docs](https://docs.claude.com/en/docs/claude-code/overview) and the [MCP setup guide](https://docs.claude.com/en/docs/claude-code/mcp).
- **ngrok** (or another tunnel) so Twilio can reach your local server

## Setup

1. **Open an ngrok tunnel** to port 5050:

   ```
   ngrok http 5050
   ```

2. **Install dependencies:**

   ```
   uv sync
   ```

3. **Configure `.env`:**

   ```
   cp .env.example .env
   ```

   Fill in all four values: `OPENAI_API_KEY`, `ANTHROPIC_API_KEY`,
   `TWILIO_AUTH_TOKEN`, and `ALLOWED_PHONE_NUMBERS` (your number in E.164
   format, e.g. `+15551234567`).

4. **Configure Twilio.** In the [Twilio Console](https://console.twilio.com/),
   go to **Phone Numbers → Manage → Active Numbers**, pick your number, and
   set the **A call comes in** webhook to:
   ```
   https://<your-ngrok-subdomain>.ngrok.app/incoming-call
   ```

## Run

```
make server
```

This runs the server in a supervisor loop that `git pull`s from `origin/main`
and restarts on exit — required for the in-call self-deploy. For one-shot
local dev:

```
uv run python main.py
```

## Credits

Originally forked from Twilio Labs'
[Speech Assistant with Twilio Voice and the OpenAI Realtime API](https://github.com/twilio-labs/speech-assistant-openai-realtime-api-python).
Rebuilt around Claude Code and MCP; the Twilio/OpenAI wiring is the part
that's still recognizable.
