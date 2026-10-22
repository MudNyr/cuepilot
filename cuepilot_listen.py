"""
CuePilot listening mode.

Captures the audio from a call (what comes out of your speakers, and optionally your own
microphone), splits it into spoken phrases, turns each phrase into text, and flags the ones
that sound like questions so the rest of the app can draft an answer.

Transcription back ends:
  * Cloud, via any OpenAI-style /audio/transcriptions endpoint (OpenAI, Groq, ...). Groq's
    whisper-large-v3-turbo is free and fast, so it's the default.
  * Offline, via faster-whisper running on your PC. Nothing leaves the machine, but it needs
    a one-off model download and more processor power.

Audio capture needs the 'soundcard' package; offline transcription needs 'faster-whisper'.
Both are optional. If a back end isn't installed, listening mode explains what to install
rather than failing. This module has no Qt dependency, so it can be unit-tested on its own.
"""
from __future__ import annotations

BUILD = "2026.10.21-8"  # must match APP_BUILD in cuepilot.py

import array
import io
import math
import mimetypes
import os
import re
import threading
import time
import uuid
import wave

SAMPLE_RATE = 16000          # what Whisper models expect
CHUNK_SECONDS = 0.1          # how often we look at the incoming level
CAPTURE_SAMPLE_RATE = 48000  # what we ask the sound card for; resampled down to SAMPLE_RATE

# Voice activity detection, tuned for speech over a call
VAD_START_RMS = 0.012        # loud enough to be speech, not room noise
VAD_END_RMS = 0.008          # drop below this and we start counting silence
VAD_MIN_SPEECH = 0.4         # ignore clicks and short blips (seconds)
VAD_HANG = 0.7               # silence this long ends a phrase (seconds)
VAD_MAX_PHRASE = 14.0        # send a long monologue in pieces (seconds)

# Who said a phrase. THEM is the far end (answered); ME is you (shown but never answered).
THEM = "them"
ME = "me"

ECHO_WINDOW = 4.0            # how far back to look for your own words when filtering echo (seconds)
ECHO_SIMILARITY = 0.72      # a far-end line this similar to a recent line of yours is treated as echo

STT_PROVIDERS = {
    "Groq (free, cloud)": {
        "kind": "cloud", "base_url": "https://api.groq.com/openai/v1",
        "model": "whisper-large-v3-turbo", "env": "GROQ_API_KEY", "needs_key": True,
        "hint": "Free. Get a key at console.groq.com. Fast and accurate, but the call audio is "
                "sent to Groq for transcription."},
    "OpenAI (cloud)": {
        "kind": "cloud", "base_url": "https://api.openai.com/v1",
        "model": "gpt-4o-mini-transcribe", "env": "OPENAI_API_KEY", "needs_key": True,
        "hint": "Uses your OpenAI key. The call audio is sent to OpenAI for transcription. "
                "gpt-4o-transcribe is more accurate; whisper-1 also works."},
    "Offline (faster-whisper)": {
        "kind": "local", "base_url": "", "model": "base.en", "env": "", "needs_key": False,
        "hint": "Runs on your PC, so nothing is sent anywhere. Install with "
                "'pip install faster-whisper'. Models: tiny.en and base.en are fast; "
                "small.en and medium.en are more accurate but slower."},
    "Custom (OpenAI-compatible)": {
        "kind": "cloud", "base_url": "", "model": "", "env": "", "needs_key": False,
        "hint": "Any server with an /audio/transcriptions endpoint. Enter its base URL "
                "(usually ending in /v1)."},
}
DEFAULT_STT_PROVIDER = "Groq (free, cloud)"

QUESTION_WORDS = {
    "what", "why", "how", "when", "where", "who", "which", "whom", "whose", "can", "could",
    "would", "will", "should", "do", "does", "did", "is", "are", "was", "were", "have", "has",
    "had", "may", "might", "shall", "what's", "how's", "why's", "whats", "hows",
}
QUESTION_PHRASES = ("tell me", "tell us", "walk me", "walk us", "explain", "describe",
                    "talk about", "talk us", "give us", "give me", "help me understand",
                    "i'd like to know", "i would like to know", "what about", "how about")


