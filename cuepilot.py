#!/usr/bin/env python3
"""
CuePilot - an always-on-top teleprompter overlay for video calls on Windows
(Google Meet, Zoom, Microsoft Teams, Webex ...) with a built-in AI writing assistant.

    pip install PySide6
    python cuepilot.py

Features
  * Frameless, semi-transparent window that floats above every other window
  * Smooth scrolling with adjustable speed, reading line, mirror mode, progress bar
  * Global hotkeys (work while the meeting window has focus)
  * Click-through mode so the overlay never blocks the meeting controls
  * Can hide itself from screen sharing and recordings (Windows 10 2004+)
  * AI assistant: draft a script, rewrite a selection, ask quick questions mid-call
    Providers: Anthropic Claude, OpenAI, Google Gemini, Ollama (local), any OpenAI-compatible server
"""
from __future__ import annotations

import copy
import ctypes
import datetime
import html
import http.client
import json
import os
import re
import socket
import sys
import threading
import traceback
import urllib.error
import urllib.parse
import urllib.request
from functools import reduce
from operator import or_
from pathlib import Path

try:
    import cuepilot_listen as listen
except Exception:  # pragma: no cover - only if the file is missing
    listen = None

try:
    import cuepilot_docs as docs
except Exception:  # pragma: no cover - only if the file is missing
    docs = None

from PySide6.QtCore import (QByteArray, QElapsedTimer, QObject, QPoint, QPointF,
                            QRectF, Qt, QTimer, Signal)
from PySide6.QtGui import (QAbstractTextDocumentLayout, QColor, QFont, QFontMetricsF,
                           QGuiApplication, QIcon, QKeySequence, QPainter, QPainterPath,
                           QPalette, QPen, QPixmap, QShortcut, QTextBlockFormat, QTextCharFormat,
                           QTextCursor, QTextDocument)
from PySide6.QtWidgets import (QApplication, QCheckBox, QColorDialog, QComboBox,
                               QFileDialog, QFontComboBox, QFormLayout, QFrame,
                               QGridLayout, QHBoxLayout, QLabel, QLineEdit, QMainWindow,
                               QMenu, QPlainTextEdit, QPushButton, QScrollArea, QSlider,
                               QSpinBox, QSplitter, QTabWidget, QToolButton, QVBoxLayout,
                               QWidget)

APP_NAME = "CuePilot"
APP_VERSION = "1.0"
IS_WINDOWS = sys.platform == "win32"

# Palette: a studio control room. Slate surfaces, an amber cue light, a red tally for "live".
BG = "#161C27"
SURFACE = "#1F2734"
SURFACE_2 = "#283241"
BORDER = "#354255"
TEXT = "#E7EBF1"
MUTED = "#8E9AAC"
AMBER = "#F2B33D"
TALLY = "#E5484D"
INK = "#1A1306"

# =============================================================================
# Windows native helpers
# =============================================================================
WDA_NONE = 0x00
WDA_MONITOR = 0x01
WDA_EXCLUDEFROMCAPTURE = 0x11
WM_HOTKEY = 0x0312
WM_QUIT = 0x0012
SWP_NOSIZE, SWP_NOMOVE, SWP_NOACTIVATE = 0x0001, 0x0002, 0x0010
GWL_EXSTYLE, WS_EX_TRANSPARENT, WS_EX_LAYERED = -20, 0x00000020, 0x00080000
MOD_ALT, MOD_CONTROL, MOD_SHIFT, MOD_NOREPEAT = 0x0001, 0x0002, 0x0004, 0x4000

if IS_WINDOWS:
    from ctypes import wintypes

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    user32.SetWindowDisplayAffinity.argtypes = [wintypes.HWND, wintypes.DWORD]
    user32.SetWindowDisplayAffinity.restype = wintypes.BOOL
    user32.SetWindowPos.argtypes = [wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int,
                                    ctypes.c_int, ctypes.c_int, wintypes.UINT]
    user32.SetWindowPos.restype = wintypes.BOOL
    user32.RegisterHotKey.argtypes = [wintypes.HWND, ctypes.c_int, wintypes.UINT, wintypes.UINT]
    user32.RegisterHotKey.restype = wintypes.BOOL
    user32.UnregisterHotKey.argtypes = [wintypes.HWND, ctypes.c_int]
    user32.UnregisterHotKey.restype = wintypes.BOOL
    user32.GetMessageW.argtypes = [ctypes.POINTER(wintypes.MSG), wintypes.HWND,
                                   wintypes.UINT, wintypes.UINT]
    user32.GetMessageW.restype = wintypes.BOOL
    user32.PostThreadMessageW.argtypes = [wintypes.DWORD, wintypes.UINT,
                                          wintypes.WPARAM, wintypes.LPARAM]
    user32.PostThreadMessageW.restype = wintypes.BOOL
    user32.GetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int]
    user32.GetWindowLongW.restype = wintypes.LONG
    user32.SetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int, wintypes.LONG]
    user32.SetWindowLongW.restype = wintypes.LONG
    kernel32.GetCurrentThreadId.restype = wintypes.DWORD
    HWND_TOPMOST = wintypes.HWND(-1)


def set_capture_excluded(hwnd: int, on: bool) -> str:
    """Hide a window from screen capture. Returns hidden / blacked / visible / failed / unsupported."""
    if not IS_WINDOWS:
        return "unsupported"
    if not on:
        user32.SetWindowDisplayAffinity(hwnd, WDA_NONE)
        return "visible"
    if user32.SetWindowDisplayAffinity(hwnd, WDA_EXCLUDEFROMCAPTURE):
        return "hidden"
    if user32.SetWindowDisplayAffinity(hwnd, WDA_MONITOR):  # older Windows 10: shows as a black box
        return "blacked"
    return "failed"


def set_native_click_through(hwnd: int, on: bool) -> None:
    """Backs up Qt's WindowTransparentForInput flag at the Win32 level."""
    if not IS_WINDOWS:
        return
    ex = user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
    new = (ex | WS_EX_TRANSPARENT | WS_EX_LAYERED) if on else (ex & ~WS_EX_TRANSPARENT)
    if new != ex:
        user32.SetWindowLongW(hwnd, GWL_EXSTYLE, new)


def force_topmost(hwnd: int) -> None:
    if IS_WINDOWS:
        user32.SetWindowPos(hwnd, HWND_TOPMOST, 0, 0, 0, 0, SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE)


# name, label, modifiers, virtual key, description
_CA = MOD_CONTROL | MOD_ALT
HOTKEYS = [
    ("toggle_play", "Ctrl+Alt+Space", _CA | MOD_NOREPEAT, 0x20, "Start or pause scrolling"),
    ("faster", "Ctrl+Alt+Up", _CA, 0x26, "Scroll faster"),
    ("slower", "Ctrl+Alt+Down", _CA, 0x28, "Scroll slower"),
    ("back", "Ctrl+Alt+Page Up", _CA, 0x21, "Jump back a few lines"),
    ("forward", "Ctrl+Alt+Page Down", _CA, 0x22, "Jump ahead a few lines"),
    ("bigger", "Ctrl+Alt+=", _CA, 0xBB, "Larger text"),
    ("smaller", "Ctrl+Alt+-", _CA, 0xBD, "Smaller text"),
    ("restart", "Ctrl+Alt+R", _CA | MOD_NOREPEAT, 0x52, "Back to the top"),
    ("toggle_overlay", "Ctrl+Alt+H", _CA | MOD_NOREPEAT, 0x48, "Show or hide the prompter"),
    ("toggle_clickthrough", "Ctrl+Alt+T", _CA | MOD_NOREPEAT, 0x54, "Turn click-through on or off"),
    ("ask", "Ctrl+Alt+A", _CA | MOD_NOREPEAT, 0x41, "Jump to the AI question box"),
    ("toggle_listen", "Ctrl+Alt+L", _CA | MOD_NOREPEAT, 0x4C, "Start or stop listening to the call"),
    ("show_panel", "Ctrl+Alt+P", _CA | MOD_NOREPEAT, 0x50, "Open the control panel"),
]


class GlobalHotkeys(QObject):
    """System-wide hotkeys via RegisterHotKey, served by a small message-loop thread."""
    triggered = Signal(str)
    registered = Signal(list)  # labels that could not be registered

    def __init__(self, parent=None):
        super().__init__(parent)
        self._tid = None
        self.ready = threading.Event()

    def start(self):
        if IS_WINDOWS:
            threading.Thread(target=self._run, name="hotkeys", daemon=True).start()

    def _run(self):
        self._tid = kernel32.GetCurrentThreadId()
        ids, failed = {}, []
        for i, (name, label, mods, vk, _desc) in enumerate(HOTKEYS, start=1):
            if user32.RegisterHotKey(None, i, mods, vk):
                ids[i] = name
            else:
                failed.append(label)
        self.registered.emit(failed)
        self.ready.set()
        msg = wintypes.MSG()
        while True:
            r = user32.GetMessageW(ctypes.byref(msg), None, 0, 0)
            if r in (0, -1):
                break
            if msg.message == WM_HOTKEY and msg.wParam in ids:
                self.triggered.emit(ids[msg.wParam])
        for i in ids:
            user32.UnregisterHotKey(None, i)

    def stop(self):
        if IS_WINDOWS and self._tid:
            user32.PostThreadMessageW(self._tid, WM_QUIT, 0, 0)


# =============================================================================
# Settings
# =============================================================================
CONFIG_DIR = Path(os.environ.get("APPDATA") or Path.home()) / APP_NAME
SETTINGS_FILE = CONFIG_DIR / "settings.json"
SCRIPT_FILE = CONFIG_DIR / "last_script.txt"

PROVIDERS = {
    "Anthropic (Claude)": {
        "kind": "anthropic", "base_url": "https://api.anthropic.com", "model": "claude-sonnet-5-5",
        "env": "ANTHROPIC_API_KEY", "needs_key": True,
        "hint": "Get a key at console.anthropic.com. Leave the key blank to use the "
                "ANTHROPIC_API_KEY environment variable. For faster answers try claude-haiku-5-5."},
    "OpenAI": {
        "kind": "openai", "base_url": "https://api.openai.com/v1", "model": "gpt-4o-mini",
        "env": "OPENAI_API_KEY", "needs_key": True,
        "hint": "Get a key at platform.openai.com. Leave the key blank to use OPENAI_API_KEY. "
                "Check the provider's docs for current model names."},
    "Google Gemini": {
        "kind": "openai", "base_url": "https://generativelanguage.googleapis.com/v1beta/openai",
        "model": "gemini-2.5-flash", "env": "GEMINI_API_KEY", "needs_key": True,
        "hint": "Get a key at aistudio.google.com. Leave the key blank to use GEMINI_API_KEY."},
    "Ollama (local, free)": {
        "kind": "openai", "base_url": "http://localhost:11434/v1", "model": "llama3.2",
        "env": "", "needs_key": False,
        "hint": "Runs on your PC, offline. Install Ollama, run 'ollama pull llama3.2', "
                "and leave the key blank."},
    "Custom (OpenAI-compatible)": {
        "kind": "openai", "base_url": "", "model": "", "env": "", "needs_key": False,
        "hint": "Any server that speaks the OpenAI chat-completions format, such as LM Studio, "
                "Groq or OpenRouter. Enter its base URL (usually ending in /v1)."},
}
DEFAULT_PROVIDER = "Anthropic (Claude)"

DEFAULTS = {
    "font_family": "Segoe UI",
    "font_size": 30,
    "bold": True,
    "align": "Left",
    "line_spacing": 140,
    "text_color": "#F4F6FA",
    "cue_color": AMBER,
    "bg_color": "#0E1420",
    "bg_opacity": 72,
    "speed": 40,               # pixels per second
    "margin": 30,
    "guide_enabled": True,
    "guide_pos": 30,           # % from the top
    "mirror": False,
    "hide_from_capture": True,
    "click_through": False,
    "overlay_visible": True,
    "overlay_geometry": None,  # [x, y, w, h]
    "panel_geometry": "",
    "last_file": "",
    "provider": DEFAULT_PROVIDER,
    "ai_profiles": {},
    "remember_key": True,
    "use_script_context": True,
    "auto_insert_answer": False,
    "stt_provider": "Groq (free, cloud)",
    "stt_profiles": {},
    "capture_mic": False,
    "auto_answer_questions": False,
    "listen_consent_ack": False,
    "doc_path": "",
    "embed_provider": "OpenAI",
    "embed_profiles": {},
    "use_contextual": True,
    "project_doc_on_question": True,
}

SAMPLE_SCRIPT = """Good morning, everyone.

Thanks for making time today. [PAUSE]

This is CuePilot, your teleprompter.
It floats above Meet, Zoom and Teams,
so your eyes can stay close to the camera.

Press Ctrl+Alt+Space to start or pause.
Ctrl+Alt+Up and Ctrl+Alt+Down change the speed. [SMILE]

Words in square brackets turn amber, so they work as stage cues.
Wrap a word in **double asterisks** to underline it.

Replace this text with your own script,
or ask the AI assistant to draft one for you."""


def load_settings() -> dict:
    s = copy.deepcopy(DEFAULTS)
    try:
        data = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            for k, v in data.items():
                if k in s:
                    s[k] = v
    except FileNotFoundError:
        pass
    except Exception as exc:  # corrupt file: start fresh, keep going
        print(f"Could not read settings: {exc}", file=sys.stderr)
    if not isinstance(s["ai_profiles"], dict):
        s["ai_profiles"] = {}
    for name, meta in PROVIDERS.items():
        prof = s["ai_profiles"].setdefault(name, {})
        prof.setdefault("base_url", meta["base_url"])
        prof.setdefault("model", meta["model"])
        prof.setdefault("api_key", "")
    if not isinstance(s["stt_profiles"], dict):
        s["stt_profiles"] = {}
    if listen is not None:
        for name, meta in listen.STT_PROVIDERS.items():
            prof = s["stt_profiles"].setdefault(name, {})
            prof.setdefault("base_url", meta["base_url"])
            prof.setdefault("model", meta["model"])
            prof.setdefault("api_key", "")
        if s["stt_provider"] not in listen.STT_PROVIDERS:
            s["stt_provider"] = listen.DEFAULT_STT_PROVIDER
    if not isinstance(s["embed_profiles"], dict):
        s["embed_profiles"] = {}
    if docs is not None:
        for name, meta in docs.EMBED_PROVIDERS.items():
            prof = s["embed_profiles"].setdefault(name, {})
            prof.setdefault("base_url", meta["base_url"])
            prof.setdefault("model", meta["model"])
            prof.setdefault("api_key", "")
        if s["embed_provider"] not in docs.EMBED_PROVIDERS:
            s["embed_provider"] = "OpenAI"
    if s["provider"] not in PROVIDERS:
        s["provider"] = DEFAULT_PROVIDER
    return s


