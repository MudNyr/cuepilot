# CuePilot

A teleprompter that floats on top of Google Meet, Zoom, Microsoft Teams or any other window on Windows, with an AI assistant that can write, rewrite and answer questions while you're on the call.

## Start it

You need Windows 10 or 11 and Python 3.10 or newer (from python.org, with "Add python.exe to PATH" ticked).

1. Put all the files in one folder.
2. Double-click `run.bat`. The first run installs PySide6 into a local `.venv` folder (about a minute). After that it opens straight away.

Prefer a single program file? Double-click `build_exe.bat`. It produces `dist\CuePilot.exe` (around 60 to 90 MB, because Qt is bundled inside). Copy it anywhere and run it without Python. Windows SmartScreen may warn that the app is unrecognised, since it isn't code-signed; choose "More info" and then "Run anyway".

## Using it on a call

1. Paste your script into the editor on the left. The prompter updates as you type.
2. Click **Place under webcam**. The prompter moves to the top centre of the screen, so your eyes stay near the camera while you read. A narrow prompter (about a third of the screen) keeps your gaze steadier.
3. Join your call, then press **Ctrl+Alt+Space** to start. Speed it up or slow it down with **Ctrl+Alt+Up / Down**. These shortcuts work while the meeting window has focus.
4. Turn on **click-through** (Ctrl+Alt+T) if the prompter sits over meeting buttons you need. Clicks then pass straight through it. Turn it off again to move or resize the prompter.

The amber band is the reading line; read the line inside it. The thin bar along the bottom shows how far through the script you are, and a red dot appears in the corner while it's scrolling.

### Screen sharing

With **Hide CuePilot from screen sharing and recordings** on (the default), others won't see the prompter or the control panel even if you share your whole screen. This needs Windows 10 version 2004 or later; older versions show a black box instead. Sharing only a single window or a browser tab never shows CuePilot either way.

## AI assistant

Open the **AI assistant** tab and choose a provider:

| Provider | What you need |
|---|---|
| Anthropic (Claude) | An API key from console.anthropic.com. Default model `claude-sonnet-5-5`; `claude-haiku-5-5` is faster for quick answers. |
| OpenAI | An API key from platform.openai.com. |
| Google Gemini | An API key from aistudio.google.com. |
| Ollama (local, free) | Install Ollama, run `ollama pull llama3.2`. No key, works offline. |
| Custom | Any OpenAI-compatible server (LM Studio, Groq, OpenRouter...). Enter its base URL, usually ending in `/v1`. |

Model names change over time, so check your provider's documentation if a model isn't found. Press **Test connection** to check everything works.

What it can do:

- **Write script**: describe the talk or paste rough notes, pick a length and tone, and it writes a script ready for reading aloud. Ctrl+Z in the editor brings back the previous script.
- **Rewrite**: select part of the script (or nothing, for the whole script) and polish, shorten, simplify, make it conversational, translate to Hindi, or give your own instruction.
- **Ask during the call**: press Ctrl+Alt+A, type a question and press Enter. You get a few short talking points, which you can drop onto the prompter right below the line you're reading, or have added automatically.

API keys are stored in plain text in `%APPDATA%\CuePilot\settings.json`. Untick **Remember key on this PC** to keep the key only for the current session, or leave the key field blank and set the `ANTHROPIC_API_KEY`, `OPENAI_API_KEY` or `GEMINI_API_KEY` environment variable instead.

## Writing scripts

- Anything in square brackets, like `[PAUSE]`, `[SMILE]` or `[NEXT SLIDE]`, shows in amber as a stage cue.
- Wrap a word in `**double asterisks**` to underline it for emphasis.
- Each line in the editor is a line on the prompter; leave blank lines between paragraphs.
- Your script is saved automatically and reopens next time. Use Open and Save (Ctrl+O / Ctrl+S) for `.txt` or `.md` files.

## Shortcuts

Anywhere in Windows:

| Keys | Action |
|---|---|
| Ctrl+Alt+Space | Start or pause |
| Ctrl+Alt+Up / Down | Faster or slower |
| Ctrl+Alt+Page Up / Page Down | Jump back or ahead |
| Ctrl+Alt+= / - | Larger or smaller text |
| Ctrl+Alt+R | Back to the top |
| Ctrl+Alt+H | Show or hide the prompter |
| Ctrl+Alt+T | Click-through on or off |
| Ctrl+Alt+A | Jump to the AI question box |
| Ctrl+Alt+P | Open the control panel |

On the prompter itself: Space, arrow keys, Page Up/Down and Home work when it has focus; the mouse wheel scrolls by hand; Ctrl+wheel changes text size; double-click starts or pauses; drag to move, drag an edge to resize, right-click for more options.

## Troubleshooting

**A shortcut does nothing.** Another program has claimed it. The Shortcuts tab lists any that couldn't be registered. Intel graphics utilities often take Ctrl+Alt+arrow keys (they rotate the screen); turn those hotkeys off in the Intel control panel and restart CuePilot.

**The prompter goes behind the meeting window.** CuePilot puts itself back on top every second and a half. If a meeting app still covers it in full-screen mode, use the meeting maximised rather than full screen.

**"Could not reach..." errors from the AI.** Check your internet connection. On a company network, a proxy or firewall may block AI services; Python picks up the Windows proxy settings automatically, but some company networks still need IT to allow the address.

**Installing PySide6 fails behind a company proxy.** Run `.venv\Scripts\python.exe -m pip install --proxy http://your-proxy:port -r requirements.txt` from the CuePilot folder.

Settings and the last script live in `%APPDATA%\CuePilot`. Delete that folder to reset everything.