def normalize_base_url(url: str, endpoint_suffixes=()) -> str:
    """Tidy a pasted server address: trim spaces and slashes, fix 'vi' typed for 'v1', and drop
    endpoint paths people paste by mistake (the app adds the endpoint itself)."""
    u = (url or "").strip().rstrip("/")
    u = re.sub(r"/vi(?=/|$)", "/v1", u, flags=re.IGNORECASE)
    changed = True
    while changed and u:
        changed = False
        for suffix in endpoint_suffixes:
            if u.lower().endswith(suffix):
                u = u[: -len(suffix)].rstrip("/")
                changed = True
    return u


STT_SUFFIXES = ("/audio/transcriptions", "/audio/translations", "/audio")


def looks_like_question(text: str) -> bool:
    t = (text or "").strip()
    if not t:
        return False
    low = t.lower()
    if "?" in t:
        return True
    words = re.findall(r"[a-z']+", low)
    if words and words[0] in QUESTION_WORDS:
        return True
    return any(low.startswith(p) for p in QUESTION_PHRASES)


def _word_set(text: str):
    return set(re.findall(r"[a-z0-9\u0900-\u097f']+", (text or "").lower()))


def text_similarity(a: str, b: str) -> float:
    """Jaccard overlap of the words in two phrases, 0..1. Used to spot your own voice
    echoing back from the far end."""
    wa, wb = _word_set(a), _word_set(b)
    if not wa or not wb:
        return 0.0
    inter = len(wa & wb)
    union = len(wa | wb)
    return inter / union if union else 0.0


class EchoFilter:
    """Remembers what you said recently so the far-end stream can drop your voice echoing back."""

    def __init__(self, window=ECHO_WINDOW, threshold=ECHO_SIMILARITY):
        self.window = window
        self.threshold = threshold
        self._recent = []  # (timestamp, text)
        self._lock = threading.Lock()

    def note_own_speech(self, text, now=None):
        now = time.monotonic() if now is None else now
        with self._lock:
            self._recent.append((now, text))
            self._prune(now)

    def is_echo(self, text, now=None):
        now = time.monotonic() if now is None else now
        with self._lock:
            self._prune(now)
            return any(text_similarity(text, mine) >= self.threshold for _ts, mine in self._recent)

    def _prune(self, now):
        self._recent = [(ts, t) for ts, t in self._recent if now - ts <= self.window]


# =============================================================================
# Audio maths (pure, unit-tested)
# =============================================================================
def rms(samples) -> float:
    if len(samples) == 0:
        return 0.0
    total = 0.0
    for s in samples:
        total += s * s
    return math.sqrt(total / len(samples))


def rms_fast(samples) -> float:
    """RMS using numpy when available, falling back to the pure version."""
    try:
        import numpy as np
        if len(samples) == 0:
            return 0.0
        a = np.asarray(samples, dtype=np.float32)
        return float(np.sqrt(np.mean(a * a)))
    except Exception:
        return rms(list(samples))


def downmix(frames, channels: int):
    """Average interleaved channels down to mono. frames is a flat sequence of floats."""
    if channels <= 1:
        return list(frames)
    out = []
    for i in range(0, len(frames) - channels + 1, channels):
        out.append(sum(frames[i:i + channels]) / channels)
    return out


def resample_linear(samples, src_rate: int, dst_rate: int):
    if src_rate == dst_rate or len(samples) == 0:
        return list(samples)
    try:
        import numpy as np
        a = np.asarray(samples, dtype=np.float32)
        n = int(len(a) * dst_rate / src_rate)
        if n <= 0:
            return []
        idx = np.linspace(0, len(a) - 1, n)
        return np.interp(idx, np.arange(len(a)), a).astype(np.float32).tolist()
    except Exception:
        ratio = dst_rate / src_rate
        n = int(len(samples) * ratio)
        out = []
        last = len(samples) - 1
        for i in range(n):
            pos = i / ratio
            i0 = int(pos)
            i1 = min(i0 + 1, last)
            frac = pos - i0
            out.append(samples[i0] * (1 - frac) + samples[i1] * frac)
        return out