def save_settings(s: dict) -> None:
    data = copy.deepcopy(s)
    if not data.get("remember_key", True):
        for prof in data["ai_profiles"].values():
            prof["api_key"] = ""
        for prof in data.get("stt_profiles", {}).values():
            prof["api_key"] = ""
        for prof in data.get("embed_profiles", {}).values():
            prof["api_key"] = ""
    try:
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        tmp = SETTINGS_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
        tmp.replace(SETTINGS_FILE)
    except Exception as exc:
        print(f"Could not save settings: {exc}", file=sys.stderr)


def load_last_script() -> str:
    try:
        return SCRIPT_FILE.read_text(encoding="utf-8")
    except Exception:
        return SAMPLE_SCRIPT


def save_last_script(text: str) -> None:
    try:
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        SCRIPT_FILE.write_text(text, encoding="utf-8")
    except Exception:
        pass


# =============================================================================
# Script formatting
# =============================================================================
CUE_RE = re.compile(r"\[([^\[\]\n]{1,80})\]")
EMPH_RE = re.compile(r"\*\*(.+?)\*\*")


def script_to_html(raw: str, text_color: str, cue_color: str) -> str:
    """One paragraph per script line, so line breaks are kept and line N == block N."""
    out = []
    for line in raw.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        esc = html.escape(line, quote=False)
        esc = EMPH_RE.sub(r"<u>\1</u>", esc)
        esc = CUE_RE.sub(lambda m: f'<span style="color:{cue_color};">[{m.group(1)}]</span>', esc)
        if not esc.strip():
            esc = "&nbsp;"
        out.append(f'<p style="margin:0px; color:{text_color};">{esc}</p>')
    return "<html><body>" + "".join(out) + "</body></html>"


def clean_ai_text(t: str) -> str:
    """Strip markdown the prompter can't use; keep [cues] and **emphasis**."""
    t = t.replace("\r\n", "\n").strip()
    t = re.sub(r"^```[^\n]*\n", "", t)
    t = re.sub(r"\n```\s*$", "", t)
    t = re.sub(r"</?passage>", "", t)
    t = re.sub(r"(?m)^\s{0,3}#{1,6}\s+", "", t)
    t = re.sub(r"(?m)^(\s*)[*\u2022]\s+", r"\1- ", t)
    t = re.sub(r"\n{3,}", "\n\n", t)
    return t.strip()


def fmt_duration(seconds: float) -> str:
    m, s = divmod(int(round(max(0.0, seconds))), 60)
    return f"{m}:{s:02d}"


def count_words(text: str) -> int:
    return len(re.findall(r"\S+", CUE_RE.sub(" ", text)))


# =============================================================================
# AI client (standard library only)
# =============================================================================
SYSTEM_SCRIPT = (
    "You write teleprompter scripts that one person reads aloud on a live video call.\n"
    "Write only the words to be spoken, in natural spoken language.\n"
    "Keep sentences short, put one thought on each line, and leave a blank line between paragraphs.\n"
    "You may add short stage cues in square brackets, such as [PAUSE] or [NEXT SLIDE], sparingly.\n"
    "You may wrap one key word per paragraph in **double asterisks** for emphasis.\n"
    "Do not use headings, bullet symbols, emojis, or any other markdown.\n"
    "Do not add an introduction, explanation or closing note. Output the script only."
)
SYSTEM_REWRITE = (
    SYSTEM_SCRIPT + "\nYou will receive part of a script inside <passage> tags and an instruction. "
    "Apply the instruction and return only the rewritten passage, without the tags."
)
SYSTEM_ASK = (
    "You support a speaker during a live video call. They will glance at your answer on a "
    "teleprompter and say it in their own words.\n"
    "Answer in at most 6 short lines, each under 15 words, most important point first.\n"
    "Plain text only: no headings, no markdown, no preamble.\n"
    "If the speaker's script or the recent conversation is provided, stay consistent with it and "
    "resolve follow-ups like 'and how does that scale?' using that context."
)
SYSTEM_GROUNDED = (
    "You support a speaker during a live video call. They will glance at your answer on a "
    "teleprompter and say it in their own words.\n"
    "You are given an extract from the speaker's own reference document that is relevant to the "
    "question. Base your answer on that extract. Stay faithful to it; do not invent facts it "
    "doesn't contain. You may add a brief connecting phrase from the script or general knowledge.\n"
    "Answer in at most 6 short lines, each under 15 words, most important point first.\n"
    "Plain text only: no headings, no markdown, no preamble."
)
SYSTEM_DETECT = (
    "You watch a live video call on behalf of one participant (the speaker). You are given the "
    "most recent lines the OTHER people on the call said, in order. Decide whether the latest "
    "part contains a question or request that the speaker is expected to answer out loud now.\n"
    "Treat as a question: direct questions, indirect ones ('I'd love to hear about your Shield "
    "experience'), and requests to explain or walk through something.\n"
    "Do NOT treat as a question: small talk, rhetorical questions, filler, check-ins like 'can you "
    "hear me?', or anything already clearly answered.\n"
    "Reply with a single JSON object and nothing else: {\"question\": \"...\"} with the question "
    "phrased clearly and self-contained (resolve pronouns using the context), or {\"question\": null} "
    "if there is nothing to answer. Output only the JSON."
)
LENGTHS = {"30 seconds": 75, "1 minute": 140, "2 minutes": 280, "3 minutes": 420,
           "5 minutes": 700, "10 minutes": 1400}
TONES = ["Professional", "Conversational", "Confident and persuasive", "Warm and friendly",
         "Crisp status update"]
REWRITE_ACTIONS = {
    "Polish and tighten": "Improve clarity and flow. Keep the meaning and roughly the same length.",
    "Make it conversational": "Make it sound natural and conversational, as if talking to colleagues. "
                              "Use contractions.",
    "Shorten by about a third": "Cut it to about two thirds of its length. Keep the key points.",
    "Simplify the language": "Use simpler, everyday words and shorter sentences.",
    "Sound more confident": "Remove hedging and filler words so it sounds confident and direct.",
    "Add pause cues": "Keep the words, but break long lines and add [PAUSE] at natural breaks.",
    "Translate to Hindi": "Translate into natural spoken Hindi in Devanagari script. "
                          "Keep the line structure and any [cues].",
    "Custom instruction": None,
}


