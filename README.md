# DialAiFriend - AI Voice Assistant for Toddlers

A toddler-focused AI voice assistant that provides engaging phone conversations with multiple personality-driven AI voices. Built with Twilio Voice, OpenAI Realtime API, and Python.

**Based on the original project:** [Speech Assistant with Twilio Voice and the OpenAI Realtime API](https://github.com/twilio-labs/speech-assistant-openai-realtime-api-python) by Twilio Labs.

## Features

🎭 **10 Unique AI Personalities** - Each voice has distinct characteristics, interests, and relationships with other voices
- Alloy (calm and steady), Ash (creative and artistic), Ballad (musical and rhythmic)
- Coral (bubbly and energetic), Echo (helpful and organized), Sage (wise and gentle)
- Shimmer (sweet and caring), Verse (poetic and thoughtful), Marin (adventurous), Cedar (strong and protective)

🗣️ **Toddler-Optimized Conversations**
- Asks specific, simple questions that 4-year-olds can easily answer
- Shares things about itself to create natural, two-way conversations
- Automatically switches topics when kids say "I dunno" or seem confused
- Keeps conversations broad and varied (no deep dives into single topics)

⏰ **Smart Silence Detection**
- Automatically detects when toddlers go quiet for 15 seconds
- Jumps in with engaging questions to keep the conversation flowing
- Won't interrupt itself when speaking

📞 **Phone-Based Interaction**
- Simple phone call interface - no apps or screens needed
- Real-time voice conversations with low latency
- Works with any phone number through Twilio

## How It Works

This application demonstrates how to use Python, [Twilio Voice](https://www.twilio.com/docs/voice) and [Media Streams](https://www.twilio.com/docs/voice/media-streams), and [OpenAI's Realtime API](https://platform.openai.com/docs/) to create an AI voice assistant specifically designed for toddlers.

The application opens websockets with the OpenAI Realtime API and Twilio, and sends voice audio from one to the other to enable a two-way conversation.

See [here](https://www.twilio.com/en-us/blog/voice-ai-assistant-openai-realtime-api-python) for a tutorial overview of the underlying code.

This application uses the following Twilio products in conjunction with OpenAI's Realtime API:
- Voice (and TwiML, Media Streams)
- Phone Numbers


## Prerequisites

To use the app, you will need:

- **Python 3.9+** We used `3.9.13` for development; download from [here](https://www.python.org/downloads/).
- **uv package manager** - Fast Python package installer. Install from [here](https://github.com/astral-sh/uv).
- **A Twilio account.** You can sign up for a free trial [here](https://www.twilio.com/try-twilio).
- **A Twilio number with _Voice_ capabilities.** [Here are instructions](https://help.twilio.com/articles/223135247-How-to-Search-for-and-Buy-a-Twilio-Phone-Number-from-Console) to purchase a phone number.
- **An OpenAI account and an OpenAI API Key.** You can sign up [here](https://platform.openai.com/).
  - **OpenAI Realtime API access.**

## Local Setup

There are 4 required steps and 1 optional step to get the app up-and-running locally for development and testing:
1. Run ngrok or another tunneling solution to expose your local server to the internet for testing. Download ngrok [here](https://ngrok.com/).
2. (optional) Create and use a virtual environment
3. Install the packages
4. Twilio setup
5. Update the .env file

### Open an ngrok tunnel
When developing & testing locally, you'll need to open a tunnel to forward requests to your local development server. These instructions use ngrok.

Open a Terminal and run:
```
ngrok http 5050
```
Once the tunnel has been opened, copy the `Forwarding` URL. It will look something like: `https://[your-ngrok-subdomain].ngrok.app`. You will
need this when configuring your Twilio number setup.

Note that the `ngrok` command above forwards to a development server running on port `5050`, which is the default port configured in this application. If
you override the `PORT` defined in `main.py`, you will need to update the `ngrok` command accordingly.

Keep in mind that each time you run the `ngrok http` command, a new URL will be created, and you'll need to update it everywhere it is referenced below.

### (Optional) Create and use a virtual environment

uv automatically manages virtual environments for you. If you want to create a specific virtual environment, you can run:

```
uv venv
source .venv/bin/activate
```

Or simply use uv commands directly without activating - uv will handle the virtual environment automatically.

### Install required packages

In the terminal (with the virtual environment, if you set it up) run:
```
uv sync
```

### Twilio setup

#### Point a Phone Number to your ngrok URL
In the [Twilio Console](https://console.twilio.com/), go to **Phone Numbers** > **Manage** > **Active Numbers** and click on the additional phone number you purchased for this app in the **Prerequisites**.

In your Phone Number configuration settings, update the first **A call comes in** dropdown to **Webhook**, and paste your ngrok forwarding URL (referenced above), followed by `/incoming-call`. For example, `https://[your-ngrok-subdomain].ngrok.app/incoming-call`. Then, click **Save configuration**.

### Update the .env file

Create a `.env` file, or copy the `.env.example` file to `.env`:

```
cp .env.example .env
```

In the .env file, update the `OPENAI_API_KEY` to your OpenAI API key from the **Prerequisites**.

## Run the app
Once ngrok is running, dependencies are installed, Twilio is configured properly, and the `.env` is set up, run the dev server with the following command:
```
uv run python main.py
```

Or use the included server script:
```
./server.sh
```

## Test the app
With the development server running, call the phone number you purchased in the **Prerequisites**. After the AI personality introduces itself, you should be able to have a natural conversation with the AI Assistant. Each call will randomly select one of the 10 personality voices to chat with your toddler!

## Special Features

### Toddler-Optimized Conversation Flow
The AI personalities are specifically designed for 4-year-olds:
- Start with specific questions like "What did you do today? Did you play with any toys?"
- Share things about themselves to create natural conversations
- Switch topics quickly when kids seem confused or say "I dunno"
- Use simple, concrete language and give examples

### Smart Silence Detection
- Automatically detects when toddlers go quiet for 15 seconds
- Jumps in with engaging questions to keep conversations flowing
- Won't interrupt itself when speaking
- Continuously monitors throughout the entire conversation

### Interrupt handling/AI preemption
When the user speaks and OpenAI sends `input_audio_buffer.speech_started`, the code will clear the Twilio Media Streams buffer and send OpenAI `conversation.item.truncate`.

Depending on your application's needs, you may want to use the [`input_audio_buffer.speech_stopped`](https://platform.openai.com/docs/api-reference/realtime-server-events/input-audio-buffer-speech-stopped) event, instead, or a combination of the two.

## Credits

This project is based on the excellent work by [Twilio Labs](https://github.com/twilio-labs) and their [Speech Assistant with Twilio Voice and the OpenAI Realtime API](https://github.com/twilio-labs/speech-assistant-openai-realtime-api-python) project.