def float_to_pcm16(samples) -> bytes:
    try:
        import numpy as np
        a = np.asarray(samples, dtype=np.float32)
        a = np.clip(a, -1.0, 1.0)
        return (a * 32767.0).astype("<i2").tobytes()
    except Exception:
        out = array.array("h")
        for s in samples:
            s = -1.0 if s < -1.0 else (1.0 if s > 1.0 else s)
            out.append(int(s * 32767))
        if array.array("h").itemsize == 2:  # keep little-endian on exotic platforms
            import sys
            if sys.byteorder == "big":
                out.byteswap()
        return out.tobytes()


def pcm16_to_wav(pcm: bytes, rate: int = SAMPLE_RATE) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm)
    return buf.getvalue()


def samples_to_wav(samples, rate: int = SAMPLE_RATE) -> bytes:
    return pcm16_to_wav(float_to_pcm16(samples), rate)


# =============================================================================
# Voice activity detector
# =============================================================================
class VoiceSplitter:
    """Feed it chunks of mono samples at SAMPLE_RATE; it calls on_phrase(samples) for each
    detected spoken phrase."""

    def __init__(self, on_phrase, rate: int = SAMPLE_RATE):
        self.on_phrase = on_phrase
        self.rate = rate
        self.reset()

    def reset(self):
        self.active = False
        self.buf = []
        self.speech_time = 0.0
        self.silence_time = 0.0
        self.level = 0.0

    def feed(self, samples):
        if not samples:
            return
        dur = len(samples) / self.rate
        self.level = rms_fast(samples)
        if not self.active:
            if self.level >= VAD_START_RMS:
                self.active = True
                self.buf = list(samples)
                self.speech_time = dur
                self.silence_time = 0.0
            return
        self.buf.extend(samples)
        if self.level >= VAD_END_RMS:
            self.speech_time += dur
            self.silence_time = 0.0
        else:
            self.silence_time += dur
        total = len(self.buf) / self.rate
        if self.silence_time >= VAD_HANG or total >= VAD_MAX_PHRASE:
            self._emit(forced=total >= VAD_MAX_PHRASE)

    def flush(self):
        if self.active and self.buf:
            self._emit(forced=False)

    def _emit(self, forced: bool):
        phrase = self.buf
        speech = self.speech_time
        self.active = False
        self.buf = []
        self.speech_time = 0.0
        self.silence_time = 0.0
        if speech >= VAD_MIN_SPEECH and phrase:
            try:
                self.on_phrase(phrase)
            except Exception:
                pass


# =============================================================================
# Transcription back ends
# =============================================================================
class STTError(RuntimeError):
    pass


def transcribe_cloud(cfg: dict, wav_bytes: bytes, timeout: float = 60) -> str:
    """POST a WAV file to an OpenAI-style /audio/transcriptions endpoint."""
    import urllib.error
    import urllib.request

    base = normalize_base_url(cfg["base_url"], STT_SUFFIXES)
    if not base:
        raise STTError("No transcription server URL is set.")
    url = base + "/audio/transcriptions"
    boundary = "----CuePilot" + uuid.uuid4().hex
    crlf = b"\r\n"

    def part(name, value):
        return (f'--{boundary}{crlf.decode()}Content-Disposition: form-data; name="{name}"'
                f'{crlf.decode()}{crlf.decode()}{value}{crlf.decode()}').encode()

    body = part("model", cfg["model"]) + part("response_format", "json")
    if cfg.get("language"):
        body += part("language", cfg["language"])
    body += (f'--{boundary}\r\nContent-Disposition: form-data; name="file"; '
             f'filename="audio.wav"\r\nContent-Type: audio/wav\r\n\r\n').encode()
    body += wav_bytes + crlf + f"--{boundary}--{crlf.decode()}".encode()

    headers = {"Content-Type": f"multipart/form-data; boundary={boundary}",
               "Content-Length": str(len(body)),
               "User-Agent": "CuePilot/1.0",
               "Accept": "application/json"}
    if cfg.get("api_key"):
        headers["Authorization"] = "Bearer " + cfg["api_key"]
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            import json
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = _error_detail(exc)
        hint = {401: "check the transcription API key",
                403: "the request was refused; check the key, and on a work network a firewall "
                     "may be blocking it",
                404: "check the server URL and model",
                413: "the audio chunk was too large",
                429: "rate limit reached, try again"}.get(exc.code, "")
        msg = f"Transcription failed (HTTP {exc.code})"
        if detail:
            msg += f": {detail}"
        if hint:
            msg += f" ({hint})"
        raise STTError(msg) from None
    except urllib.error.URLError as exc:
        import urllib.parse
        host = urllib.parse.urlsplit(url).netloc or url
        raise STTError(f"Couldn't reach the transcription server at {host}: {exc.reason}.") from None
    except Exception as exc:
        raise STTError(f"Transcription failed: {exc}") from None
    return (data.get("text") or "").strip()