def _http_post_json(url: str, body: dict, headers: dict, timeout: float):
    req = urllib.request.Request(url, data=json.dumps(body).encode("utf-8"),
                                 headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            payload = json.loads(exc.read().decode("utf-8", "replace"))
            if isinstance(payload, list) and payload:
                payload = payload[0]
            err = payload.get("error", payload) if isinstance(payload, dict) else payload
            detail = err.get("message", "") if isinstance(err, dict) else str(err)
        except Exception:
            pass
        hint = {401: "check the API key", 403: "this key has no access to that model",
                404: "check the server URL and model name",
                429: "rate limit or quota reached, try again shortly"}.get(exc.code, "")
        msg = f"The AI service returned HTTP {exc.code}"
        if detail:
            msg += f": {detail}"
        if hint:
            msg += f" ({hint})"
        raise RuntimeError(msg) from None
    except (TimeoutError, socket.timeout):
        raise RuntimeError("The AI service took too long to answer. Try again.") from None
    except urllib.error.URLError as exc:
        host = urllib.parse.urlsplit(url).netloc or url
        raise RuntimeError(f"Could not reach {host}: {exc.reason}. "
                           "Check your internet connection and the server URL.") from None
    except json.JSONDecodeError:
        raise RuntimeError("The AI service sent a reply that isn't JSON. Check the server URL.") from None
    except (OSError, http.client.HTTPException) as exc:
        host = urllib.parse.urlsplit(url).netloc or url
        raise RuntimeError(f"The connection to {host} dropped ({exc.__class__.__name__}). "
                           "Try again.") from None


def ai_complete(cfg: dict, system: str, user: str, max_tokens: int = 1500,
                timeout: float = 120) -> str:
    base = cfg["base_url"].strip().rstrip("/")
    headers = {"Content-Type": "application/json", "User-Agent": f"{APP_NAME}/{APP_VERSION}"}
    if cfg["kind"] == "anthropic":
        if base.endswith("/v1"):
            base = base[:-3]
        headers["x-api-key"] = cfg["api_key"]
        headers["anthropic-version"] = "2023-06-01"
        body = {"model": cfg["model"], "max_tokens": max_tokens, "system": system,
                "messages": [{"role": "user", "content": user}]}
        data = _http_post_json(base + "/v1/messages", body, headers, timeout)
        text = "".join(b.get("text", "") for b in data.get("content", [])
                       if isinstance(b, dict) and b.get("type") == "text")
    else:
        if cfg["api_key"]:
            headers["Authorization"] = "Bearer " + cfg["api_key"]
        body = {"model": cfg["model"],
                "messages": [{"role": "system", "content": system},
                             {"role": "user", "content": user}]}
        data = _http_post_json(base + "/chat/completions", body, headers, timeout)
        try:
            content = data["choices"][0]["message"]["content"] or ""
        except (KeyError, IndexError, TypeError):
            raise RuntimeError("The AI service sent an unexpected reply. Check the server URL.") from None
        if isinstance(content, list):
            content = "".join(p.get("text", "") for p in content if isinstance(p, dict))
        text = content
    if not text.strip():
        raise RuntimeError("The AI service returned an empty reply. Try again or pick another model.")
    return text.strip()


class AIRequest(QObject):
    """Runs one AI call on a daemon thread and reports back through queued signals."""
    succeeded = Signal(str)
    failed = Signal(str)
    done = Signal(object)

    def __init__(self, cfg, system, user, max_tokens, parent=None):
        super().__init__(parent)
        self._args = (cfg, system, user, max_tokens)

    def start(self):
        threading.Thread(target=self._run, name="ai-request", daemon=True).start()

    def _run(self):
        try:
            self.succeeded.emit(ai_complete(*self._args))
        except Exception as exc:
            self.failed.emit(str(exc) or exc.__class__.__name__)
        finally:
            self.done.emit(self)


class ListenerBridge(QObject):
    """The listener runs on background threads; these signals hop safely onto the GUI thread."""
    transcript = Signal(str, bool, str)   # text, is_question, speaker
    status = Signal(str, bool)       # message, is_error
    level = Signal(float)            # 0..1 input loudness


class DocBridge(QObject):
    """Signals from the document background workers onto the GUI thread."""
    loaded = Signal(int, str)            # passage count, message
    load_failed = Signal(str)
    embed_progress = Signal(int, int, str)
    embed_done = Signal(bool, str)       # contextual?, message
    embed_failed = Signal(str)
    search_result = Signal(str, str, str, str)  # question, meta, heading, text
    search_empty = Signal(str)           # question


class DocWorker(threading.Thread):
    """Runs one document job (load, embed, or search) on a daemon thread."""

    def __init__(self, fn, parent_set):
        super().__init__(name="cuepilot-doc", daemon=True)
        self._fn = fn
        self._set = parent_set
        self._set.add(self)

    def run(self):
        try:
            self._fn()
        finally:
            self._set.discard(self)


# =============================================================================
# The overlay prompter
# =============================================================================
ICON_FONTS = ["Segoe Fluent Icons", "Segoe MDL2 Assets"]
GLYPH = {"play": "\uE768", "pause": "\uE769", "top": "\uE892", "up": "\uE70E", "down": "\uE70D",
         "smaller": "\uE8E7", "bigger": "\uE8E8", "settings": "\uE713", "close": "\uE711"}


def _enum_int(value) -> int:
    return int(value.value) if hasattr(value, "value") else int(value)


def make_timer(parent, ms, slot, single=True) -> QTimer:
    t = QTimer(parent)
    t.setSingleShot(single)
    t.setInterval(ms)
    t.timeout.connect(slot)
    return t


class OverlayToolbar(QFrame):
    """Small control strip that appears while the mouse is over the prompter."""

    def __init__(self, overlay: "PrompterOverlay"):
        super().__init__(overlay)
        self.ov = overlay
        self.setObjectName("ovbar")
        self.setStyleSheet(f"""
            QFrame#ovbar {{ background: rgba(16, 22, 32, 225); border: 1px solid rgba(255,255,255,28);
                            border-radius: 8px; }}
            QToolButton {{ color: {TEXT}; background: transparent; border: none; border-radius: 5px;
                           padding: 4px 7px; }}
            QToolButton:hover {{ background: rgba(255,255,255,30); }}
            QLabel {{ color: {MUTED}; background: transparent; font-family: "Segoe UI"; font-size: 9pt; }}
        """)
        icon_font = QFont()
        icon_font.setFamilies(ICON_FONTS)
        icon_font.setPointSize(10)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(6, 2, 6, 2)
        lay.setSpacing(1)

        def btn(glyph, tip, slot):
            b = QToolButton(self)
            b.setText(GLYPH[glyph])
            b.setFont(icon_font)
            b.setToolTip(tip)
            b.setCursor(Qt.CursorShape.PointingHandCursor)
            b.clicked.connect(slot)
            lay.addWidget(b)
            return b

        btn("top", "Back to the top (Ctrl+Alt+R)", overlay.restart)
        self.play = btn("play", "Start or pause (Ctrl+Alt+Space)", overlay.toggle_play)
        btn("down", "Slower (Ctrl+Alt+Down)", overlay.slower)
        self.speed = QLabel(self)
        self.speed.setMinimumWidth(30)
        self.speed.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.speed.setToolTip("Scroll speed")
        lay.addWidget(self.speed)
        btn("up", "Faster (Ctrl+Alt+Up)", overlay.faster)
        lay.addSpacing(8)
        btn("smaller", "Smaller text (Ctrl+Alt+-)", overlay.smaller)
        btn("bigger", "Larger text (Ctrl+Alt+=)", overlay.bigger)
        lay.addStretch(1)
        btn("settings", "Open the control panel (Ctrl+Alt+P)", lambda: overlay.openPanelRequested.emit())
        btn("close", "Hide the prompter (Ctrl+Alt+H)", overlay.hide)
        self.sync()

    def sync(self):
        self.play.setText(GLYPH["pause"] if self.ov.playing else GLYPH["play"])
        self.speed.setText(str(self.ov.s["speed"]))


class PrompterOverlay(QWidget):
    stateChanged = Signal()
    openPanelRequested = Signal()
    visibilityChanged = Signal(bool)

    EDGE = 8

    def __init__(self, settings: dict):
        flags = (Qt.WindowType.FramelessWindowHint | Qt.WindowType.WindowStaysOnTopHint
                 | Qt.WindowType.Tool)
        super().__init__(None, flags)
        self.s = settings
        self.setWindowTitle(f"{APP_NAME} prompter")
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)
        self.setMouseTracking(True)
        self.setMinimumSize(300, 110)
        self.setFocusPolicy(Qt.FocusPolicy.ClickFocus)

        self.doc = QTextDocument(self)
        self.doc.setDocumentMargin(0)
        self.doc.setUndoRedoEnabled(False)
        self.raw_text = ""
        self.offset = 0.0
        self.playing = False
        self.click_through = bool(settings.get("click_through"))
        if self.click_through:
            self.setWindowFlag(Qt.WindowType.WindowTransparentForInput, True)
        self.capture_state = ""
        self._line_h = 30.0
        self._press_pos = None

        self.clock = QElapsedTimer()
        self.clock.start()
        self._last_ms = 0
        self.anim = make_timer(self, 15, self._tick, single=False)
        self.anim.setTimerType(Qt.TimerType.PreciseTimer)
        self.topmost_timer = make_timer(self, 1500, self._reassert_topmost, single=False)

        self.bar = OverlayToolbar(self)
        self.bar.hide()
        self._bar_hide = make_timer(self, 700, self.bar.hide)
        self.rebuild()

    # ---------------------------------------------------------------- layout
    def _font(self) -> QFont:
        f = QFont(self.s["font_family"])
        f.setPointSizeF(float(self.s["font_size"]))
        f.setWeight(QFont.Weight.DemiBold if self.s["bold"] else QFont.Weight.Normal)
        return f

    def _text_width(self) -> float:
        return max(60.0, self.width() - 2.0 * self.s["margin"])

    def max_offset(self) -> float:
        return max(0.0, self.doc.size().height() - self._line_h)

    def rebuild(self, keep_ratio: bool = False):
        old_max = self.max_offset()
        ratio = self.offset / old_max if old_max > 0 else 0.0
        font = self._font()
        self.doc.setDefaultFont(font)
        self.doc.setHtml(script_to_html(self.raw_text, self.s["text_color"], self.s["cue_color"]))
        fmt = QTextBlockFormat()
        fmt.setLineHeight(float(self.s["line_spacing"]), 1)  # 1 = ProportionalHeight
        fmt.setAlignment(Qt.AlignmentFlag.AlignHCenter if self.s["align"] in ("Center", "Centre")
                         else Qt.AlignmentFlag.AlignLeft)
        cur = QTextCursor(self.doc)
        cur.select(QTextCursor.SelectionType.Document)
        cur.mergeBlockFormat(fmt)
        self.doc.setTextWidth(self._text_width())
        self._line_h = QFontMetricsF(font).lineSpacing() * self.s["line_spacing"] / 100.0
        if keep_ratio:
            self.offset = ratio * self.max_offset()
        self.offset = min(max(0.0, self.offset), self.max_offset())
        self.update()

    def set_text(self, raw: str):
        self.raw_text = raw
        self.rebuild()

    def resizeEvent(self, e):
        old_max = self.max_offset()
        ratio = self.offset / old_max if old_max > 0 else 0.0
        self.doc.setTextWidth(self._text_width())
        self.offset = ratio * self.max_offset()
        self.bar.setGeometry(8, 6, self.width() - 16, 32)
        super().resizeEvent(e)

    # -------------------------------------------------------------- playback
    def set_playing(self, on: bool):
        if on and self.offset >= self.max_offset() - 1:
            self.offset = 0.0  # at the end: start over
        self.playing = on
        if on:
            self._last_ms = self.clock.elapsed()
            self.anim.start()
        else:
            self.anim.stop()
        self.bar.sync()
        self.update()
        self.stateChanged.emit()

    def toggle_play(self):
        self.set_playing(not self.playing)

    def _tick(self):
        now = self.clock.elapsed()
        dt = min(0.1, (now - self._last_ms) / 1000.0)
        self._last_ms = now
        self.offset += self.s["speed"] * dt
        if self.offset >= self.max_offset():
            self.offset = self.max_offset()
            self.set_playing(False)
        self.update()

    def faster(self):
        self.s["speed"] = min(400, max(self.s["speed"] + 1, round(self.s["speed"] * 1.15)))
        self.bar.sync()
        self.stateChanged.emit()

    def slower(self):
        self.s["speed"] = max(5, min(self.s["speed"] - 1, round(self.s["speed"] / 1.15)))
        self.bar.sync()
        self.stateChanged.emit()

    def bigger(self):
        self.s["font_size"] = min(120, self.s["font_size"] + 2)
        self.rebuild(keep_ratio=True)
        self.stateChanged.emit()

    def smaller(self):
        self.s["font_size"] = max(12, self.s["font_size"] - 2)
        self.rebuild(keep_ratio=True)
        self.stateChanged.emit()

    def jump(self, direction: int):
        self.offset = min(max(0.0, self.offset + direction * self.height() * 0.35), self.max_offset())
        self.update()
        self.stateChanged.emit()

    def restart(self):
        self.offset = 0.0
        self.update()
        self.stateChanged.emit()

    def total_seconds(self) -> float:
        return self.max_offset() / max(1, self.s["speed"])

    def reading_line_index(self) -> int:
        """Script line currently under the reading line."""
        pos = self.doc.documentLayout().hitTest(QPointF(2, self.offset + self._line_h * 0.5),
                                                Qt.HitTestAccuracy.FuzzyHit)
        if pos < 0:
            return 0
        return max(0, self.doc.findBlock(pos).blockNumber())

    # ------------------------------------------------------- native window bits
    def apply_native(self):
        if not IS_WINDOWS or not self.isVisible():
            return
        hwnd = int(self.winId())
        set_native_click_through(hwnd, self.click_through)
        self.capture_state = set_capture_excluded(hwnd, bool(self.s["hide_from_capture"]))
        force_topmost(hwnd)
        self.stateChanged.emit()

    def set_click_through(self, on: bool):
        self.click_through = on
        wh = self.windowHandle()
        if wh is not None:
            wh.setFlag(Qt.WindowType.WindowTransparentForInput, on)
        if on:
            self.bar.hide()
        QTimer.singleShot(0, self.apply_native)
        self.update()
        self.stateChanged.emit()

    def _reassert_topmost(self):
        # Some meeting apps raise their own windows when they go full screen.
        if self.isVisible() and QApplication.activePopupWidget() is None:
            force_topmost(int(self.winId()))

    def snap_under_webcam(self):
        screen = self.screen() or QGuiApplication.primaryScreen()
        g = screen.availableGeometry()
        w = min(self.width(), int(g.width() * 0.7))
        self.setGeometry(g.center().x() - w // 2, g.top() + 6, w, self.height())

    def showEvent(self, e):
        super().showEvent(e)
        wh = self.windowHandle()
        if wh is not None and self.click_through:
            wh.setFlag(Qt.WindowType.WindowTransparentForInput, True)
        QTimer.singleShot(0, self.apply_native)
        self.topmost_timer.start()
        self.visibilityChanged.emit(True)

    def hideEvent(self, e):
        super().hideEvent(e)
        self.topmost_timer.stop()
        if self.playing:
            self.set_playing(False)
        self.visibilityChanged.emit(False)

    # ---------------------------------------------------------------- painting
    def paintEvent(self, _e):
        s = self.s
        w, h = self.width(), self.height()
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setRenderHint(QPainter.RenderHint.TextAntialiasing)

        frame = QPainterPath()
        frame.addRoundedRect(QRectF(0.5, 0.5, w - 1, h - 1), 14, 14)
        bg = QColor(s["bg_color"])
        # Alpha never reaches 0: fully transparent pixels would let clicks fall through.
        bg.setAlpha(max(3, round(255 * s["bg_opacity"] / 100)))
        p.fillPath(frame, bg)
        if s["bg_opacity"] < 35 or (self.underMouse() and not self.click_through):
            p.setPen(QPen(QColor(255, 255, 255, 46), 1))
            p.drawPath(frame)
        p.setClipPath(frame)

        guide_y = h * s["guide_pos"] / 100.0
        cue = QColor(s["cue_color"])
        if s["guide_enabled"]:
            band = QColor(cue)
            band.setAlpha(32)
            bh = self._line_h * 1.04
            p.fillRect(QRectF(0, guide_y - bh / 2, w, bh), band)
            for x0, x1 in ((4, 12), (w - 4, w - 12)):
                tri = QPainterPath()
                tri.moveTo(x0, guide_y - 7)
                tri.lineTo(x1, guide_y)
                tri.lineTo(x0, guide_y + 7)
                tri.closeSubpath()
                p.fillPath(tri, cue)

        if self.raw_text.strip():
            p.save()
            if s["mirror"]:
                p.translate(w, 0)
                p.scale(-1, 1)
            top = guide_y - self._line_h * 0.5 - self.offset
            p.translate(s["margin"], top)
            ctx = QAbstractTextDocumentLayout.PaintContext()
            pal = ctx.palette
            pal.setColor(QPalette.ColorRole.Text, QColor(s["text_color"]))
            ctx.palette = pal
            ctx.clip = QRectF(0, -top, self.doc.textWidth(), h)
            self.doc.documentLayout().draw(p, ctx)
            p.restore()
        else:
            muted = QColor(s["text_color"])
            muted.setAlpha(150)
            p.setPen(muted)
            p.setFont(QFont("Segoe UI", 12))
            flags = int(Qt.AlignmentFlag.AlignCenter.value) | int(Qt.TextFlag.TextWordWrap.value)
            p.drawText(self.rect().adjusted(24, 24, -24, -24), flags,
                       "Type or paste your script in the control panel.\nIt will scroll here.")

        mx = self.max_offset()
        if mx > 0:
            p.fillRect(QRectF(14, h - 4, (w - 28) * (self.offset / mx), 2), cue)
        if self.playing:
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QColor(TALLY))
            p.drawEllipse(QPointF(16, 16), 4.5, 4.5)
        if self.click_through:
            p.setPen(QColor(255, 255, 255, 110))
            p.setFont(QFont("Segoe UI", 8))
            p.drawText(QRectF(0, h - 22, w - 14, 16),
                       int(Qt.AlignmentFlag.AlignRight.value | Qt.AlignmentFlag.AlignVCenter.value),
                       "Click-through on. Ctrl+Alt+T to move or resize.")
        p.end()

    # ------------------------------------------------------------ mouse/keys
    def _edges_at(self, pos):
        m, w, h = self.EDGE, self.width(), self.height()
        edges = []
        if pos.x() <= m:
            edges.append(Qt.Edge.LeftEdge)
        elif pos.x() >= w - m:
            edges.append(Qt.Edge.RightEdge)
        if pos.y() <= m:
            edges.append(Qt.Edge.TopEdge)
        elif pos.y() >= h - m:
            edges.append(Qt.Edge.BottomEdge)
        return edges

    _CURSORS = {
        frozenset([Qt.Edge.LeftEdge]): Qt.CursorShape.SizeHorCursor,
        frozenset([Qt.Edge.RightEdge]): Qt.CursorShape.SizeHorCursor,
        frozenset([Qt.Edge.TopEdge]): Qt.CursorShape.SizeVerCursor,
        frozenset([Qt.Edge.BottomEdge]): Qt.CursorShape.SizeVerCursor,
        frozenset([Qt.Edge.LeftEdge, Qt.Edge.TopEdge]): Qt.CursorShape.SizeFDiagCursor,
        frozenset([Qt.Edge.RightEdge, Qt.Edge.BottomEdge]): Qt.CursorShape.SizeFDiagCursor,
        frozenset([Qt.Edge.RightEdge, Qt.Edge.TopEdge]): Qt.CursorShape.SizeBDiagCursor,
        frozenset([Qt.Edge.LeftEdge, Qt.Edge.BottomEdge]): Qt.CursorShape.SizeBDiagCursor,
    }

    def mousePressEvent(self, e):
        if e.button() == Qt.MouseButton.LeftButton:
            edges = self._edges_at(e.position().toPoint())
            wh = self.windowHandle()
            if edges and wh is not None:
                wh.startSystemResize(reduce(or_, edges))
            else:
                self._press_pos = e.position()
            e.accept()
            return
        super().mousePressEvent(e)

    def mouseMoveEvent(self, e):
        if self._press_pos is not None and (e.buttons() & Qt.MouseButton.LeftButton):
            if (e.position() - self._press_pos).manhattanLength() > 4:
                self._press_pos = None
                wh = self.windowHandle()
                if wh is not None:
                    wh.startSystemMove()
            return
        shape = self._CURSORS.get(frozenset(self._edges_at(e.position().toPoint())))
        if shape is None:
            self.unsetCursor()
        else:
            self.setCursor(shape)

    def mouseReleaseEvent(self, e):
        self._press_pos = None
        super().mouseReleaseEvent(e)

    def mouseDoubleClickEvent(self, e):
        if e.button() == Qt.MouseButton.LeftButton:
            self.toggle_play()

    def wheelEvent(self, e):
        dy = e.angleDelta().y()
        if e.modifiers() & Qt.KeyboardModifier.ControlModifier:
            (self.bigger if dy > 0 else self.smaller)()
        else:
            self.offset = min(max(0.0, self.offset - dy * 0.6), self.max_offset())
            self.update()
        e.accept()

    def keyPressEvent(self, e):
        K = Qt.Key
        actions = {K.Key_Space: self.toggle_play, K.Key_Up: self.faster, K.Key_Down: self.slower,
                   K.Key_PageUp: lambda: self.jump(-1), K.Key_PageDown: lambda: self.jump(1),
                   K.Key_Home: self.restart, K.Key_Plus: self.bigger, K.Key_Equal: self.bigger,
                   K.Key_Minus: self.smaller, K.Key_Escape: lambda: self.set_playing(False)}
        actions = {_enum_int(key): fn for key, fn in actions.items()}
        fn = actions.get(_enum_int(e.key()))
        if fn is None:
            super().keyPressEvent(e)
            return
        fn()
        e.accept()

    def enterEvent(self, e):
        if not self.click_through:
            self._bar_hide.stop()
            self.bar.show()
            self.bar.raise_()
        self.update()
        super().enterEvent(e)

    def leaveEvent(self, e):
        self._bar_hide.start()
        self.unsetCursor()
        self.update()
        super().leaveEvent(e)

    def contextMenuEvent(self, e):
        m = QMenu(self)
        items = [("Pause" if self.playing else "Start", self.toggle_play),
                 ("Back to the top", self.restart),
                 (None, None),
                 ("Place under the webcam", self.snap_under_webcam),
                 ("Turn on click-through", lambda: self.set_click_through(True)),
                 (None, None),
                 ("Open the control panel", lambda: self.openPanelRequested.emit()),
                 ("Hide the prompter", lambda: self.hide())]
        for text, fn in items:
            if text is None:
                m.addSeparator()
                continue
            act = m.addAction(text)
            act.triggered.connect(fn)
        m.exec(e.globalPos())


