# CuePilot

A teleprompter that floats on top of Google Meet, Zoom, Microsoft Teams or any other window on Windows, with an AI assistant that can write, rewrite and answer questions while you're on the call.

## Start it

You need Windows 10 or 11 and Python 3.10 or newer (from python.org, with "Add python.exe to PATH" ticked).

1. Put all the files in one folder.
2. Double-click `run.bat`. The first run installs the packages it needs into a local `.venv` folder (a minute or two). After that it opens straight away. Listening mode also needs `soundcard` and `numpy`, which `run.bat` installs for you.

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

**Claude on Google Cloud (Vertex AI).** If you'd rather use Claude through Google Cloud, pick **Claude on Google Cloud (Vertex AI)**. Google Cloud doesn't use API keys for this; it uses your Google sign-in (called Application Default Credentials). One-time setup:

1. Install the Google Cloud CLI from cloud.google.com/sdk/docs/install.
2. Open Command Prompt and run `gcloud auth application-default login`, then sign in in the browser window that opens.
3. In the Google Cloud console, choose your project, enable the **Vertex AI API**, then open **Model Garden**, search for Claude, and enable the model you want. This needs a billing account on the project.
4. In CuePilot, enter your **Project ID**, leave **Region** as `global`, and paste the exact **model ID** shown in Model Garden. Click **Test connection**.

CuePilot refreshes the sign-in automatically. If it ever expires, run the `gcloud auth application-default login` command again.

Model names change over time, so check your provider's documentation if a model isn't found. Press **Test connection** to check everything works.

What it can do:

- **Write script**: describe the talk or paste rough notes, pick a length and tone, and it writes a script ready for reading aloud. Ctrl+Z in the editor brings back the previous script.
- **Rewrite**: select part of the script (or nothing, for the whole script) and polish, shorten, simplify, make it conversational, translate to Hindi, or give your own instruction.
- **Ask during the call**: press Ctrl+Alt+A, type a question and press Enter. You get a few short talking points, which you can drop onto the prompter right below the line you're reading, or have added automatically.

API keys are stored in plain text in `%APPDATA%\CuePilot\settings.json`. Untick **Remember key on this PC** to keep the key only for the current session, or leave the key field blank and set the `ANTHROPIC_API_KEY`, `OPENAI_API_KEY` or `GEMINI_API_KEY` environment variable instead.

## Listen and answer

CuePilot can listen to the call, show a live transcript, flag the questions people ask, and draft an answer for each one on the prompter. Open the **AI assistant** tab and find **Listen and answer** near the bottom.

**Before you use it:** tick the box confirming everyone on the call knows they're being transcribed. Recording or transcribing people without telling them is illegal in some places, including Germany. You're responsible for getting consent, and listening won't start until you confirm it.

Pick how the speech is turned into text:

| Transcription | What you need |
|---|---|
| Groq (free, cloud) | A free key from console.groq.com. Fast and accurate. The default. |
| OpenAI (cloud) | Your OpenAI key. Uses `gpt-4o-mini-transcribe`. |
| Offline (faster-whisper) | `pip install faster-whisper`. Runs on your PC, so no audio leaves the machine. Needs more processor power and a one-off model download. |
| Custom | Any server with an `/audio/transcriptions` endpoint. |

The cloud options send the call audio to that service for transcription only. If that's a concern, use the offline option. This transcription key is separate from the AI key you already set up; the AI key still writes the answers.

How to use it:

1. Click **Start listening** (or press Ctrl+Alt+L). The level meter shows it's hearing audio.
2. What the other person says appears in the transcript box.
3. A moment after they stop, CuePilot asks your AI whether they asked a question worth answering. Real questions are marked ❓ in amber; small talk, filler and rhetorical questions are ignored.
4. **Tap a question** to draft an answer, which appears in the answer box above. From there, use **Insert at reading line** or **Add to end** to put it on the prompter.
5. Or tick **Answer questions automatically** so every detected question gets an answer without a tap.

### How it decides what to answer

CuePilot doesn't just match question marks. A moment after the other person stops talking, it sends the last few lines of their speech to your AI and asks whether there's a real question you're expected to answer. That means it understands indirect questions ("I'd love to hear about your Shield experience"), handles questions spread over several sentences, and skips small talk and rhetorical questions. The trade-off is a small, steady stream of AI calls while you're listening, and a second or two of delay before a question is flagged. This uses the same AI key you set up at the top of the tab, not the transcription key.

Answers use your script plus the recent conversation as context, so a follow-up like "and how does that scale?" is answered against what was just discussed, not in a vacuum.

### Whose voice gets answered

CuePilot only answers the other person, never you. It captures two separate streams: the call audio from your speakers (the other person) and, optionally, your microphone (you). Because it knows which stream each phrase came from, it answers the other person's questions and never your own. Your lines still appear in the transcript, greyed out and prefixed "you", so you can follow the whole conversation.

**Also capture my microphone** is optional, and you can usually leave it off. CuePilot hears the other person straight from your speakers, so it doesn't need your mic to work. Turn it on only if you want your own lines in the transcript, or if your own voice keeps showing up as the other person (some calls echo you back).