def _error_detail(exc) -> str:
    """Pull a readable reason out of an HTTP error, JSON or not."""
    try:
        raw = exc.read().decode("utf-8", "replace")
    except Exception:
        return ""
    try:
        import json
        payload = json.loads(raw)
        if isinstance(payload, dict):
            err = payload.get("error") or payload.get("detail") or payload.get("message") or ""
        else:
            err = payload
        text = err.get("message", "") if isinstance(err, dict) else str(err)
    except Exception:
        text = re.sub(r"<[^>]+>", " ", raw)  # plain text or an HTML error page
    return re.sub(r"\s+", " ", text).strip()[:200]


class LocalWhisper:
    """Lazy wrapper around faster-whisper so the import cost is only paid if it's used."""

    def __init__(self, model_name: str):
        self.model_name = model_name
        self._model = None
        self._lock = threading.Lock()

    def _ensure(self):
        if self._model is not None:
            return
        with self._lock:
            if self._model is not None:
                return
            try:
                from faster_whisper import WhisperModel
            except ImportError:
                raise STTError("Offline transcription needs faster-whisper. Install it with: "
                               "pip install faster-whisper") from None
            try:
                self._model = WhisperModel(self.model_name, device="auto", compute_type="int8")
            except Exception as exc:
                raise STTError(f"Couldn't load the offline model '{self.model_name}': {exc}. "
                               "The first use downloads it, which needs an internet connection "
                               "once.") from None

    def transcribe(self, wav_bytes: bytes, language: str = "") -> str:
        self._ensure()
        try:
            segments, _info = self._model.transcribe(
                io.BytesIO(wav_bytes), language=(language or None), vad_filter=False)
            return "".join(seg.text for seg in segments).strip()
        except Exception as exc:
            raise STTError(f"Offline transcription failed: {exc}") from None


# =============================================================================
# Audio capture (Windows loopback via soundcard)
# =============================================================================
def capture_available():
    try:
        import soundcard  # noqa: F401
        return True, ""
    except Exception as exc:
        return False, (f"Listening needs the 'soundcard' package. Install it with: "
                       f"pip install soundcard ({exc.__class__.__name__})")


def list_speakers():
    try:
        import soundcard as sc
        names = []
        try:
            default = sc.default_speaker().name
        except Exception:
            default = None
        for spk in sc.all_speakers():
            names.append(spk.name)
        return names, default
    except Exception:
        return [], None


def list_microphones():
    try:
        import soundcard as sc
        mics = [m for m in sc.all_microphones(include_loopback=False)]
        names = [m.name for m in mics]
        try:
            default = sc.default_microphone().name
        except Exception:
            default = None
        return names, default
    except Exception:
        return [], None