# =============================================================================
# Control panel
# =============================================================================
PANEL_QSS = """
QWidget { background: %(BG)s; color: %(TEXT)s; font-family: "Segoe UI"; font-size: 10pt; }
QLabel, QCheckBox { background: transparent; }
QLabel#title { font-family: "Bahnschrift", "Segoe UI"; font-size: 17pt; font-weight: 600; }
QLabel#subtitle { color: %(MUTED)s; font-size: 10pt; padding-top: 6px; }
QLabel#section { font-family: "Bahnschrift", "Segoe UI"; font-size: 11.5pt; font-weight: 600;
                 padding-top: 10px; }
QLabel#hint { color: %(MUTED)s; font-size: 9pt; }
QLabel#key { background: %(SURFACE_2)s; border: 1px solid %(BORDER)s; border-radius: 4px;
             padding: 2px 8px; font-family: "Bahnschrift", "Segoe UI"; }
QPlainTextEdit, QLineEdit, QSpinBox, QComboBox, QFontComboBox {
    background: %(SURFACE)s; border: 1px solid %(BORDER)s; border-radius: 6px; padding: 5px 7px;
    selection-background-color: %(AMBER)s; selection-color: %(INK)s; }
QPlainTextEdit:focus, QLineEdit:focus, QSpinBox:focus, QComboBox:focus { border-color: %(AMBER)s; }
QPlainTextEdit#script { font-size: 12.5pt; padding: 12px 14px; }
QComboBox QAbstractItemView { background: %(SURFACE)s; border: 1px solid %(BORDER)s;
    selection-background-color: %(AMBER)s; selection-color: %(INK)s; }
QPushButton { background: %(SURFACE_2)s; border: 1px solid %(BORDER)s; border-radius: 6px;
              padding: 6px 14px; }
QPushButton:hover { border-color: %(AMBER)s; }
QPushButton:pressed { background: %(SURFACE)s; }
QPushButton:disabled { color: %(MUTED)s; border-color: %(SURFACE_2)s; }
QPushButton#primary { background: %(AMBER)s; color: %(INK)s; border: none; font-weight: 600;
                      padding: 7px 22px; }
QPushButton#primary:hover { background: #F6C463; }
QPushButton#primary[live="true"] { background: %(TALLY)s; color: white; }
QPushButton#primary:disabled { background: %(SURFACE_2)s; color: %(MUTED)s; }
QTabWidget::pane { border: 1px solid %(BORDER)s; border-radius: 8px; top: -1px; }
QTabBar::tab { background: transparent; color: %(MUTED)s; padding: 8px 16px;
               border-bottom: 2px solid transparent; }
QTabBar::tab:selected { color: %(TEXT)s; border-bottom: 2px solid %(AMBER)s; }
QTabBar::tab:hover { color: %(TEXT)s; }
QCheckBox { spacing: 8px; }
QCheckBox::indicator { width: 15px; height: 15px; border: 1px solid %(BORDER)s; border-radius: 4px;
                       background: %(SURFACE)s; }
QCheckBox::indicator:checked { background: %(AMBER)s; border-color: %(AMBER)s; }
QSlider::groove:horizontal { height: 4px; background: %(BORDER)s; border-radius: 2px; }
QSlider::sub-page:horizontal { background: %(AMBER)s; border-radius: 2px; }
QSlider::handle:horizontal { background: %(TEXT)s; width: 14px; height: 14px; margin: -5px 0;
                             border-radius: 7px; }
QScrollArea { border: none; }
QScrollBar:vertical { background: transparent; width: 10px; }
QScrollBar::handle:vertical { background: %(BORDER)s; border-radius: 4px; min-height: 30px; }
QScrollBar::add-line, QScrollBar::sub-line { height: 0px; }
QSplitter::handle { background: transparent; }
QStatusBar { color: %(MUTED)s; }
QToolTip { background: %(SURFACE_2)s; color: %(TEXT)s; border: 1px solid %(BORDER)s; padding: 4px; }
QMenu { background: %(SURFACE)s; border: 1px solid %(BORDER)s; padding: 4px; }
QMenu::item { padding: 6px 18px; border-radius: 4px; }
QMenu::item:selected { background: %(AMBER)s; color: %(INK)s; }
""" % {"BG": BG, "SURFACE": SURFACE, "SURFACE_2": SURFACE_2, "BORDER": BORDER, "TEXT": TEXT,
       "MUTED": MUTED, "AMBER": AMBER, "TALLY": TALLY, "INK": INK}

CAPTURE_HINTS = {
    "hidden": "Hidden: others won't see CuePilot when you share your whole screen or record it.",
    "blacked": "This Windows version can only black CuePilot out in screen shares. Windows 10 "
               "version 2004 or later hides it completely.",
    "visible": "CuePilot will appear if you share your entire screen.",
    "failed": "Windows didn't allow hiding this window, so it may appear in screen shares.",
    "unsupported": "Hiding from screen sharing works on Windows only.",
    "": "Applies when the prompter is open.",
}