On speakers with the mic on, the mic also overhears the other person. CuePilot recognises that from the timing: the overheard copy arrives at the same moment as the speakers' copy, so it's dropped and the question is still answered. Your own voice echoed back by a call arrives later, after a network delay, so that copy is the one dropped. Headphones avoid the overlap entirely.

It uses Windows loopback capture, which needs Windows 10 or 11.

## Reference document

Upload a document and CuePilot will show the most relevant passage from it, in your document's own words, whenever the other person asks something. It never paraphrases your material; it only picks which passage to show. Find **Reference document** at the bottom of the **AI assistant** tab.

Supported files: PDF, Word (.docx), and plain text or markdown.

**How it finds the right passage.** Three levels, best first, each falling back to the next:

| Search | Needs | What it does |
|---|---|---|
| Contextual | an embeddings key (e.g. Voyage AI) + your Claude key | Before matching, it adds a one-line note to each passage saying where it sits in the document, so an isolated paragraph about "throughput under load" still matches a question about "scaling". On by default. |
| Smart (semantic) | an embeddings key | Matches by meaning, so different words for the same idea still match. |
| Keyword | nothing | Matches shared words. Free, offline, instant. Always available as a fallback. |

Smart and contextual search use an embeddings API. Anthropic doesn't make an embeddings model and recommends Voyage AI, so that's the default (`voyage-4-lite`; get a key at voyageai.com). OpenAI's and Gemini's embeddings work too. Set it under **Smart search** in the document section. The document is embedded once when you upload it, which takes a few seconds; after that each question is one tiny lookup. Contextual search also makes one small call per passage to your main AI when the document loads, to write those context notes. Without an embeddings key, keyword search still works.

How to use it:

1. Click **Upload document** and choose your file.
2. Wait a moment while it's prepared (keyword search is ready at once; smart search finishes a few seconds later).
3. When a question is detected, the best-matching passage appears in the box, with its section heading, and goes onto the prompter automatically. Untick **Show the matching passage on the prompter automatically** if you'd rather tap **Show on prompter** yourself.

### Document and AI together

The document and the AI answer work side by side. The prompter shows your document's own exact wording to speak from; the answer box beside it shows a refined AI answer phrased for speaking.

Everything runs in parallel for speed. The instant it detects a question, CuePilot:

1. shows the best keyword passage on the prompter straight away — no network, so it appears in a fraction of a second;
2. starts the AI answer at the same time, without waiting for the lookup;
3. runs the smart (semantic) lookup alongside, which upgrades the shown passage a moment later and, if the document has a good match, refines the answer to be grounded in that passage.

CuePilot judges whether the document actually contains the answer:

- **When the document has a good match:** the passage is on the prompter immediately, and the AI answer in the side panel is grounded in your material. The panel notes "Grounded in your document."
- **When nothing in the document fits:** the document panel says "nothing closely matching," and the AI answers from your script and general knowledge instead, so you still get a usable answer.

So you're reading your own material almost instantly, the answers never wait on each other, and the document is the starting point whenever it has the answer.

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
| Ctrl+Alt+L | Start or stop listening to the call |
| Ctrl+Alt+P | Open the control panel |

On the prompter itself: Space, arrow keys, Page Up/Down and Home work when it has focus; the mouse wheel scrolls by hand; Ctrl+wheel changes text size; double-click starts or pauses; drag to move, drag an edge to resize, right-click for more options.

## Self-test

`python cuepilot.py --selftest` (or `CuePilot.exe --selftest --report selftest.txt`) opens the real windows and checks around 260 things on its own: drawing, scrolling, every setting and shortcut, click-through, hiding from screen sharing, the AI features, question detection, listening mode and document search against fake servers on your PC, saving files and settings. It uses a temporary folder, so your own script and settings aren't touched, and nothing is sent to the internet.

Each line of the report says PASS, WARN or FAIL. WARN means the check depends on the machine (for example, a build server without a visible screen can't take a real screenshot); FAIL means something in CuePilot is broken. The GitHub workflow runs the self-test on the Python code and again on the finished exe, and stops the build if anything fails. The reports are saved with each run under **Artifacts → selftest-reports**.

## Troubleshooting

**The app closes straight away or nothing happens.** Look in `%APPDATA%\CuePilot\error.log` and send me what it says.


**A shortcut does nothing.** Another program has claimed it. The Shortcuts tab lists any that couldn't be registered. Intel graphics utilities often take Ctrl+Alt+arrow keys (they rotate the screen); turn those hotkeys off in the Intel control panel and restart CuePilot.

**The prompter goes behind the meeting window.** CuePilot puts itself back on top every second and a half. If a meeting app still covers it in full-screen mode, use the meeting maximised rather than full screen.

**"Could not reach..." errors from the AI.** Check your internet connection. On a company network, a proxy or firewall may block AI services; Python picks up the Windows proxy settings automatically, but some company networks still need IT to allow the address.

**Installing PySide6 fails behind a company proxy.** Run `.venv\Scripts\python.exe -m pip install --proxy http://your-proxy:port -r requirements.txt` from the CuePilot folder.

Settings and the last script live in `%APPDATA%\CuePilot`. Delete that folder to reset everything.