def _resolve_loopback(sc, speaker_name=None):
    """Find a microphone object that records what a speaker plays (WASAPI loopback)."""
    loops = [m for m in sc.all_microphones(include_loopback=True)
             if getattr(m, "isloopback", False)]
    if speaker_name:
        for m in loops:
            if m.name == speaker_name or speaker_name in m.name:
                return m
        try:
            spk = sc.get_speaker(speaker_name)
            for m in loops:
                if spk.name in m.name:
                    return m
        except Exception:
            pass
    try:
        default = sc.default_speaker().name
        for m in loops:
            if default in m.name:
                return m
    except Exception:
        pass
    return loops[0] if loops else None


class _StreamReader(threading.Thread):
    """Records one device, resamples to SAMPLE_RATE mono, and feeds a VoiceSplitter.
    Each phrase is tagged with a label (THEM or ME)."""

    def __init__(self, device, label, splitter, stop_event, on_error, on_level=None):
        super().__init__(name=f"cuepilot-capture-{label}", daemon=True)
        self.device = device
        self.label = label
        self.splitter = splitter
        self._stop = stop_event
        self.on_error = on_error
        self.on_level = on_level or (lambda level: None)

    def run(self):
        import numpy as np
        frames = int(CAPTURE_SAMPLE_RATE * CHUNK_SECONDS)
        try:
            with self.device.recorder(samplerate=CAPTURE_SAMPLE_RATE, blocksize=frames) as stream:
                while not self._stop.is_set():
                    data = stream.record(numframes=frames)
                    mono = data.mean(axis=1) if data.ndim > 1 else data
                    down = resample_linear(mono.tolist(), CAPTURE_SAMPLE_RATE, SAMPLE_RATE)
                    self.on_level(rms_fast(down))
                    self.splitter.feed(down)
        except Exception as exc:
            self.on_error(f"{self.label} audio capture stopped: {exc}")
        finally:
            self.splitter.flush()


class AudioCapture:
    """Captures the far end (speaker loopback) and, optionally, your microphone as two
    SEPARATE streams. Each detected phrase is reported with a label so the far end can be
    answered and your own voice can be excluded."""

    def __init__(self, on_phrase, speaker_name=None, mic_name=None, capture_mic=False,
                 on_error=None, on_level=None):
        # on_phrase(label, samples) -> None
        self.on_phrase = on_phrase
        self.speaker_name = speaker_name
        self.mic_name = mic_name
        self.capture_mic = capture_mic
        self.on_error = on_error or (lambda msg: None)
        self.on_level = on_level or (lambda level: None)
        self._stop = threading.Event()
        self._readers = []

    def start(self):
        try:
            import soundcard as sc
        except Exception as exc:
            self.on_error(f"Listening needs the 'soundcard' package: pip install soundcard ({exc})")
            return False
        try:
            import numpy  # noqa: F401
        except Exception:
            self.on_error("Listening needs NumPy: pip install numpy")
            return False

        loop = _resolve_loopback(sc, self.speaker_name)
        if loop is None:
            self.on_error("No speaker loopback device was found. On Windows 10 or 11 this should "
                          "exist automatically; check that a speaker is set as the default output.")
            return False

        # The far end: level meter follows this stream.
        them_splitter = VoiceSplitter(lambda s: self.on_phrase(THEM, s))
        self._readers.append(_StreamReader(loop, THEM, them_splitter, self._stop,
                                           self.on_error, self.on_level))

        if self.capture_mic:
            mic = None
            try:
                mic = sc.get_microphone(self.mic_name) if self.mic_name else sc.default_microphone()
            except Exception:
                self.on_error("Couldn't open the microphone; listening to the call audio only.")
            if mic is not None:
                me_splitter = VoiceSplitter(lambda s: self.on_phrase(ME, s))
                self._readers.append(_StreamReader(mic, ME, me_splitter, self._stop, self.on_error))

        for r in self._readers:
            r.start()
        return True

    def stop(self):
        self._stop.set()

    def is_alive(self):
        return any(r.is_alive() for r in self._readers)