def make_icon() -> QIcon:
    pm = QPixmap(64, 64)
    pm.fill(Qt.GlobalColor.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(QColor(SURFACE_2))
    p.drawRoundedRect(QRectF(2, 2, 60, 60), 14, 14)
    p.setBrush(QColor(TEXT))
    for y, w in ((18, 36), (29, 40), (40, 24)):
        p.drawRoundedRect(QRectF(12, y, w, 5), 2.5, 2.5)
    p.setBrush(QColor(AMBER))
    p.drawEllipse(QPointF(48, 46), 7, 7)
    p.end()
    return QIcon(pm)


def label(text: str, name: str = "", wrap: bool = False) -> QLabel:
    lab = QLabel(text)
    if name:
        lab.setObjectName(name)
    lab.setWordWrap(wrap)
    return lab


def hrow(*widgets, stretch_last=False) -> QHBoxLayout:
    row = QHBoxLayout()
    row.setSpacing(8)
    for w in widgets:
        if w is None:
            row.addStretch(1)
        else:
            row.addWidget(w)
    if stretch_last:
        row.addStretch(1)
    return row


class ControlPanel(QMainWindow):
    def __init__(self, settings: dict, overlay: PrompterOverlay):
        super().__init__()
        self.s = settings
        self.ov = overlay
        self._requests = set()
        self._answer_requests = set()
        self._answer_token = 0
        self._shown_answer_grounded = False
        self._shown_answer_token = None
        self._busy = False
        self._pending = None
        self._loading_profile = False
        self._current_provider = self.s["provider"]
        self._hotkey_failures: list[str] = []

        self.setWindowTitle(f"{APP_NAME}: teleprompter for video calls")
        self.setWindowIcon(make_icon())
        self.setStyleSheet(PANEL_QSS)
        avail = QGuiApplication.primaryScreen().availableGeometry()
        self.resize(min(1140, avail.width() - 40), min(760, avail.height() - 40))
        if self.s.get("panel_geometry"):
            try:
                self.restoreGeometry(QByteArray.fromBase64(self.s["panel_geometry"].encode()))
            except Exception:
                pass

        self._push_timer = make_timer(self, 220, self._push_text)
        self._info_timer = make_timer(self, 1000, self._tick_info, single=False)
        self._info_timer.start()

        central = QWidget()
        root = QHBoxLayout(central)
        root.setContentsMargins(18, 14, 18, 10)
        split = QSplitter(Qt.Orientation.Horizontal)
        split.setHandleWidth(14)
        split.addWidget(self._build_editor())
        self.tabs = QTabWidget()
        self.tabs.setMinimumWidth(400)
        self.tabs.addTab(self._scroll(self._build_display_tab()), "Display")
        self.ai_tab = self._scroll(self._build_ai_tab())
        self.tabs.addTab(self.ai_tab, "AI assistant")
        self.tabs.addTab(self._scroll(self._build_shortcuts_tab()), "Shortcuts")
        split.addWidget(self.tabs)
        split.setStretchFactor(0, 3)
        split.setStretchFactor(1, 2)
        split.setSizes([680, 440])
        root.addWidget(split)
        self.setCentralWidget(central)

        for seq, fn in (("Ctrl+O", self._open_file), ("Ctrl+S", self._save_file),
                        ("Ctrl+Shift+S", self._save_file_as), ("F5", self.ov.toggle_play)):
            sc = QShortcut(QKeySequence(seq), self)
            sc.activated.connect(fn)

        overlay.stateChanged.connect(self._sync_controls)
        overlay.visibilityChanged.connect(self._on_overlay_visibility)
        overlay.openPanelRequested.connect(self.bring_to_front)

        self.editor.setPlainText(load_last_script())
        self._push_text()
        self._sync_controls()
        self.statusBar().showMessage("Ready. Press Ctrl+Alt+Space in any app to start scrolling.", 8000)

    # ------------------------------------------------------------------ builders
    def _scroll(self, inner: QWidget) -> QScrollArea:
        sa = QScrollArea()
        sa.setWidgetResizable(True)
        sa.setFrameShape(QFrame.Shape.NoFrame)
        sa.setWidget(inner)
        return sa

    def _build_editor(self) -> QWidget:
        w = QWidget()
        v = QVBoxLayout(w)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(10)

        open_btn = QPushButton("Open…")
        open_btn.setToolTip("Open a .txt or .md script (Ctrl+O)")
        open_btn.clicked.connect(self._open_file)
        save_btn = QPushButton("Save")
        save_btn.setToolTip("Save the script (Ctrl+S)")
        save_btn.clicked.connect(self._save_file)
        v.addLayout(hrow(label(APP_NAME, "title"), label("teleprompter for video calls", "subtitle"),
                         None, open_btn, save_btn))

        self.editor = QPlainTextEdit()
        self.editor.setObjectName("script")
        self.editor.setPlaceholderText(
            "Type or paste your script here. The prompter updates as you type.\n\n"
            "Put stage cues in square brackets, like [PAUSE] or [NEXT SLIDE]; they show in amber.\n"
            "Wrap a word in **double asterisks** to underline it.")
        self.editor.textChanged.connect(self._push_timer.start)
        v.addWidget(self.editor, 1)

        self.info = label("", "hint")
        v.addWidget(self.info)

        self.play_btn = QPushButton("Start")
        self.play_btn.setObjectName("primary")
        self.play_btn.setToolTip("Start or pause scrolling (Ctrl+Alt+Space, or F5 here)")
        self.play_btn.clicked.connect(self.ov.toggle_play)
        top_btn = QPushButton("Back to top")
        top_btn.clicked.connect(self.ov.restart)
        self.show_btn = QPushButton("Hide prompter")
        self.show_btn.setToolTip("Ctrl+Alt+H")
        self.show_btn.clicked.connect(self._toggle_overlay)
        snap_btn = QPushButton("Place under webcam")
        snap_btn.setToolTip("Moves the prompter to the top centre of its screen, "
                            "close to where most laptop cameras sit")
        snap_btn.clicked.connect(self._snap)
        v.addLayout(hrow(self.play_btn, top_btn, None, snap_btn, self.show_btn))
        return w

    def _slider(self, key, lo, hi, fmt, on_change):
        box = QWidget()
        lay = QHBoxLayout(box)
        lay.setContentsMargins(0, 0, 0, 0)
        sl = QSlider(Qt.Orientation.Horizontal)
        sl.setRange(lo, hi)
        sl.setValue(int(self.s[key]))
        val = QLabel(fmt(sl.value()))
        val.setMinimumWidth(52)
        val.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)

        def changed(v):
            val.setText(fmt(v))
            on_change(v)
        sl.valueChanged.connect(changed)
        lay.addWidget(sl, 1)
        lay.addWidget(val)
        return box, sl, val

    def _color_button(self, key, rebuild):
        b = QPushButton()
        b.setFixedSize(70, 26)
        b.setCursor(Qt.CursorShape.PointingHandCursor)

        def paint():
            b.setStyleSheet(f"background: {self.s[key]}; border: 1px solid {BORDER}; border-radius: 5px;")

        def pick():
            c = QColorDialog.getColor(QColor(self.s[key]), self, "Choose a colour")
            if c.isValid():
                self._set(key, c.name(), rebuild=rebuild)
                paint()
        paint()
        b.clicked.connect(pick)
        return b

    def _build_display_tab(self) -> QWidget:
        w = QWidget()
        v = QVBoxLayout(w)
        v.setContentsMargins(18, 12, 18, 18)
        form = QFormLayout()
        form.setHorizontalSpacing(16)
        form.setVerticalSpacing(10)
        form.setLabelAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)

        v.addWidget(label("Text", "section"))
        self.font_box = QFontComboBox()
        self.font_box.setCurrentFont(QFont(self.s["font_family"]))
        self.font_box.currentFontChanged.connect(
            lambda f: self._set("font_family", f.family(), rebuild=True, keep_ratio=True))
        form.addRow("Font", self.font_box)

        self.size_spin = QSpinBox()
        self.size_spin.setRange(12, 120)
        self.size_spin.setSuffix(" pt")
        self.size_spin.setValue(int(self.s["font_size"]))
        self.size_spin.valueChanged.connect(
            lambda val: self._set("font_size", val, rebuild=True, keep_ratio=True))
        self.bold_chk = QCheckBox("Semi-bold")
        self.bold_chk.setChecked(bool(self.s["bold"]))
        self.bold_chk.toggled.connect(lambda on: self._set("bold", on, rebuild=True, keep_ratio=True))
        size_row = QWidget()
        size_row.setLayout(hrow(self.size_spin, self.bold_chk, stretch_last=True))
        size_row.layout().setContentsMargins(0, 0, 0, 0)
        form.addRow("Size", size_row)

        self.align_combo = QComboBox()
        self.align_combo.addItems(["Left", "Centre"])
        self.align_combo.setCurrentText("Centre" if self.s["align"] in ("Center", "Centre") else "Left")
        self.align_combo.currentTextChanged.connect(lambda t: self._set("align", t, rebuild=True))
        self.spacing_spin = QSpinBox()
        self.spacing_spin.setRange(100, 250)
        self.spacing_spin.setSingleStep(10)
        self.spacing_spin.setSuffix(" %")
        self.spacing_spin.setValue(int(self.s["line_spacing"]))
        self.spacing_spin.valueChanged.connect(
            lambda val: self._set("line_spacing", val, rebuild=True, keep_ratio=True))
        align_row = QWidget()
        align_row.setLayout(hrow(self.align_combo, label("Line spacing", "hint"), self.spacing_spin,
                                 stretch_last=True))
        align_row.layout().setContentsMargins(0, 0, 0, 0)
        form.addRow("Alignment", align_row)

        colours = QWidget()
        colours.setLayout(hrow(self._color_button("text_color", True), label("Cues", "hint"),
                               self._color_button("cue_color", True), label("Background", "hint"),
                               self._color_button("bg_color", False), stretch_last=True))
        colours.layout().setContentsMargins(0, 0, 0, 0)
        form.addRow("Text colour", colours)

        box, self.opacity_slider, _ = self._slider("bg_opacity", 0, 100, lambda x: f"{x} %",
                                                   lambda x: self._set("bg_opacity", x))
        form.addRow("Background", box)
        self.margin_spin = QSpinBox()
        self.margin_spin.setRange(0, 200)
        self.margin_spin.setSuffix(" px")
        self.margin_spin.setValue(int(self.s["margin"]))
        self.margin_spin.valueChanged.connect(
            lambda val: self._set("margin", val, rebuild=True, keep_ratio=True))
        form.addRow("Side margin", self.margin_spin)
        v.addLayout(form)

        v.addWidget(label("Scrolling", "section"))
        form2 = QFormLayout()
        form2.setHorizontalSpacing(16)
        form2.setVerticalSpacing(10)
        box, self.speed_slider, self.speed_val = self._slider(
            "speed", 5, 400, lambda x: f"{x}", self._on_speed)
        form2.addRow("Speed", box)
        self.guide_chk = QCheckBox("Show a reading line")
        self.guide_chk.setChecked(bool(self.s["guide_enabled"]))
        self.guide_chk.toggled.connect(lambda on: self._set("guide_enabled", on))
        form2.addRow("", self.guide_chk)
        box, self.guide_slider, _ = self._slider("guide_pos", 10, 80, lambda x: f"{x} %",
                                                 lambda x: self._set("guide_pos", x))
        form2.addRow("Reading line at", box)
        self.mirror_chk = QCheckBox("Mirror the text (for a glass teleprompter)")
        self.mirror_chk.setChecked(bool(self.s["mirror"]))
        self.mirror_chk.toggled.connect(lambda on: self._set("mirror", on))
        form2.addRow("", self.mirror_chk)
        v.addLayout(form2)

        v.addWidget(label("During the call", "section"))
        self.capture_chk = QCheckBox("Hide CuePilot from screen sharing and recordings")
        self.capture_chk.setChecked(bool(self.s["hide_from_capture"]))
        self.capture_chk.toggled.connect(self._on_capture_toggled)
        v.addWidget(self.capture_chk)
        self.capture_hint = label("", "hint", wrap=True)
        v.addWidget(self.capture_hint)
        v.addWidget(label("Sharing a single window or browser tab never shows CuePilot.", "hint", wrap=True))
        v.addSpacing(6)
        self.click_chk = QCheckBox("Click-through: clicks pass to the meeting window behind")
        self.click_chk.setChecked(bool(self.s["click_through"]))
        self.click_chk.toggled.connect(self._on_click_through)
        v.addWidget(self.click_chk)
        v.addWidget(label("Turn this off (Ctrl+Alt+T) when you want to move or resize the prompter.",
                          "hint", wrap=True))
        v.addStretch(1)
        return w

    def _build_ai_tab(self) -> QWidget:
        w = QWidget()
        v = QVBoxLayout(w)
        v.setContentsMargins(18, 12, 18, 18)
        v.setSpacing(8)

        v.addWidget(label("Connection", "section"))
        form = QFormLayout()
        form.setHorizontalSpacing(16)
        form.setVerticalSpacing(8)
        self.provider_combo = QComboBox()
        self.provider_combo.addItems(list(PROVIDERS))
        self.url_edit = QLineEdit()
        self.key_edit = QLineEdit()
        self.key_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self.key_edit.setPlaceholderText("Paste your API key")
        self.model_edit = QLineEdit()
        form.addRow("Provider", self.provider_combo)
        form.addRow("Server URL", self.url_edit)
        form.addRow("API key", self.key_edit)
        form.addRow("Model", self.model_edit)
        v.addLayout(form)
        self.provider_hint = label("", "hint", wrap=True)
        v.addWidget(self.provider_hint)

        show_key = QCheckBox("Show key")
        show_key.toggled.connect(lambda on: self.key_edit.setEchoMode(
            QLineEdit.EchoMode.Normal if on else QLineEdit.EchoMode.Password))
        self.remember_chk = QCheckBox("Remember key on this PC")
        self.remember_chk.setToolTip(f"Saved in plain text in {SETTINGS_FILE}")
        self.remember_chk.setChecked(bool(self.s["remember_key"]))
        self.remember_chk.toggled.connect(lambda on: self.s.__setitem__("remember_key", on))
        self.test_btn = QPushButton("Test connection")
        self.test_btn.clicked.connect(self._test_connection)
        v.addLayout(hrow(show_key, self.remember_chk, None, self.test_btn))

        v.addWidget(label("Draft a script", "section"))
        self.topic_edit = QPlainTextEdit()
        self.topic_edit.setPlaceholderText("What is the talk about? Paste bullet points, notes or a rough draft.")
        self.topic_edit.setMaximumHeight(96)
        v.addWidget(self.topic_edit)
        self.length_combo = QComboBox()
        self.length_combo.addItems(list(LENGTHS))
        self.length_combo.setCurrentText("2 minutes")
        self.tone_combo = QComboBox()
        self.tone_combo.addItems(TONES)
        self.gen_btn = QPushButton("Write script")
        self.gen_btn.clicked.connect(self._generate)
        v.addLayout(hrow(self.length_combo, self.tone_combo, None, self.gen_btn))
        v.addWidget(label("Replaces the script in the editor. Ctrl+Z in the editor brings the old one back.",
                          "hint", wrap=True))

        v.addWidget(label("Rewrite", "section"))
        self.rewrite_combo = QComboBox()
        self.rewrite_combo.addItems(list(REWRITE_ACTIONS))
        self.rewrite_btn = QPushButton("Rewrite")
        self.rewrite_btn.clicked.connect(self._rewrite)
        v.addLayout(hrow(self.rewrite_combo, None, self.rewrite_btn))
        self.custom_edit = QLineEdit()
        self.custom_edit.setPlaceholderText("Your instruction, e.g. 'Add a one-line summary at the end'")
        self.custom_edit.setVisible(False)
        self.rewrite_combo.currentTextChanged.connect(
            lambda t: self.custom_edit.setVisible(REWRITE_ACTIONS.get(t) is None))
        v.addWidget(self.custom_edit)
        v.addWidget(label("Rewrites the text you select in the editor, or the whole script if nothing "
                          "is selected.", "hint", wrap=True))

        v.addWidget(label("Ask during the call", "section"))
        self.ask_edit = QLineEdit()
        self.ask_edit.setPlaceholderText("Type a question and press Enter. Ctrl+Alt+A jumps here.")
        self.ask_edit.returnPressed.connect(self._ask_clicked)
        self.ask_btn = QPushButton("Ask")
        self.ask_btn.clicked.connect(self._ask_clicked)
        v.addLayout(hrow(self.ask_edit, self.ask_btn))
        self.context_chk = QCheckBox("Use my script as context")
        self.context_chk.setChecked(bool(self.s["use_script_context"]))
        self.context_chk.toggled.connect(lambda on: self.s.__setitem__("use_script_context", on))
        self.auto_chk = QCheckBox("Put answers on the prompter automatically")
        self.auto_chk.setChecked(bool(self.s["auto_insert_answer"]))
        self.auto_chk.toggled.connect(lambda on: self.s.__setitem__("auto_insert_answer", on))
        v.addLayout(hrow(self.context_chk, self.auto_chk, stretch_last=True))
        self.answer_box = QPlainTextEdit()
        self.answer_box.setReadOnly(True)
        self.answer_box.setPlaceholderText("Answers appear here.")
        self.answer_box.setMaximumHeight(150)
        v.addWidget(self.answer_box)
        ins = QPushButton("Insert at reading line")
        ins.setToolTip("Adds the answer right below the line you're reading now")
        ins.clicked.connect(self._insert_answer_at_reading_line)
        app_btn = QPushButton("Add to end")
        app_btn.clicked.connect(self._append_answer)
        copy_btn = QPushButton("Copy")
        copy_btn.clicked.connect(lambda: QGuiApplication.clipboard().setText(self.answer_box.toPlainText()))
        v.addLayout(hrow(ins, app_btn, copy_btn, stretch_last=True))

        self.ai_status = label("", "hint", wrap=True)
        v.addWidget(self.ai_status)

        self._build_listen_section(v)
        self._build_doc_section(v)
        v.addStretch(1)

        # load the saved provider profile, then start listening for edits
        self._loading_profile = True
        self.provider_combo.setCurrentText(self._current_provider)
        self._load_profile(self._current_provider)
        self._loading_profile = False
        self.provider_combo.currentTextChanged.connect(self._on_provider_changed)
        for edit in (self.url_edit, self.key_edit, self.model_edit):
            edit.textChanged.connect(self._store_profile)
        return w

    # ------------------------------------------------------------ listen UI
    def _build_listen_section(self, v):
        self.listener = None
        self._loading_stt = False
        self._current_stt = self.s["stt_provider"]

        v.addWidget(label("Listen and answer", "section"))
        if listen is None:
            v.addWidget(label("Listening mode isn't available in this build.", "hint", wrap=True))
            self.listen_btn = QPushButton("Start listening")
            self.listen_btn.setEnabled(False)
            v.addWidget(self.listen_btn)
            return

        v.addWidget(label("CuePilot listens to the call, shows what's said, and flags questions "
                          "so you can answer them with one tap.", "hint", wrap=True))

        self.consent_chk = QCheckBox("Everyone on the call knows it's being transcribed")
        self.consent_chk.setChecked(bool(self.s["listen_consent_ack"]))
        self.consent_chk.toggled.connect(self._on_consent)
        v.addWidget(self.consent_chk)
        v.addWidget(label("Recording or transcribing people without telling them is illegal in some "
                          "places. You're responsible for getting consent.", "hint", wrap=True))

        form = QFormLayout()
        form.setHorizontalSpacing(16)
        form.setVerticalSpacing(8)
        self.stt_combo = QComboBox()
        self.stt_combo.addItems(list(listen.STT_PROVIDERS))
        self.stt_url = QLineEdit()
        self.stt_key = QLineEdit()
        self.stt_key.setEchoMode(QLineEdit.EchoMode.Password)
        self.stt_key.setPlaceholderText("Paste your transcription API key")
        self.stt_model = QLineEdit()
        form.addRow("Transcribe with", self.stt_combo)
        form.addRow("Server URL", self.stt_url)
        form.addRow("API key", self.stt_key)
        form.addRow("Model", self.stt_model)
        v.addLayout(form)
        self.stt_hint = label("", "hint", wrap=True)
        v.addWidget(self.stt_hint)

        self.mic_chk = QCheckBox("Also capture my microphone (shown greyed out, never answered)")
        self.mic_chk.setChecked(bool(self.s["capture_mic"]))
        self.mic_chk.toggled.connect(lambda on: self.s.__setitem__("capture_mic", on))
        self.mic_chk.setToolTip("CuePilot only answers the other person. Your microphone is used to "
                                "show your own lines and to filter your voice echoing back through "
                                "your speakers; it's never answered.")
        v.addWidget(self.mic_chk)
        self.auto_answer_chk = QCheckBox("Answer questions automatically as they're heard")
        self.auto_answer_chk.setChecked(bool(self.s["auto_answer_questions"]))
        self.auto_answer_chk.toggled.connect(lambda on: self.s.__setitem__("auto_answer_questions", on))
        v.addWidget(self.auto_answer_chk)
        v.addWidget(label("Otherwise, tap a question below to answer it.", "hint"))

        self.listen_btn = QPushButton("Start listening")
        self.listen_btn.setObjectName("primary")
        self.listen_btn.clicked.connect(self._toggle_listen)
        self.listen_level = QLabel("")
        self.listen_level.setMinimumWidth(90)
        self.listen_level.setToolTip("Input loudness")
        clear_btn = QPushButton("Clear")
        clear_btn.clicked.connect(self._clear_transcript)
        v.addLayout(hrow(self.listen_btn, self.listen_level, None, clear_btn))

        v.addWidget(label("Heard on the call (tap a question to answer it):", "hint"))
        self.transcript_box = QPlainTextEdit()
        self.transcript_box.setReadOnly(True)
        self.transcript_box.setPlaceholderText("What people say appears here. Questions are marked ❓.")
        self.transcript_box.setMaximumHeight(130)
        self.transcript_box.setMouseTracking(True)
        self.transcript_box.mousePressEvent = self._transcript_clicked
        v.addWidget(self.transcript_box)
        self.listen_status = label("", "hint", wrap=True)
        v.addWidget(self.listen_status)

        self._questions = []     # list of (line_text, question_text)
        self._history = []       # list of (who, text), who in ("them", "you")
        self._detect_buffer = []  # far-end lines awaiting an AI question check
        self.bridge = ListenerBridge(self)
        self.bridge.transcript.connect(self._on_transcript)
        self.bridge.status.connect(self._on_listen_status)
        self.bridge.level.connect(self._on_listen_level)
        self._level_timer = make_timer(self, 120, self._fade_level, single=False)
        # Fires ~1.6 s after the other person stops, then asks the AI if there's a question.
        self._detect_timer = make_timer(self, 1600, self._run_detection, single=True)
        self._detect_requests = set()

        self._loading_stt = True
        self.stt_combo.setCurrentText(self._current_stt)
        self._load_stt_profile(self._current_stt)
        self._loading_stt = False
        self.stt_combo.currentTextChanged.connect(self._on_stt_provider_changed)
        for edit in (self.stt_url, self.stt_key, self.stt_model):
            edit.textChanged.connect(self._store_stt_profile)

    def _load_stt_profile(self, name):
        prof = self.s["stt_profiles"].get(name, {})
        meta = listen.STT_PROVIDERS[name]
        self.stt_url.setText(prof.get("base_url", meta["base_url"]))
        self.stt_key.setText(prof.get("api_key", ""))
        self.stt_model.setText(prof.get("model", meta["model"]))
        local = meta["kind"] == "local"
        self.stt_url.setEnabled(not local)
        self.stt_key.setEnabled(not local)
        self.stt_key.setPlaceholderText("Not needed" if local or not meta["needs_key"]
                                        else "Paste your transcription API key")
        self.stt_hint.setText(meta["hint"])

    def _store_stt_profile(self, *_):
        if self._loading_stt:
            return
        self.s["stt_profiles"][self._current_stt] = {
            "base_url": self.stt_url.text().strip(),
            "api_key": self.stt_key.text().strip(),
            "model": self.stt_model.text().strip(),
        }

    def _on_stt_provider_changed(self, name):
        self._store_stt_profile()
        self._current_stt = name
        self.s["stt_provider"] = name
        self._loading_stt = True
        self._load_stt_profile(name)
        self._loading_stt = False

    def _on_consent(self, on):
        self.s["listen_consent_ack"] = on
        if not on and self.listener is not None:
            self._stop_listen()

    def _toggle_listen(self):
        if self.listener is not None:
            self._stop_listen()
        else:
            self._start_listen()

    def _start_listen(self):
        if not self.consent_chk.isChecked():
            self._set_listen_status("Confirm everyone on the call knows they're being transcribed "
                                    "before you start.", error=True)
            self.consent_chk.setFocus()
            return
        ok, msg = listen.capture_available()
        if not ok:
            self._set_listen_status(msg, error=True)
            return
        cfg, err = listen.build_stt_config(self._current_stt, self.stt_url.text(), self.stt_key.text(),
                                           self.stt_model.text())
        if err:
            self._set_listen_status(err, error=True)
            return
        try:
            self.listener = listen.Listener(
                cfg,
                on_transcript=lambda t, q, spk: self.bridge.transcript.emit(t, q, spk),
                on_status=lambda m, e: self.bridge.status.emit(m, e),
                on_level=lambda lv: self.bridge.level.emit(lv),
                capture_mic=self.mic_chk.isChecked(),
                suppress_echo=True)
            self.listener.start()
        except Exception as exc:
            self.listener = None
            self._set_listen_status(f"Couldn't start listening: {exc}", error=True)
            return
        self.listen_btn.setText("Stop listening")
        self.listen_btn.setProperty("live", "true")
        self._restyle(self.listen_btn)
        self._level_timer.start()
        if self.mic_chk.isChecked():
            self._set_listen_status("Listening. CuePilot answers only the other person; your own "
                                    "voice is shown greyed out. On speakers, headphones give the "
                                    "cleanest separation.")
        else:
            self._set_listen_status("Listening to the other person. Turn on 'capture my microphone' "
                                    "if your own voice leaks in and you want it filtered out.")
        self.statusBar().showMessage("Listening. Only the other person's questions are answered.", 6000)

    def _stop_listen(self):
        if self.listener is not None:
            try:
                self.listener.stop()
            except Exception:
                pass
            self.listener = None
        self.listen_btn.setText("Start listening")
        self.listen_btn.setProperty("live", "false")
        self._restyle(self.listen_btn)
        self._level_timer.stop()
        self._detect_timer.stop()
        self._detect_buffer = []
        self.listen_level.setText("")
        self._set_listen_status("Stopped listening.")

    def _restyle(self, widget):
        widget.style().unpolish(widget)
        widget.style().polish(widget)

    def _set_listen_status(self, msg, error=False):
        self.listen_status.setStyleSheet(f"color: {TALLY};" if error else f"color: {MUTED};")
        self.listen_status.setText(msg)

    def _on_listen_status(self, msg, error):
        self._set_listen_status(msg, error)
        if error:
            self.statusBar().showMessage(msg, 8000)

    def _on_listen_level(self, level):
        self._last_level = max(getattr(self, "_last_level", 0.0), level)

    def _fade_level(self):
        lv = getattr(self, "_last_level", 0.0)
        bars = min(10, int(lv * 120))
        self.listen_level.setText("▮" * bars + "▯" * (10 - bars) if self.listener else "")
        self._last_level = lv * 0.5

    def _on_transcript(self, text, is_question, speaker=None):
        if listen is not None and speaker == listen.ME:
            # Your own voice: show it greyed out, never answer it.
            self._append_transcript_line(f"you:  {text}", mine=True)
            self._history.append(("you", text))
            self._trim_history()
            return
        prefix = "them: " if (listen is not None and self.mic_chk.isChecked()) else ""
        # The quick rules give an instant hint; the AI confirms a beat later.
        mark = "• " if is_question else ""
        line = f"{mark}{prefix}{text}"
        self._append_transcript_line(line, mine=False)
        self._history.append(("them", text))
        self._trim_history()
        # Buffer the far-end line and schedule an AI check shortly after they stop talking.
        self._detect_buffer.append(text)
        self._detect_timer.start()

    def _trim_history(self):
        if len(self._history) > 40:
            self._history = self._history[-40:]

    def _recent_context(self, limit=12):
        rows = self._history[-limit:]
        return "\n".join(f"{'You' if who == 'you' else 'Them'}: {t}" for who, t in rows)

    def _run_detection(self):
        """Ask the AI whether the recent far-end lines contain a question to answer."""
        if listen is None or self.listener is None or not self._detect_buffer:
            return
        recent = self._history[-10:]
        if not recent:
            return
        self._detect_buffer = []
        convo = "\n".join(f"{'You' if who == 'you' else 'Them'}: {t}" for who, t in recent)
        cfg, err = self._ai_config()
        if err:
            self._set_listen_status("Listening, but question detection needs the AI set up: " + err,
                                    error=True)
            return
        user = ("Recent lines from the call (most recent last):\n" + convo
                + "\n\nIs there a question the speaker should answer now?")
        req = AIRequest(cfg, SYSTEM_DETECT, user, 200, self)
        req.succeeded.connect(self._on_detection)
        req.failed.connect(lambda msg: None)  # a failed detection is silent; the next line retries
        req.done.connect(self._detect_requests.discard)
        self._detect_requests.add(req)
        req.start()

    def _on_detection(self, reply):
        question = self._parse_detected_question(reply)
        if not question:
            return
        if self._is_duplicate_question(question):
            return
        marker = "❓ "
        line = marker + question
        self._append_transcript_line(line, mine=False, question=True)
        self._questions.append((line, question))
        if self.auto_answer_chk.isChecked():
            self._handle_question(question, auto=True)
        else:
            # Still surface the document passage so it's ready when they tap.
            if docs is not None and getattr(self, "ref_doc", None) is not None:
                self.search_document(question, answer_after=False)
            self._set_listen_status(f"Question from the other person. Tap it above to answer: "
                                    f"{question[:60]}")

    @staticmethod
    def _parse_detected_question(reply):
        import json as _json
        import re as _re
        if not reply:
            return None
        m = _re.search(r"\{.*\}", reply, _re.DOTALL)
        if m:
            try:
                data = _json.loads(m.group(0))
                q = data.get("question")
                if isinstance(q, str) and q.strip() and q.strip().lower() not in ("null", "none"):
                    return q.strip()
                return None
            except Exception:
                pass
        # Fall back: treat a short plain reply that isn't a refusal as the question.
        low = reply.strip().lower()
        if low in ("null", "none", "no", "{}", '{"question": null}'):
            return None
        return reply.strip()[:300] if "?" in reply else None

    def _is_duplicate_question(self, question):
        ql = question.lower().strip()
        for _line, prev in self._questions[-4:]:
            if listen is not None and listen.text_similarity(prev, question) >= 0.8:
                return True
            if prev.lower().strip() == ql:
                return True
        return False

    def _append_transcript_line(self, line, mine=False, question=False):
        cursor = self.transcript_box.textCursor()
        cursor.movePosition(QTextCursor.MoveOperation.End)
        fmt = QTextCharFormat()
        if question:
            fmt.setForeground(QColor(AMBER))
        else:
            fmt.setForeground(QColor(MUTED) if mine else QColor(self.s["text_color"]))
        if self.transcript_box.document().characterCount() > 1:
            cursor.insertBlock()
        cursor.setCharFormat(fmt)
        cursor.insertText(line)
        self.transcript_box.setTextCursor(cursor)
        bar = self.transcript_box.verticalScrollBar()
        bar.setValue(bar.maximum())

    def _append_transcript_line(self, line, mine=False):
        cursor = self.transcript_box.textCursor()
        cursor.movePosition(QTextCursor.MoveOperation.End)
        fmt = QTextCharFormat()
        fmt.setForeground(QColor(MUTED) if mine else QColor(self.s["text_color"]))
        if self.transcript_box.document().characterCount() > 1:
            cursor.insertBlock()
        cursor.setCharFormat(fmt)
        cursor.insertText(line)
        self.transcript_box.setTextCursor(cursor)
        bar = self.transcript_box.verticalScrollBar()
        bar.setValue(bar.maximum())

    def _transcript_clicked(self, event):
        QPlainTextEdit.mousePressEvent(self.transcript_box, event)
        cursor = self.transcript_box.cursorForPosition(event.pos())
        clicked = cursor.block().text()
        for line, question in self._questions:
            if line == clicked:
                self._answer_question(question)
                return

    def _answer_question(self, question):
        self._handle_question(question, auto=True)

    def _handle_question(self, question, auto=True):
        """Answer as fast as possible: fire everything at once.
          1. Show the instant keyword passage on the prompter (no network).
          2. Start the AI answer straight away (the slow part), not waiting for the lookup.
          3. Start the semantic document lookup in parallel; when it lands it upgrades the
             shown passage and, if it's a good match, triggers one grounded refinement."""
        self.ask_edit.setText(question)
        self._answer_token = getattr(self, "_answer_token", 0) + 1
        token = self._answer_token
        has_doc = docs is not None and getattr(self, "ref_doc", None) is not None

        if has_doc:
            # instant, offline keyword pass — the prompter never waits for the network
            self._instant_doc_pass(question)
            # semantic refine in parallel (may upgrade the passage and ground the answer)
            self.search_document(question, answer_after=True, token=token)

        # fire the AI answer immediately, in parallel with the lookup
        self._ask_grounded(question, passage=self._current_good_passage(), token=token)

    def _current_good_passage(self):
        return getattr(self, "_last_doc_result", None)

    def _instant_doc_pass(self, question):
        if self.ref_doc is None or docs is None:
            return
        try:
            results = self.ref_doc.search_keyword(question, top_k=1)
        except Exception:
            results = []
        if results and results[0].get("good"):
            r = results[0]
            self._show_doc_passage(question, r["heading"], r["text"], "keyword", project=True)
        else:
            self._last_doc_result = None

    def _ask_grounded(self, question, passage=None, token=None):
        """Ask the AI, grounding the answer in a document passage when one is given."""
        context = self._recent_context() if self._history else ""
        self.ask_edit.setText(question)
        if self._ask(extra_context=context, passage=passage, token=token):
            where = "from your document" if passage else "from the script"
            self._set_listen_status(f"Answering ({where}): {question[:50]}")

    def _clear_transcript(self):
        self.transcript_box.clear()
        self._questions = []
        self._history = []
        self._detect_buffer = []

    # ------------------------------------------------------------ reference doc UI
    def _build_doc_section(self, v):
        self.ref_doc = None
        self._doc_workers = set()
        self._embed_stop = False
        self._last_doc_result = None

        v.addWidget(label("Reference document", "section"))
        if docs is None:
            v.addWidget(label("Document search isn't available in this build.", "hint", wrap=True))
            return
        v.addWidget(label("Upload a document (PDF, Word, or text). When the other person asks "
                          "something, CuePilot shows the most relevant passage from it, in your "
                          "document's own words.", "hint", wrap=True))

        self.doc_name_lbl = label("No document loaded.", "hint", wrap=True)
        load_btn = QPushButton("Upload document…")
        load_btn.clicked.connect(self._load_document)
        unload_btn = QPushButton("Remove")
        unload_btn.clicked.connect(self._unload_document)
        v.addLayout(hrow(load_btn, unload_btn, None))
        v.addWidget(self.doc_name_lbl)

        self.contextual_chk = QCheckBox("Smart search: match by meaning, not just keywords "
                                        "(needs an embeddings key)")
        self.contextual_chk.setChecked(bool(self.s["use_contextual"]))
        self.contextual_chk.toggled.connect(self._on_contextual_toggled)
        v.addWidget(self.contextual_chk)

        self.embed_box = QWidget()
        eb = QVBoxLayout(self.embed_box)
        eb.setContentsMargins(0, 0, 0, 0)
        form = QFormLayout()
        form.setHorizontalSpacing(16)
        form.setVerticalSpacing(8)
        self.embed_combo = QComboBox()
        self.embed_combo.addItems(list(docs.EMBED_PROVIDERS))
        self.embed_url = QLineEdit()
        self.embed_key = QLineEdit()
        self.embed_key.setEchoMode(QLineEdit.EchoMode.Password)
        self.embed_key.setPlaceholderText("Paste your embeddings API key")
        self.embed_model = QLineEdit()
        form.addRow("Embeddings", self.embed_combo)
        form.addRow("Server URL", self.embed_url)
        form.addRow("API key", self.embed_key)
        form.addRow("Model", self.embed_model)
        eb.addLayout(form)
        eb.addWidget(label("Smart search embeds your document once when you upload it. Contextual "
                           "search (on by default) also adds a one-line context note to each "
                           "passage using your main AI, which improves matching. Without an "
                           "embeddings key, search still works by keywords.", "hint", wrap=True))
        v.addWidget(self.embed_box)

        self.project_doc_chk = QCheckBox("Show the matching passage on the prompter automatically")
        self.project_doc_chk.setChecked(bool(self.s["project_doc_on_question"]))
        self.project_doc_chk.toggled.connect(lambda on: self.s.__setitem__("project_doc_on_question", on))
        v.addWidget(self.project_doc_chk)

        v.addWidget(label("Most relevant passage:", "hint"))
        self.doc_result = QPlainTextEdit()
        self.doc_result.setReadOnly(True)
        self.doc_result.setPlaceholderText("When a question is detected, the best-matching passage "
                                           "from your document appears here.")
        self.doc_result.setMaximumHeight(150)
        v.addWidget(self.doc_result)
        show_btn = QPushButton("Show on prompter")
        show_btn.clicked.connect(self._project_doc_result)
        append_btn = QPushButton("Add to script")
        append_btn.clicked.connect(self._append_doc_result)
        self.doc_status = label("", "hint", wrap=True)
        v.addLayout(hrow(show_btn, append_btn, None))
        v.addWidget(self.doc_status)

        self.doc_bridge = DocBridge(self)
        self.doc_bridge.loaded.connect(self._on_doc_loaded)
        self.doc_bridge.load_failed.connect(self._on_doc_failed)
        self.doc_bridge.embed_progress.connect(self._on_embed_progress)
        self.doc_bridge.embed_done.connect(self._on_embed_done)
        self.doc_bridge.embed_failed.connect(self._on_embed_failed)
        self.doc_bridge.search_result.connect(self._on_doc_search_result)
        self.doc_bridge.search_empty.connect(self._on_doc_search_empty_answer)
        self._pending_answer = None

        self._loading_embed = True
        self._current_embed = self.s["embed_provider"]
        self.embed_combo.setCurrentText(self._current_embed)
        self._load_embed_profile(self._current_embed)
        self._loading_embed = False
        self.embed_combo.currentTextChanged.connect(self._on_embed_provider_changed)
        for edit in (self.embed_url, self.embed_key, self.embed_model):
            edit.textChanged.connect(self._store_embed_profile)
        self.embed_box.setVisible(self.contextual_chk.isChecked())

        if self.s.get("doc_path") and Path(self.s["doc_path"]).exists():
            QTimer.singleShot(400, lambda: self._load_document(self.s["doc_path"]))

    def _load_embed_profile(self, name):
        prof = self.s["embed_profiles"].get(name, {})
        meta = docs.EMBED_PROVIDERS[name]
        self.embed_url.setText(prof.get("base_url", meta["base_url"]))
        self.embed_key.setText(prof.get("api_key", ""))
        self.embed_model.setText(prof.get("model", meta["model"]))

    def _store_embed_profile(self, *_):
        if getattr(self, "_loading_embed", False):
            return
        self.s["embed_profiles"][self._current_embed] = {
            "base_url": self.embed_url.text().strip(),
            "api_key": self.embed_key.text().strip(),
            "model": self.embed_model.text().strip(),
        }

    def _on_embed_provider_changed(self, name):
        self._store_embed_profile()
        self._current_embed = name
        self.s["embed_provider"] = name
        self._loading_embed = True
        self._load_embed_profile(name)
        self._loading_embed = False

    def _on_contextual_toggled(self, on):
        self.s["use_contextual"] = on
        self.embed_box.setVisible(on)
        if on and self.ref_doc is not None and not self.ref_doc.embedded:
            self._embed_document()

    def _embed_config(self):
        return docs.build_embed_config(self._current_embed, self.embed_url.text(),
                                       self.embed_key.text(), self.embed_model.text())

    def _load_document(self, path=None):
        if not path:
            start = str(Path(self.s.get("doc_path") or Path.home()).parent)
            path, _ = QFileDialog.getOpenFileName(
                self, "Choose a reference document", start,
                "Documents (*.pdf *.docx *.txt *.md);;All files (*)")
        if not path:
            return
        self.doc_name_lbl.setText(f"Loading {Path(path).name}…")

        def job():
            try:
                text = docs.read_document(path)
                doc = docs.ReferenceDoc(Path(path).name)
                count = doc.load_text(text)
                self.ref_doc = doc
                self.s["doc_path"] = path
                self.doc_bridge.loaded.emit(count, Path(path).name)
            except docs.DocError as exc:
                self.doc_bridge.load_failed.emit(str(exc))
            except Exception as exc:
                self.doc_bridge.load_failed.emit(f"Couldn't read the document: {exc}")
        DocWorker(job, self._doc_workers).start()

    def _on_doc_loaded(self, count, name):
        self.doc_name_lbl.setText(f"{name} — {count} passage(s), keyword search ready.")
        self.doc_status.setText("")
        if self.contextual_chk.isChecked():
            self._embed_document()

    def _on_doc_failed(self, msg):
        self.ref_doc = None
        self.doc_name_lbl.setText("No document loaded.")
        self.doc_status.setStyleSheet(f"color: {TALLY};")
        self.doc_status.setText(msg)

    def _unload_document(self):
        self._embed_stop = True
        self.ref_doc = None
        self.s["doc_path"] = ""
        self.doc_name_lbl.setText("No document loaded.")
        self.doc_result.clear()
        self.doc_status.setText("")

    def _embed_document(self):
        if self.ref_doc is None or docs is None:
            return
        cfg, err = self._embed_config()
        if err:
            self.doc_status.setStyleSheet(f"color: {MUTED};")
            self.doc_status.setText("Using keyword search. For smart search, " + err[0].lower() + err[1:])
            return
        context_ai = None
        if self.contextual_chk.isChecked():
            ai_cfg, ai_err = self._ai_config()
            if not ai_err:
                context_ai = {"complete": lambda sysm, usr, mx: ai_complete(ai_cfg, sysm, usr, mx)}
        self._embed_stop = False
        self.doc_status.setStyleSheet(f"color: {MUTED};")
        self.doc_status.setText("Preparing smart search…")
        doc = self.ref_doc

        def job():
            try:
                doc.build_embeddings(
                    cfg, context_ai=context_ai,
                    on_progress=lambda i, n, phase: self.doc_bridge.embed_progress.emit(i, n, phase),
                    should_stop=lambda: self._embed_stop or self.ref_doc is not doc)
                if self.ref_doc is doc and doc.embedded:
                    self.doc_bridge.embed_done.emit(doc.contextual, "")
            except docs.DocError as exc:
                self.doc_bridge.embed_failed.emit(str(exc))
            except Exception as exc:
                self.doc_bridge.embed_failed.emit(f"Smart search setup failed: {exc}")
        DocWorker(job, self._doc_workers).start()

    def _on_embed_progress(self, i, n, phase):
        if phase == "preparing" and n:
            self.doc_status.setText(f"Preparing smart search… passage {i} of {n}")
        elif phase == "embedding":
            self.doc_status.setText("Finishing smart search…")

    def _on_embed_done(self, contextual, _msg):
        kind = "contextual" if contextual else "semantic"
        self.doc_status.setStyleSheet(f"color: {MUTED};")
        self.doc_status.setText(f"Smart search ready ({kind} matching).")

    def _on_embed_failed(self, msg):
        self.doc_status.setStyleSheet(f"color: {TALLY};")
        self.doc_status.setText(msg + " Falling back to keyword search.")

    def search_document(self, question, answer_after=False, token=None):
        """Semantic document lookup on a worker thread, in parallel with the AI answer. When it
        returns it upgrades the shown passage and, if answer_after and the match is good, triggers
        one grounded refinement of the answer."""
        if self.ref_doc is None or docs is None or not question.strip():
            return
        doc = self.ref_doc
        cfg = None
        if doc.embedded:
            cfg, _err = self._embed_config()
        if cfg is None:
            return  # no embeddings: the instant keyword pass already did the work

        def job():
            try:
                results = doc.search_semantic(question, cfg, top_k=1)
            except Exception:
                return  # keep the keyword result already shown
            if self.ref_doc is not doc or not results:
                return
            r = results[0]
            good = "good" if r.get("good") else "weak"
            after = "answer" if answer_after else "noanswer"
            # pack metadata and the passage separately so text can contain anything
            meta = f"{r['method']}|{good}|{after}|{token or 0}"
            self.doc_bridge.search_result.emit(question, meta, r["heading"], r["text"])
        DocWorker(job, self._doc_workers).start()

    def _show_doc_passage(self, question, heading, text, method, project):
        self._last_doc_result = (heading, text)
        shown = (f"[{heading}]\n{text}" if heading else text)
        self.doc_result.setPlainText(shown)
        label_method = {"contextual": "smart", "semantic": "smart",
                        "keyword": "keyword"}.get(method, method)
        self.doc_status.setStyleSheet(f"color: {MUTED};")
        self.doc_status.setText(f"Passage found by {label_method} search for: {question[:50]}")
        if project and self.project_doc_chk.isChecked():
            self._project_doc_result()

    def _on_doc_search_result(self, question, meta, heading, text):
        method, good_flag, after, token = (meta.split("|") + ["", "", "", "0"])[:4]
        good = good_flag == "good"
        if good:
            # Upgrade the shown passage to the semantic match (may differ from the keyword one).
            self._show_doc_passage(question, heading, text, method, project=True)
        elif getattr(self, "_last_doc_result", None) is None:
            # Neither keyword nor semantic found anything good.
            self.doc_result.setPlainText("")
            self.doc_status.setStyleSheet(f"color: {MUTED};")
            self.doc_status.setText("Nothing closely matching in the document. The AI answer is "
                                    "from the script instead.")
        # Grounded refinement: only when this is still the current question and the match is good.
        if after == "answer" and good and str(getattr(self, "_answer_token", 0)) == token:
            self._ask_grounded(question, passage=self._last_doc_result, token=int(token))

    def _on_doc_search_empty_answer(self, question):
        if getattr(self, "_last_doc_result", None) is None:
            self.doc_result.setPlainText("")
            self.doc_status.setText("Nothing matching in the document.")

    def _project_doc_result(self):
        if not self._last_doc_result:
            return
        heading, text = self._last_doc_result
        block = (f"[FROM: {heading}]\n{text}\n[END]" if heading else f"[REFERENCE]\n{text}\n[END]")
        self._push_text()
        line_idx = self.ov.reading_line_index()
        block_doc = self.editor.document().findBlockByNumber(line_idx)
        cur = QTextCursor(block_doc) if block_doc.isValid() else QTextCursor(self.editor.document())
        if block_doc.isValid():
            cur.movePosition(QTextCursor.MoveOperation.EndOfBlock)
        else:
            cur.movePosition(QTextCursor.MoveOperation.End)
        cur.beginEditBlock()
        cur.insertText("\n\n" + block + "\n")
        cur.endEditBlock()
        self._push_text()
        self._set_listen_status("Reference passage added to the prompter at the reading line.")

    def _append_doc_result(self):
        if not self._last_doc_result:
            return
        heading, text = self._last_doc_result
        block = (f"[FROM: {heading}]\n{text}\n[END]" if heading else f"[REFERENCE]\n{text}\n[END]")
        cur = QTextCursor(self.editor.document())
        cur.movePosition(QTextCursor.MoveOperation.End)
        cur.beginEditBlock()
        cur.insertText("\n\n" + block)
        cur.endEditBlock()
        self._push_text()

    def _build_shortcuts_tab(self) -> QWidget:
        w = QWidget()
        v = QVBoxLayout(w)
        v.setContentsMargins(18, 12, 18, 18)
        v.addWidget(label("Anywhere in Windows", "section"))
        v.addWidget(label("These work even while Meet, Zoom or Teams has focus.", "hint"))
        grid = QGridLayout()
        grid.setHorizontalSpacing(14)
        grid.setVerticalSpacing(7)
        for r, (_n, keys, _m, _vk, desc) in enumerate(HOTKEYS):
            grid.addWidget(label(keys, "key"), r, 0, Qt.AlignmentFlag.AlignLeft)
            grid.addWidget(QLabel(desc), r, 1)
        grid.setColumnStretch(1, 1)
        v.addLayout(grid)
        self.hotkey_warning = label("", "hint", wrap=True)
        self.hotkey_warning.setStyleSheet(f"color: {TALLY};")
        self.hotkey_warning.setVisible(False)
        v.addWidget(self.hotkey_warning)

        v.addWidget(label("On the prompter", "section"))
        grid2 = QGridLayout()
        grid2.setHorizontalSpacing(14)
        grid2.setVerticalSpacing(7)
        local = [("Space", "Start or pause"), ("Up / Down", "Faster or slower"),
                 ("Page Up / Down", "Jump back or ahead"), ("Home", "Back to the top"),
                 ("Mouse wheel", "Scroll by hand"), ("Ctrl + wheel", "Text size"),
                 ("Double-click", "Start or pause"), ("Drag", "Move the prompter"),
                 ("Drag an edge", "Resize"), ("Right-click", "More options")]
        for r, (keys, desc) in enumerate(local):
            grid2.addWidget(label(keys, "key"), r, 0, Qt.AlignmentFlag.AlignLeft)
            grid2.addWidget(QLabel(desc), r, 1)
        grid2.setColumnStretch(1, 1)
        v.addLayout(grid2)

        v.addWidget(label("In the script", "section"))
        v.addWidget(label("[PAUSE], [SMILE], [NEXT SLIDE]: anything in square brackets shows as an "
                          "amber cue.\n**word**: underlined for emphasis.", "hint", wrap=True))
        v.addStretch(1)
        return w

    # ------------------------------------------------------------------ settings
    def _set(self, key, value, rebuild=False, keep_ratio=False):
        self.s[key] = value
        if rebuild:
            self.ov.rebuild(keep_ratio=keep_ratio)
        else:
            self.ov.update()
        self._update_info()

    def _on_speed(self, v):
        self.s["speed"] = v
        self.ov.bar.sync()
        self._update_info()

    def _on_capture_toggled(self, on):
        self.s["hide_from_capture"] = on
        self.ov.apply_native()
        self._apply_capture_to_panel()
        self._sync_controls()

    def _apply_capture_to_panel(self):
        if IS_WINDOWS and self.isVisible():
            set_capture_excluded(int(self.winId()), bool(self.s["hide_from_capture"]))

    def _on_click_through(self, on):
        self.s["click_through"] = on
        self.ov.set_click_through(on)
        self.statusBar().showMessage(
            "Click-through on: clicks go to the window behind the prompter." if on
            else "Click-through off: you can drag and resize the prompter.", 5000)

    def _sync_controls(self):
        live = self.ov.playing
        self.play_btn.setText("Pause" if live else "Start")
        self.play_btn.setProperty("live", "true" if live else "false")
        self.play_btn.style().unpolish(self.play_btn)
        self.play_btn.style().polish(self.play_btn)
        for widget, value in ((self.speed_slider, int(self.s["speed"])),
                              (self.size_spin, int(self.s["font_size"]))):
            if widget.value() != value:
                widget.blockSignals(True)
                widget.setValue(value)
                widget.blockSignals(False)
        self.speed_val.setText(str(self.s["speed"]))
        if self.click_chk.isChecked() != self.ov.click_through:
            self.click_chk.blockSignals(True)
            self.click_chk.setChecked(self.ov.click_through)
            self.click_chk.blockSignals(False)
            self.s["click_through"] = self.ov.click_through
        if not IS_WINDOWS:
            state = "unsupported"
        elif not self.s["hide_from_capture"]:
            state = "visible"
        else:
            state = self.ov.capture_state
        self.capture_hint.setText(CAPTURE_HINTS.get(state, ""))
        self._update_info()

    def _on_overlay_visibility(self, visible):
        self.s["overlay_visible"] = visible
        self.show_btn.setText("Hide prompter" if visible else "Show prompter")

    def _tick_info(self):
        if self.ov.playing:
            self._update_info()

    def _update_info(self):
        text = self.editor.toPlainText()
        words = count_words(text)
        if not words:
            self.info.setText("No script yet.")
            return
        spoken = words / 140 * 60
        remaining = (self.ov.max_offset() - self.ov.offset) / max(1, self.s["speed"])
        self.info.setText(f"{words} words. About {fmt_duration(spoken)} spoken at a normal pace; "
                          f"{fmt_duration(self.ov.total_seconds())} at the current scroll speed, "
                          f"{fmt_duration(remaining)} left.")

    # ------------------------------------------------------------------ script
    def _push_text(self):
        self._push_timer.stop()
        text = self.editor.toPlainText()
        self.ov.set_text(text)
        save_last_script(text)
        self._update_info()

    def _replace_editor_text(self, text, start=None, end=None):
        cur = QTextCursor(self.editor.document())
        if start is None:
            cur.select(QTextCursor.SelectionType.Document)
        else:
            cur.setPosition(start)
            cur.setPosition(end, QTextCursor.MoveMode.KeepAnchor)
        cur.beginEditBlock()
        cur.insertText(text)
        cur.endEditBlock()
        self._push_text()

    def _open_file(self):
        start_dir = str(Path(self.s["last_file"]).parent) if self.s["last_file"] else str(Path.home())
        path, _ = QFileDialog.getOpenFileName(self, "Open a script", start_dir,
                                              "Text files (*.txt *.md);;All files (*)")
        if path:
            self._load_path(path)

    def _load_path(self, path):
        try:
            text = Path(path).read_text(encoding="utf-8", errors="replace")
        except Exception as exc:
            self.statusBar().showMessage(f"Couldn't open the file: {exc}", 8000)
            return
        self._replace_editor_text(text)
        self.s["last_file"] = path
        self.ov.restart()
        self.statusBar().showMessage(f"Opened {Path(path).name}", 5000)

    def _save_file(self):
        if self.s["last_file"]:
            self._write_file(self.s["last_file"])
        else:
            self._save_file_as()

    def _save_file_as(self):
        start = self.s["last_file"] or str(Path.home() / "script.txt")
        path, _ = QFileDialog.getSaveFileName(self, "Save the script", start,
                                              "Text files (*.txt);;Markdown (*.md)")
        if path:
            self._write_file(path)

    def _write_file(self, path):
        try:
            Path(path).write_text(self.editor.toPlainText(), encoding="utf-8")
            self.s["last_file"] = path
            self.statusBar().showMessage(f"Saved {Path(path).name}", 5000)
        except Exception as exc:
            self.statusBar().showMessage(f"Couldn't save the file: {exc}", 8000)

    # ------------------------------------------------------------------ overlay
    def _toggle_overlay(self):
        self.ov.setVisible(not self.ov.isVisible())

    def _snap(self):
        if not self.ov.isVisible():
            self.ov.show()
        self.ov.snap_under_webcam()

    def bring_to_front(self):
        self.showNormal()
        self.raise_()
        self.activateWindow()

    # ------------------------------------------------------------------ hotkeys
    def on_hotkey(self, name):
        ov = self.ov
        actions = {
            "toggle_play": ov.toggle_play, "faster": ov.faster, "slower": ov.slower,
            "back": lambda: ov.jump(-1), "forward": lambda: ov.jump(1),
            "bigger": ov.bigger, "smaller": ov.smaller, "restart": ov.restart,
            "toggle_overlay": self._toggle_overlay,
            "toggle_clickthrough": lambda: self.click_chk.setChecked(not self.click_chk.isChecked()),
            "show_panel": self.bring_to_front,
            "ask": self._focus_ask,
            "toggle_listen": self._hotkey_listen,
        }
        fn = actions.get(name)
        if fn:
            fn()

    def on_hotkeys_registered(self, failed):
        self._hotkey_failures = list(failed)
        if failed:
            self.hotkey_warning.setText(
                "Another app already uses " + ", ".join(failed) + ", so those shortcuts won't work "
                "here. Use the prompter toolbar instead, or close the other app and restart CuePilot. "
                "Intel graphics utilities often take Ctrl+Alt+arrow keys.")
            self.hotkey_warning.setVisible(True)
            self.statusBar().showMessage(f"{len(failed)} global shortcut(s) are taken by another app. "
                                         "See the Shortcuts tab.", 10000)

    def _focus_ask(self):
        self.bring_to_front()
        self.tabs.setCurrentWidget(self.ai_tab)
        self.ask_edit.setFocus()
        self.ask_edit.selectAll()

    def _hotkey_listen(self):
        if listen is None or not hasattr(self, "listen_btn") or not self.listen_btn.isEnabled():
            return
        self.tabs.setCurrentWidget(self.ai_tab)
        self._toggle_listen()

    # ------------------------------------------------------------------ AI
    def _load_profile(self, name):
        prof = self.s["ai_profiles"][name]
        self.url_edit.setText(prof.get("base_url", ""))
        self.key_edit.setText(prof.get("api_key", ""))
        self.model_edit.setText(prof.get("model", ""))
        meta = PROVIDERS[name]
        self.key_edit.setPlaceholderText("Paste your API key" if meta["needs_key"] else "Not needed")
        self.provider_hint.setText(meta["hint"])

    def _store_profile(self, *_):
        if self._loading_profile:
            return
        self.s["ai_profiles"][self._current_provider] = {
            "base_url": self.url_edit.text().strip(),
            "api_key": self.key_edit.text().strip(),
            "model": self.model_edit.text().strip(),
        }

    def _on_provider_changed(self, name):
        self._store_profile()
        self._current_provider = name
        self.s["provider"] = name
        self._loading_profile = True
        self._load_profile(name)
        self._loading_profile = False

    def _ai_config(self):
        name = self._current_provider
        meta = PROVIDERS[name]
        url = self.url_edit.text().strip()
        model = self.model_edit.text().strip()
        key = self.key_edit.text().strip()
        if not key and meta["env"]:
            key = os.environ.get(meta["env"], "").strip()
        if not url:
            return None, "Enter the server URL for this provider."
        if not model:
            return None, "Enter a model name."
        if meta["needs_key"] and not key:
            return None, f"Add an API key for {name}. {meta['hint']}"
        return {"kind": meta["kind"], "base_url": url, "api_key": key, "model": model}, None

    def _set_busy(self, busy):
        self._busy = busy
        for b in (self.test_btn, self.gen_btn, self.rewrite_btn, self.ask_btn):
            b.setEnabled(not busy)

    def _status(self, msg, error=False):
        self.ai_status.setStyleSheet(f"color: {TALLY};" if error else f"color: {MUTED};")
        self.ai_status.setText(msg)
        self.statusBar().showMessage(msg, 8000)

    def _run_ai(self, system, user, on_ok, what, max_tokens=1500):
        if self._busy:
            self._status("Still working on the last request.")
            return False
        cfg, err = self._ai_config()
        if err:
            self._status(err, error=True)
            return False
        self._set_busy(True)
        self._status(f"{what}…")
        req = AIRequest(cfg, system, user, max_tokens, self)
        req.succeeded.connect(on_ok)
        req.failed.connect(self._on_ai_failed)
        req.done.connect(self._on_ai_done)
        self._requests.add(req)
        req.start()
        return True

    def _on_ai_failed(self, msg):
        self._status(msg, error=True)

    def _on_ai_done(self, req):
        self._requests.discard(req)
        req.deleteLater()
        self._set_busy(False)

    def _test_connection(self):
        self._run_ai("You are a connection test.", "Reply with the single word OK.",
                     self._on_test_ok, f"Contacting {self._current_provider}", max_tokens=16)

    def _on_test_ok(self, text):
        self._status(f"Connected. {self._current_provider} replied: {text[:40]}")

    def _generate(self):
        topic = self.topic_edit.toPlainText().strip()
        if not topic:
            self._status("Describe the talk first: a topic, bullet points or rough notes.", error=True)
            self.topic_edit.setFocus()
            return
        length = self.length_combo.currentText()
        user = (f"Write a teleprompter script.\n\nTopic and notes:\n{topic}\n\n"
                f"Length: about {LENGTHS[length]} words (roughly {length} when spoken).\n"
                f"Tone: {self.tone_combo.currentText()}.")
        self._run_ai(SYSTEM_SCRIPT, user, self._on_generated, "Writing your script", max_tokens=4000)

    def _on_generated(self, text):
        self._replace_editor_text(clean_ai_text(text))
        self.ov.restart()
        self._status("Script written. Ctrl+Z in the editor brings back the previous one.")

    def _rewrite(self):
        cur = self.editor.textCursor()
        if cur.hasSelection():
            start, end = cur.selectionStart(), cur.selectionEnd()
            passage = cur.selectedText().replace("\u2029", "\n")
        else:
            start = end = None
            passage = self.editor.toPlainText()
        if not passage.strip():
            self._status("There's no text to rewrite yet.", error=True)
            return
        action = self.rewrite_combo.currentText()
        instruction = REWRITE_ACTIONS.get(action) or self.custom_edit.text().strip()
        if not instruction:
            self._status("Type your instruction first.", error=True)
            self.custom_edit.setFocus()
            return
        user = f"Instruction: {instruction}\n\n<passage>\n{passage}\n</passage>"
        if self._run_ai(SYSTEM_REWRITE, user, self._on_rewritten, "Rewriting", max_tokens=4000):
            self._pending = (start, end, passage)

    def _on_rewritten(self, text):
        start, end, original = self._pending or (None, None, "")
        self._pending = None
        text = clean_ai_text(text)
        if start is None:
            unchanged = self.editor.toPlainText() == original
        else:
            cur = QTextCursor(self.editor.document())
            cur.setPosition(min(start, self.editor.document().characterCount() - 1))
            cur.setPosition(min(end, self.editor.document().characterCount() - 1),
                            QTextCursor.MoveMode.KeepAnchor)
            unchanged = cur.selectedText().replace("\u2029", "\n") == original
        if not unchanged:
            self.answer_box.setPlainText(text)
            self._status("The script changed while the AI was working, so the rewrite is in the "
                         "answer box instead.")
            return
        self._replace_editor_text(text, start, end)
        self._status("Rewritten. Ctrl+Z in the editor undoes it.")

    def _ask_clicked(self):
        q = self.ask_edit.text().strip()
        if not q:
            self.ask_edit.setFocus()
            return
        if docs is not None and getattr(self, "ref_doc", None) is not None:
            self._handle_question(q, auto=True)
        else:
            self._ask()

    def _ask(self, extra_context="", passage=None, token=None):
        q = self.ask_edit.text().strip()
        if not q:
            self.ask_edit.setFocus()
            return False
        script = self.editor.toPlainText().strip()
        parts = []
        system = SYSTEM_ASK
        if passage:
            heading, text = passage
            label = f"{heading}\n{text}" if heading else text
            parts.append(f"<document_extract>\n{label}\n</document_extract>")
            system = SYSTEM_GROUNDED
        if self.context_chk.isChecked() and script:
            parts.append(f"<script>\n{script[:8000]}\n</script>")
        if extra_context:
            parts.append(f"<recent_conversation>\n{extra_context}\n</recent_conversation>")
        parts.append(f"Question: {q}")
        user = "\n\n".join(parts)
        grounded = passage is not None
        return self._run_answer_request(system, user, grounded, token)

    def _run_answer_request(self, system, user, grounded, token):
        """Answer requests can run in parallel (a quick plain one and a grounded upgrade), so they
        don't go through the single-slot _run_ai. A grounded answer always wins over a plain one
        for the same question; a plain one never overwrites a grounded one."""
        cfg, err = self._ai_config()
        if err:
            self._status(err, error=True)
            return False
        self._status("Thinking…")
        req = AIRequest(cfg, system, user, 700, self)
        meta = {"grounded": grounded, "token": token if token is not None else -1}
        req.succeeded.connect(lambda text, m=meta: self._on_answer(text, m))
        req.failed.connect(self._on_ai_failed)
        req.done.connect(self._answer_requests.discard)
        self._answer_requests.add(req)
        req.start()
        return True

    def _on_answer(self, text, meta=None):
        meta = meta or {"grounded": False, "token": -1}
        token = meta.get("token", -1)
        grounded = meta.get("grounded", False)
        current = getattr(self, "_answer_token", 0)
        # Drop answers for an older question.
        if token not in (-1, current):
            return
        # A plain answer must not overwrite a grounded one already shown for this question.
        if token == current and not grounded and getattr(self, "_shown_answer_grounded", False) \
                and getattr(self, "_shown_answer_token", None) == token:
            return
        self._shown_answer_grounded = grounded
        self._shown_answer_token = token
        note = "Grounded in your document. " if grounded else ""
        self.answer_box.setPlainText(clean_ai_text(text))
        if self.auto_chk.isChecked():
            self._insert_answer_at_reading_line()
            self._status(note + "Answer added below the reading line.")
        else:
            self._status(note + "Answer ready in the side panel.")

    def _answer_block(self):
        ans = self.answer_box.toPlainText().strip()
        return f"[ANSWER]\n{ans}\n[BACK TO SCRIPT]" if ans else ""

    def _insert_answer_at_reading_line(self):
        block_text = self._answer_block()
        if not block_text:
            return
        self._push_text()  # make sure the prompter matches the editor before mapping lines
        block = self.editor.document().findBlockByNumber(self.ov.reading_line_index())
        if not block.isValid():
            self._append_answer()
            return
        cur = QTextCursor(block)
        cur.movePosition(QTextCursor.MoveOperation.EndOfBlock)
        cur.beginEditBlock()
        cur.insertText("\n\n" + block_text + "\n")
        cur.endEditBlock()
        self._push_text()

    def _append_answer(self):
        block_text = self._answer_block()
        if not block_text:
            return
        cur = QTextCursor(self.editor.document())
        cur.movePosition(QTextCursor.MoveOperation.End)
        cur.beginEditBlock()
        cur.insertText("\n\n" + block_text)
        cur.endEditBlock()
        self._push_text()

    # ------------------------------------------------------------------ window
    def showEvent(self, e):
        super().showEvent(e)
        QTimer.singleShot(0, self._apply_capture_to_panel)

    def closeEvent(self, e):
        if getattr(self, "listener", None) is not None:
            try:
                self.listener.stop()
            except Exception:
                pass
            self.listener = None
        self._embed_stop = True
        self._store_profile()
        if hasattr(self, "stt_combo"):
            self._store_stt_profile()
        if hasattr(self, "embed_combo"):
            self._store_embed_profile()
        self._push_text()
        g = self.ov.geometry()
        self.s["overlay_geometry"] = [g.x(), g.y(), g.width(), g.height()]
        self.s["panel_geometry"] = bytes(self.saveGeometry().toBase64()).decode()
        self.s["overlay_visible"] = self.ov.isVisible()
        save_settings(self.s)
        self.ov.close()
        super().closeEvent(e)
        QApplication.quit()