# =============================================================================
# Listener: ties capture, splitting and transcription together
# =============================================================================
class Listener:
    """Owns the capture streams and a transcription worker. Reports each spoken phrase with
    who said it, so the far end (THEM) can be answered while your own voice (ME) is shown but
    never answered. On speakers, your voice echoes back through the far end; those echoes are
    filtered out using what your microphone heard moments earlier."""

    def __init__(self, stt_config: dict, on_transcript, on_status, on_level=None,
                 capture_mic=False, speaker_name=None, mic_name=None, suppress_echo=True):
        self.cfg = stt_config
        # on_transcript(text, is_question, speaker) -> None, where speaker is THEM or ME
        self.on_transcript = on_transcript
        self.on_status = on_status            # (message, is_error) -> None
        self.on_level = on_level or (lambda level: None)
        self.capture_mic = capture_mic
        self.speaker_name = speaker_name
        self.mic_name = mic_name
        self.suppress_echo = suppress_echo and capture_mic
        self._queue = []                      # (label, wav)
        self._queue_lock = threading.Lock()
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._capture = None
        self._worker = None
        self._local = None
        self._echo = EchoFilter()
        if self.cfg["kind"] == "local":
            self._local = LocalWhisper(self.cfg["model"])

    # --------- lifecycle
    def start(self):
        self._capture = AudioCapture(self._on_phrase, speaker_name=self.speaker_name,
                                     mic_name=self.mic_name, capture_mic=self.capture_mic,
                                     on_error=lambda m: self.on_status(m, True),
                                     on_level=self.on_level)
        self._worker = threading.Thread(target=self._transcribe_loop, name="cuepilot-stt",
                                        daemon=True)
        self._worker.start()
        started = self._capture.start()
        if started:
            self.on_status("Listening. The other person's words will appear here.", False)
        return started

    def stop(self):
        self._stop.set()
        self._wake.set()
        if self._capture:
            self._capture.stop()

    def is_running(self):
        return self._capture is not None and self._capture.is_alive()

    # --------- phrase handling
    def _on_phrase(self, label, samples):
        ended_at = time.monotonic()  # when this phrase finished; compared across the two streams
        wav = samples_to_wav(samples)
        with self._queue_lock:
            # Keep your own phrases prioritised so echo notes are recorded before the
            # matching far-end phrase is transcribed; otherwise drop the oldest far-end audio.
            self._queue.append((label, wav, ended_at))
            if len(self._queue) > 16:
                them = [item for item in self._queue if item[0] == THEM]
                mine = [item for item in self._queue if item[0] == ME]
                self._queue = (mine + them)[-16:]
        self._wake.set()

    def _transcribe_one(self, wav):
        if self.cfg["kind"] == "local":
            return self._local.transcribe(wav, self.cfg.get("language", ""))
        return transcribe_cloud(self.cfg, wav)

    # When both streams hear the same words, timing says which copy is real:
    #  * Bleed: on speakers, the mic hears the far end at the same moment the speakers play it.
    #    The mic copy is fake; the far-end line is kept. This is the common case.
    #  * Echo: the call sends your own voice back. It arrives after a network delay, so your mic
    #    phrase ended clearly earlier. The far-end copy is fake and dropped.
    ECHO_LAG = 0.3        # your phrase must end at least this much earlier to count as echoed back
    BLEED_WINDOW = 1.0    # far-end and mic phrases ending this close together are the same sound
    ME_HOLD = 1.5         # hold your lines briefly so a matching far-end line can claim them
    MATCH_KEEP = 8.0      # how long to remember phrases for matching (seconds)

    def _transcribe_loop(self):
        self._recent_them = []   # (end_time, text)
        self._recent_me = []     # (end_time, text)
        self._pending_me = []    # (end_time, text, transcribed_at)
        while not self._stop.is_set():
            self._wake.wait(timeout=0.3)
            self._wake.clear()
            while not self._stop.is_set():
                with self._queue_lock:
                    item = self._queue.pop(0) if self._queue else None
                if item is None:
                    break
                label, wav = item[0], item[1]
                t_end = item[2] if len(item) > 2 else time.monotonic()
                try:
                    text = self._transcribe_one(wav)
                except STTError as exc:
                    self.on_status(str(exc), True)
                    continue
                except Exception as exc:
                    self.on_status(f"Transcription error: {exc}", True)
                    continue
                text = (text or "").strip()
                if not _is_meaningful(text):
                    continue
                if label == ME:
                    self._handle_me(text, t_end)
                else:
                    self._handle_them(text, t_end)
            self._flush_pending_me()
        self._flush_pending_me(force=True)

    @staticmethod
    def _overlap(a, b):
        """Share of the shorter phrase's words found in the longer one (handles partial copies)."""
        wa, wb = _word_set(a), _word_set(b)
        if not wa or not wb:
            return 0.0
        return len(wa & wb) / min(len(wa), len(wb))

    def _handle_me(self, text, t_end):
        if not self.suppress_echo:
            self.on_transcript(text, False, ME)
            return
        # Bleed: the far end already said this at the same moment, so the mic just overheard it.
        for te, tt in self._recent_them:
            if abs(t_end - te) <= self.BLEED_WINDOW and self._overlap(text, tt) >= ECHO_SIMILARITY:
                return
        self._pending_me.append((t_end, text, time.monotonic()))

    def _handle_them(self, text, t_end):
        if self.suppress_echo:
            # Any of your held lines that are really this far-end sound overheard: discard them.
            kept = []
            for te, mt, ts in self._pending_me:
                if abs(t_end - te) <= self.BLEED_WINDOW and t_end - te < self.ECHO_LAG \
                        and self._overlap(mt, text) >= ECHO_SIMILARITY:
                    continue
                kept.append((te, mt, ts))
            self._pending_me = kept
            # Echo: you said this clearly earlier, and the call has sent it back.
            mine = [(te, mt) for te, mt, _ts in self._pending_me] + self._recent_me
            for te, mt in mine:
                lag = t_end - te
                if self.ECHO_LAG <= lag <= ECHO_WINDOW and text_similarity(text, mt) >= ECHO_SIMILARITY:
                    self._remember(self._recent_them, t_end, text)
                    return
        self._remember(self._recent_them, t_end, text)
        self.on_transcript(text, looks_like_question(text), THEM)

    def _flush_pending_me(self, force=False):
        if not getattr(self, "_pending_me", None):
            return
        now = time.monotonic()
        still = []
        for te, mt, ts in self._pending_me:
            if force or now - ts >= self.ME_HOLD:
                self._remember(self._recent_me, te, mt)
                self.on_transcript(mt, False, ME)
            else:
                still.append((te, mt, ts))
        self._pending_me = still

    def _remember(self, lst, t_end, text):
        lst.append((t_end, text))
        cutoff = time.monotonic() - self.MATCH_KEEP
        lst[:] = [(t, x) for t, x in lst if t >= cutoff]