# =============================================================================
# Startup
# =============================================================================
def apply_dark_palette(app: QApplication):
    pal = QPalette()
    roles = {
        QPalette.ColorRole.Window: BG, QPalette.ColorRole.WindowText: TEXT,
        QPalette.ColorRole.Base: SURFACE, QPalette.ColorRole.AlternateBase: SURFACE_2,
        QPalette.ColorRole.Text: TEXT, QPalette.ColorRole.Button: SURFACE_2,
        QPalette.ColorRole.ButtonText: TEXT, QPalette.ColorRole.Highlight: AMBER,
        QPalette.ColorRole.HighlightedText: INK, QPalette.ColorRole.ToolTipBase: SURFACE_2,
        QPalette.ColorRole.ToolTipText: TEXT, QPalette.ColorRole.PlaceholderText: MUTED,
    }
    for role, colour in roles.items():
        pal.setColor(role, QColor(colour))
    app.setPalette(pal)


def place_overlay(overlay: PrompterOverlay, settings: dict):
    geo = settings.get("overlay_geometry")
    if isinstance(geo, list) and len(geo) == 4:
        x, y, w, h = (int(v) for v in geo)
        if QGuiApplication.screenAt(QPoint(x + w // 2, y + 10)) is not None:
            overlay.setGeometry(x, y, max(300, w), max(110, h))
            return
    g = QGuiApplication.primaryScreen().availableGeometry()
    w = min(760, int(g.width() * 0.6))
    overlay.setGeometry(g.center().x() - w // 2, g.top() + 6, w, 230)


def install_crash_log():
    """With pythonw or the windowed exe there is no console, so errors go to a log file."""
    def hook(exc_type, exc, tb):
        try:
            CONFIG_DIR.mkdir(parents=True, exist_ok=True)
            with open(CONFIG_DIR / "error.log", "a", encoding="utf-8") as f:
                f.write(f"\n--- {datetime.datetime.now():%Y-%m-%d %H:%M:%S} ---\n")
                f.write("".join(traceback.format_exception(exc_type, exc, tb)))
        except Exception:
            pass
        try:
            sys.__excepthook__(exc_type, exc, tb)
        except Exception:
            pass
    sys.excepthook = hook


def create_app(argv):
    if IS_WINDOWS:
        try:
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("CuePilot.Teleprompter")
        except Exception:
            pass
    QGuiApplication.setHighDpiScaleFactorRoundingPolicy(
        Qt.HighDpiScaleFactorRoundingPolicy.PassThrough)
    app = QApplication(argv)
    app.setApplicationName(APP_NAME)
    app.setStyle("Fusion")
    apply_dark_palette(app)
    app.setWindowIcon(make_icon())

    settings = load_settings()
    overlay = PrompterOverlay(settings)
    place_overlay(overlay, settings)
    panel = ControlPanel(settings, overlay)

    hotkeys = GlobalHotkeys()
    hotkeys.triggered.connect(panel.on_hotkey)
    hotkeys.registered.connect(panel.on_hotkeys_registered)
    hotkeys.start()

    panel.show()
    if settings.get("overlay_visible", True):
        overlay.show()
    panel._on_overlay_visibility(overlay.isVisible())
    return app, settings, overlay, panel, hotkeys


def main():
    if "--selftest" in sys.argv:
        import cuepilot_selftest
        code = cuepilot_selftest.run(sys.modules[__name__], sys.argv)
        try:
            sys.stdout and sys.stdout.flush()
        except Exception:
            pass
        os._exit(code)  # skip Qt teardown so the exit code is exactly the test result
    install_crash_log()
    app, _settings, _overlay, _panel, hotkeys = create_app(sys.argv)
    rc = app.exec()
    hotkeys.stop()
    sys.exit(rc)


if __name__ == "__main__":
    main()