# Whisper emits these for near-silence or background noise; drop them.
_NOISE = {"", ".", "..", "...", "you", "thank you", "thanks for watching", "bye",
          "thank you.", "you.", "[blank_audio]", "(silence)", "um", "uh"}


def _is_meaningful(text: str) -> bool:
    t = text.strip().lower()
    if t in _NOISE:
        return False
    return len(re.findall(r"[a-z0-9\u0900-\u097f]+", t, re.I)) >= 1 and len(t) >= 2


def build_stt_config(provider_name: str, url: str, key: str, model: str, language: str = ""):
    """Validate the fields and return (config, error_message)."""
    meta = STT_PROVIDERS.get(provider_name)
    if meta is None:
        return None, "Unknown transcription provider."
    kind = meta["kind"]
    if not model.strip():
        return None, "Enter a transcription model name."
    if kind == "local":
        return {"kind": "local", "model": model.strip(), "language": language.strip()}, None
    if not url.strip():
        return None, "Enter the transcription server URL."
    if not key.strip() and meta["env"]:
        key = os.environ.get(meta["env"], "")
    if meta["needs_key"] and not key.strip():
        return None, f"Add an API key for {provider_name}. {meta['hint']}"
    return {"kind": "cloud", "base_url": normalize_base_url(url, STT_SUFFIXES),
            "api_key": key.strip(), "model": model.strip(), "language": language.strip()}, None


mimetypes.add_type("audio/wav", ".wav")
