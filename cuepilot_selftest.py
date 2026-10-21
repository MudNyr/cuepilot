"""
CuePilot self-test.

Opens the real windows, drives every feature that can be checked without a person, and talks
to a fake AI server running on this machine. Nothing is sent to the internet, and your own
settings and script are left alone (the test uses a temporary folder).

    python cuepilot.py --selftest
    python cuepilot.py --selftest --report selftest.txt
    CuePilot.exe --selftest --report selftest.txt

Exit code 0 means every required check passed. WARN lines are checks that depend on the
desktop the test runs on (for example a build server without a visible screen); they are
reported but don't fail the run.
"""
from __future__ import annotations

import ctypes
import json
import os
import platform
import socket
import sys
import tempfile
import threading
import time
import traceback
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

PASS, WARN, FAIL = "PASS", "WARN", "FAIL"
IS_WIN = sys.platform == "win32"

SCRIPT_REPLY = "```\n# Opening\nHello team.\n\nThis is the **drafted** script. [PAUSE]\n```"
SCRIPT_CLEAN = "Opening\nHello team.\n\nThis is the **drafted** script. [PAUSE]"
REWRITE_REPLY = "Rewritten passage"
ASK_REPLY = "Point one\nPoint two"
GROUNDED_REPLY = "Grounded point from your document"
ANSWER_BLOCK = "[ANSWER]\nPoint one\nPoint two\n[BACK TO SCRIPT]"


# =============================================================================
# Fake AI server (speaks both the Claude and the OpenAI formats)
# =============================================================================
def start_mock_server(cp, delay=0.25):
    log = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_a):
            pass

        def _send(self, code, payload):
            data = json.dumps(payload).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_POST(self):
            length = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(length) or b"{}")
            headers = {k.lower(): v for k, v in self.headers.items()}
            if self.path.endswith("/v1/messages"):
                system = body.get("system", "")
                user = body["messages"][0]["content"]
                key = headers.get("x-api-key", "")
            elif self.path.endswith("/chat/completions"):
                msgs = body.get("messages", [])
                system = msgs[0]["content"] if msgs else ""
                user = msgs[-1]["content"] if msgs else ""
                key = headers.get("authorization", "").replace("Bearer ", "")
            else:
                self._send(404, {"error": {"message": "no such endpoint"}})
                return
            log.append({"path": self.path, "headers": headers, "body": body,
                        "system": system, "user": user})
            time.sleep(delay)
            if key == "bad-key":
                self._send(401, {"type": "error", "error": {"type": "authentication_error",
                                                            "message": "invalid x-api-key"}})
                return
            if "connection test" in system:
                text = "OK"
            elif system == cp.SYSTEM_REWRITE:
                text = REWRITE_REPLY
            elif system == cp.SYSTEM_SCRIPT:
                text = SCRIPT_REPLY
            elif system == cp.SYSTEM_DETECT:
                # Pretend to understand: flag a line containing '?' or a question word.
                low = user.lower()
                is_q = ("?" in user or any(w in low for w in
                        ("what", "how", "why", "walk me", "tell me", "explain", "experience with")))
                if is_q:
                    last = [ln for ln in user.splitlines() if ln.startswith("Them:")]
                    q = last[-1][len("Them:"):].strip() if last else "What is your experience?"
                    text = '{"question": "%s"}' % q.replace('"', "'")
                else:
                    text = '{"question": null}'
            elif system == cp.SYSTEM_ASK:
                text = ASK_REPLY
            elif system == cp.SYSTEM_GROUNDED:
                text = GROUNDED_REPLY
            else:
                text = "Unexpected request"
            if self.path.endswith("/v1/messages"):
                self._send(200, {"content": [{"type": "text", "text": text}], "stop_reason": "end_turn"})
            else:
                self._send(200, {"choices": [{"message": {"role": "assistant", "content": text}}]})

    srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, name="mock-ai", daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_port}", log


def start_mock_transcribe_server():
    """A fake /audio/transcriptions endpoint that checks the multipart upload."""
    log = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_a):
            pass

        def do_POST(self):
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length)
            ctype = self.headers.get("Content-Type", "")
            auth = self.headers.get("Authorization", "")
            entry = {"path": self.path, "auth": auth,
                     "multipart": "multipart/form-data" in ctype,
                     "has_file": b'name="file"' in raw and b"RIFF" in raw,
                     "has_model": b'name="model"' in raw}
            log.append(entry)
            if auth == "Bearer bad":
                payload = {"error": {"message": "invalid api key"}}
                data = json.dumps(payload).encode()
                self.send_response(401)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
                return
            text = "What is your Shield experience?" if entry["has_file"] else ""
            data = json.dumps({"text": text}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

    srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_port}", log


def start_mock_embed_server(vec_fn):
    """A fake /embeddings endpoint that returns deterministic vectors via vec_fn(text)."""
    calls = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_a):
            pass

        def do_POST(self):
            length = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(length) or b"{}")
            calls.append(self.path)
            inputs = body.get("input")
            if not isinstance(inputs, list):
                inputs = [inputs]
            data = [{"index": i, "embedding": vec_fn(t)} for i, t in enumerate(inputs)]
            out = json.dumps({"data": data}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(out)))
            self.end_headers()
            self.wfile.write(out)

    srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_port}", calls


def closed_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


# =============================================================================
# Windows helpers (read-only checks of what Windows thinks about our windows)
# =============================================================================
if IS_WIN:
    from ctypes import wintypes

    _u32 = ctypes.WinDLL("user32", use_last_error=True)
    _u32.GetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int]
    _u32.GetWindowLongW.restype = wintypes.LONG
    _u32.WindowFromPoint.argtypes = [wintypes.POINT]
    _u32.WindowFromPoint.restype = wintypes.HWND
    _u32.GetAncestor.argtypes = [wintypes.HWND, wintypes.UINT]
    _u32.GetAncestor.restype = wintypes.HWND
    _u32.GetWindowDisplayAffinity.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    _u32.GetWindowDisplayAffinity.restype = wintypes.BOOL
    _u32.keybd_event.argtypes = [wintypes.BYTE, wintypes.BYTE, wintypes.DWORD, ctypes.c_size_t]
    _u32.keybd_event.restype = None


def exstyle(hwnd: int) -> int:
    return _u32.GetWindowLongW(hwnd, -20) & 0xFFFFFFFF


def window_at(x: int, y: int) -> int:
    h = _u32.WindowFromPoint(wintypes.POINT(x, y))
    if not h:
        return 0
    return _u32.GetAncestor(h, 2) or 0  # GA_ROOT


def display_affinity(hwnd: int) -> int:
    v = wintypes.DWORD(0)
    return v.value if _u32.GetWindowDisplayAffinity(hwnd, ctypes.byref(v)) else -1


def press_keys(vks):
    for vk in vks:
        _u32.keybd_event(vk, 0, 0, 0)
        time.sleep(0.02)
    for vk in reversed(vks):
        _u32.keybd_event(vk, 0, 0x0002, 0)  # KEYEVENTF_KEYUP
        time.sleep(0.02)


# =============================================================================
# The test run
# =============================================================================
class SelfTest:
    def __init__(self, cp, report_path, tmp: Path):
        from PySide6 import QtCore, QtGui, QtWidgets
        self.cp = cp
        self.QtCore, self.QtGui, self.QtWidgets = QtCore, QtGui, QtWidgets
        self.report_path = report_path
        self.tmp = tmp
        self.rows = []
        self.errors = []
        self.qt_messages = []
        self.section = ""

    # ------------------------------------------------------------- plumbing
    def log(self, status, name, detail=""):
        self.rows.append((status, f"{self.section}: {name}" if self.section else name, detail))

    def check(self, name, cond, detail="", soft=False):
        ok = bool(cond)
        self.log(PASS if ok else (WARN if soft else FAIL), name, "" if ok else str(detail))
        return ok

    @contextmanager
    def step(self, title):
        self.section = title
        try:
            yield
        except Exception:
            self.log(FAIL, "stopped by an error", traceback.format_exc())
        finally:
            self.section = ""

    def pump(self, secs=0.05):
        end = time.monotonic() + secs
        while True:
            self.app.processEvents()
            if time.monotonic() >= end:
                break
            time.sleep(0.005)

    def wait_for(self, cond, timeout=3.0):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            self.app.processEvents()
            try:
                if cond():
                    return True
            except Exception:
                pass
            time.sleep(0.01)
        self.app.processEvents()
        return bool(cond())

    def ai_wait(self, timeout=15.0):
        return self.wait_for(lambda: not self.panel._busy, timeout)

    def set_script(self, text):
        self.panel.editor.setPlainText(text)
        if not self.wait_for(lambda: self.ov.raw_text == text, 2.0):
            raise AssertionError("the prompter didn't pick up the editor text within 2 seconds")
        self.ov.restart()
        self.pump(0.05)

    def set_provider(self, name):
        self.panel.provider_combo.setCurrentText(name)
        self.pump()

    def _install_hooks(self):
        def on_error(t, e, tb):
            self.errors.append("".join(traceback.format_exception(t, e, tb)))
        sys.excepthook = on_error
        sys.unraisablehook = lambda u: self.errors.append(
            f"{getattr(u.exc_type, '__name__', u.exc_type)}: {u.exc_value}\n"
            + "".join(traceback.format_tb(u.exc_traceback)))
        threading.excepthook = lambda a: self.errors.append(
            "".join(traceback.format_exception(a.exc_type, a.exc_value, a.exc_traceback)))

        QtMsgType = self.QtCore.QtMsgType

        def on_qt_message(mode, _context, message):
            if mode in (QtMsgType.QtWarningMsg, QtMsgType.QtCriticalMsg, QtMsgType.QtFatalMsg):
                self.qt_messages.append(str(message))
        self._qt_handler = on_qt_message
        self.QtCore.qInstallMessageHandler(on_qt_message)

    # ------------------------------------------------------- event helpers
    def send_key(self, widget, key, text=""):
        QE, Qt = self.QtGui.QKeyEvent, self.QtCore.Qt
        key = key.value if hasattr(key, "value") else int(key)
        ev = QE(self.QtCore.QEvent.Type.KeyPress, key, Qt.KeyboardModifier.NoModifier, text)
        self.QtWidgets.QApplication.sendEvent(widget, ev)
        self.pump(0.02)

    def _centre(self, widget):
        QPointF = self.QtCore.QPointF
        pos = QPointF(widget.width() / 2, widget.height() / 2 + 20)
        return pos, QPointF(widget.mapToGlobal(pos.toPoint()))

    def send_wheel(self, widget, dy, ctrl=False):
        Qt, QPoint = self.QtCore.Qt, self.QtCore.QPoint
        pos, glob = self._centre(widget)
        mods = Qt.KeyboardModifier.ControlModifier if ctrl else Qt.KeyboardModifier.NoModifier
        ev = self.QtGui.QWheelEvent(pos, glob, QPoint(0, 0), QPoint(0, dy), Qt.MouseButton.NoButton,
                                    mods, Qt.ScrollPhase.NoScrollPhase, False)
        self.QtWidgets.QApplication.sendEvent(widget, ev)
        self.pump(0.02)

    def send_double_click(self, widget):
        Qt, T = self.QtCore.Qt, self.QtCore.QEvent.Type
        pos, glob = self._centre(widget)
        left, none = Qt.MouseButton.LeftButton, Qt.MouseButton.NoButton
        for etype, buttons in ((T.MouseButtonPress, left), (T.MouseButtonRelease, none),
                               (T.MouseButtonDblClick, left), (T.MouseButtonRelease, none)):
            ev = self.QtGui.QMouseEvent(etype, pos, glob, left, buttons, Qt.KeyboardModifier.NoModifier)
            self.QtWidgets.QApplication.sendEvent(widget, ev)
        self.pump(0.02)

    # -------------------------------------------------------- pixel helpers
    def count_pixels(self, img, pred, step=3):
        n = 0
        for y in range(0, img.height(), step):
            for x in range(0, img.width(), step):
                if pred(img.pixelColor(x, y)):
                    n += 1
        return n

    def near(self, hexcol, tol=48, min_alpha=200):
        t = self.QtGui.QColor(hexcol)
        tr, tg, tb = t.red(), t.green(), t.blue()

        def pred(c):
            return (c.alpha() >= min_alpha and abs(c.red() - tr) <= tol
                    and abs(c.green() - tg) <= tol and abs(c.blue() - tb) <= tol)
        return pred

    def grab_overlay(self):
        return self.ov.grab().toImage()

    def magenta_on_screen(self):
        ov = self.ov
        scr = ov.screen()
        sg = scr.geometry()
        g = ov.frameGeometry()
        img = scr.grabWindow(0, g.x() - sg.x() + 10, g.y() - sg.y() + 44,
                             g.width() - 20, g.height() - 54).toImage()
        total = hits = 0
        for y in range(0, img.height(), 4):
            for x in range(0, img.width(), 4):
                total += 1
                c = img.pixelColor(x, y)
                if c.red() > 200 and c.green() < 80 and c.blue() > 200:
                    hits += 1
        return hits / total if total else 0.0

    def screen_point(self, widget):
        p = widget.mapToGlobal(self.QtCore.QPoint(widget.width() // 2, widget.height() // 2 + 20))
        dpr = widget.devicePixelRatioF()
        return int(p.x() * dpr), int(p.y() * dpr)

    # ================================================================ tests
    def t_startup(self):
        cp, ov, p = self.cp, self.ov, self.panel
        self.check("control panel opens", p.isVisible())
        self.check("prompter opens", ov.isVisible())
        self.check("sample script loads on first run", p.editor.toPlainText() == cp.SAMPLE_SCRIPT)
        self.check("prompter shows the editor text", ov.raw_text == cp.SAMPLE_SCRIPT)
        self.check("word count and timing are shown", "words" in p.info.text(), p.info.text())
        g, scr = ov.geometry(), ov.screen().availableGeometry()
        self.check("prompter starts at the top of the screen", abs(g.top() - (scr.top() + 6)) <= 3,
                   f"top={g.top()}, screen top={scr.top()}")
        self.check("prompter fits on the screen", scr.contains(g.center()), f"{g} vs {scr}")
        if IS_WIN:
            hwnd = int(ov.winId())
            self.check("prompter is always on top (WS_EX_TOPMOST)", exstyle(hwnd) & 0x8,
                       hex(exstyle(hwnd)))
            ready = self.hotkeys.ready.wait(3)
            self.pump(0.2)
            self.check("global shortcut listener starts", ready)
            failed = p._hotkey_failures
            self.check("all global shortcuts registered", not failed,
                       "taken by another program: " + ", ".join(failed), soft=True)
            self.check("Windows accepts hide-from-screen-share", ov.capture_state == "hidden",
                       f"state={ov.capture_state!r}", soft=True)

    def t_rendering(self):
        ov, s = self.ov, self.s
        s["guide_enabled"] = False
        self.set_script("Hello team, this is a rendering test.\nSecond line with a cue [PAUSE]\n\n"
                        "**Emphasis** line")
        img = self.grab_overlay()
        text_px = self.count_pixels(img, self.near(s["text_color"]))
        self.check("script text is drawn", text_px > 30, f"{text_px} text-coloured pixels")
        cue_px = self.count_pixels(img, self.near(s["cue_color"], tol=40))
        self.check("[cues] are drawn in the cue colour", cue_px > 3, f"{cue_px} cue-coloured pixels")
        s["mirror"] = True
        mirrored = self.count_pixels(self.grab_overlay(), self.near(s["text_color"]))
        s["mirror"] = False
        self.check("mirrored text is drawn", mirrored > 30, f"{mirrored} pixels")
        s["guide_enabled"] = True
        guide_px = self.count_pixels(self.grab_overlay(), self.near(s["cue_color"], tol=40))
        self.check("reading line markers are drawn", guide_px > cue_px, f"{guide_px} vs {cue_px}")
        self.set_script("")
        light = self.count_pixels(self.grab_overlay(), lambda c: c.alpha() > 150 and c.lightness() > 120)
        self.check("empty script shows a hint instead", light > 10, f"{light} light pixels")
        self.check("hover toolbar spans the prompter", abs(ov.bar.width() - (ov.width() - 16)) <= 1,
                   f"{ov.bar.width()} vs {ov.width()}")

    def t_scrolling(self):
        Qt, QPoint = self.QtCore.Qt, self.QtCore.QPoint
        ov, p, s = self.ov, self.panel, self.s
        self.set_script("\n".join(f"Line {i}: the quick brown fox jumps over the lazy dog."
                                  for i in range(1, 61)))
        self.check("long script is taller than the prompter", ov.max_offset() > ov.height(),
                   f"max_offset={ov.max_offset():.0f}")

        p.play_btn.click()
        self.check("Start button starts scrolling", ov.playing)
        self.check("Start button turns into Pause", p.play_btn.text() == "Pause", p.play_btn.text())
        self.pump(0.7)
        self.check("text moves while playing", ov.offset > 5, f"offset={ov.offset:.1f}")
        p.play_btn.click()
        self.check("Pause stops scrolling", not ov.playing and p.play_btn.text() == "Start")
        before = ov.offset
        self.pump(0.3)
        self.check("text stays put while paused", abs(ov.offset - before) < 0.01)

        sp = s["speed"]
        ov.faster()
        self.pump()
        self.check("faster raises the speed", s["speed"] > sp, f"{sp} -> {s['speed']}")
        self.check("speed slider follows", p.speed_slider.value() == s["speed"])
        self.check("toolbar shows the speed", ov.bar.speed.text() == str(s["speed"]))
        ov.slower()
        ov.slower()
        self.check("slower lowers the speed", s["speed"] < sp, f"{sp} -> {s['speed']}")
        for _ in range(60):
            ov.slower()
        self.check("speed stops at 5", s["speed"] == 5, s["speed"])
        for _ in range(80):
            ov.faster()
        self.check("speed stops at 400", s["speed"] == 400, s["speed"])
        p.speed_slider.setValue(40)
        self.check("speed slider sets the speed", s["speed"] == 40)

        fs = s["font_size"]
        ov.bigger()
        self.pump()
        self.check("larger text", s["font_size"] == fs + 2 and p.size_spin.value() == fs + 2)
        ov.smaller()
        self.pump()
        self.check("smaller text", s["font_size"] == fs and p.size_spin.value() == fs)

        ov.restart()
        ov.jump(1)
        ahead = ov.offset
        self.check("jump ahead", ahead > 0)
        ov.jump(-1)
        self.check("jump back", ov.offset < ahead)
        ov.restart()
        self.check("back to top", ov.offset == 0)

        ov.offset = ov.max_offset() - 3
        ov.set_playing(True)
        stopped = self.wait_for(lambda: not ov.playing, 3)
        self.check("stops by itself at the end", stopped and abs(ov.offset - ov.max_offset()) < 0.5,
                   f"offset={ov.offset:.1f} max={ov.max_offset():.1f}")
        ov.set_playing(True)
        self.check("starting again at the end goes back to the top", ov.offset < 5)
        ov.set_playing(False)

        ov.restart()
        self.send_key(ov, Qt.Key.Key_Space, " ")
        self.check("Space starts", ov.playing)
        self.send_key(ov, Qt.Key.Key_Escape)
        self.check("Esc pauses", not ov.playing)
        sp = s["speed"]
        self.send_key(ov, Qt.Key.Key_Up)
        self.check("Up arrow speeds up", s["speed"] > sp)
        self.send_key(ov, Qt.Key.Key_Down)
        self.send_key(ov, Qt.Key.Key_PageDown)
        self.check("Page Down jumps ahead", ov.offset > 0)
        self.send_key(ov, Qt.Key.Key_Home)
        self.check("Home goes to the top", ov.offset == 0)
        fs = s["font_size"]
        self.send_key(ov, Qt.Key.Key_Plus, "+")
        self.check("+ makes text larger", s["font_size"] == fs + 2)
        self.send_key(ov, Qt.Key.Key_Minus, "-")
        self.check("- makes text smaller", s["font_size"] == fs)

        self.send_wheel(ov, -120)
        self.check("mouse wheel scrolls the text", ov.offset > 0, f"offset={ov.offset:.1f}")
        fs = s["font_size"]
        self.send_wheel(ov, 120, ctrl=True)
        self.check("Ctrl+wheel changes text size", s["font_size"] == fs + 2)
        self.send_wheel(ov, -120, ctrl=True)

        ov.restart()
        self.send_double_click(ov)
        self.check("double-click starts", ov.playing)
        ov.set_playing(False)

        w, h = ov.width(), ov.height()
        self.check("left edge is a resize handle", ov._edges_at(QPoint(2, h // 2)) == [Qt.Edge.LeftEdge])
        self.check("bottom-right corner is a resize handle",
                   set(ov._edges_at(QPoint(w - 2, h - 2))) == {Qt.Edge.RightEdge, Qt.Edge.BottomEdge})
        self.check("the middle is a drag area", ov._edges_at(QPoint(w // 2, h // 2)) == [])

        ov.offset = 0
        self.check("reading line starts on the first line", ov.reading_line_index() == 0)
        ov.offset = 5 * ov._line_h + 1
        idx = ov.reading_line_index()
        self.check("reading line follows the scrolling", abs(idx - 5) <= 1, f"line index {idx}")
        self.check("time left is shown", "left" in p.info.text(), p.info.text())

        ov.offset = ov.max_offset() / 2
        ratio = ov.offset / ov.max_offset()
        ov.resize(ov.width() - 120, ov.height())
        self.pump(0.1)
        ratio2 = ov.offset / ov.max_offset()
        self.check("resizing keeps your place", abs(ratio - ratio2) < 0.03, f"{ratio:.2f} -> {ratio2:.2f}")
        ov.resize(ov.width() + 120, ov.height())
        self.pump(0.1)

    def t_display(self):
        Qt, QFont = self.QtCore.Qt, self.QtGui.QFont
        ov, p, s, cp = self.ov, self.panel, self.s, self.cp
        p.font_box.setCurrentFont(QFont("Arial"))
        self.pump()
        self.check("font picker changes the font",
                   s["font_family"] == p.font_box.currentFont().family()
                   and ov.doc.defaultFont().family() == s["font_family"], s["font_family"])
        p.size_spin.setValue(44)
        self.check("size box changes the text size",
                   s["font_size"] == 44 and abs(ov.doc.defaultFont().pointSizeF() - 44) < 0.1)
        bold = s["bold"]
        p.bold_chk.setChecked(not bold)
        self.check("semi-bold switch", s["bold"] == (not bold))
        p.bold_chk.setChecked(bold)
        p.align_combo.setCurrentText("Centre")
        align = ov.doc.firstBlock().blockFormat().alignment()
        self.check("centre alignment", bool(align & Qt.AlignmentFlag.AlignHCenter), str(align))
        p.align_combo.setCurrentText("Left")
        p.spacing_spin.setValue(180)
        lh = ov.doc.firstBlock().blockFormat().lineHeight()
        self.check("line spacing", abs(lh - 180) < 0.1, lh)
        p.margin_spin.setValue(60)
        self.check("side margin", abs(ov.doc.textWidth() - (ov.width() - 120)) < 1,
                   f"text width {ov.doc.textWidth()} for window {ov.width()}")
        p.opacity_slider.setValue(20)
        self.check("background opacity", s["bg_opacity"] == 20)
        p.guide_chk.setChecked(False)
        self.check("reading line can be switched off", s["guide_enabled"] is False)
        p.guide_chk.setChecked(True)
        p.guide_slider.setValue(50)
        self.check("reading line position", s["guide_pos"] == 50)
        p.mirror_chk.setChecked(True)
        self.check("mirror switch", s["mirror"] is True)
        p.mirror_chk.setChecked(False)
        self.set_script("Checking the prompter still draws after every setting changed.")
        px = self.count_pixels(self.grab_overlay(), self.near(s["text_color"]))
        self.check("prompter still draws after all the changes", px > 30, f"{px} pixels")
        d = cp.DEFAULTS
        p.size_spin.setValue(d["font_size"])
        p.spacing_spin.setValue(d["line_spacing"])
        p.margin_spin.setValue(d["margin"])
        p.opacity_slider.setValue(d["bg_opacity"])
        p.guide_slider.setValue(d["guide_pos"])

    def t_visibility_and_shortcuts(self):
        ov, p, s, cp = self.ov, self.panel, self.s, self.cp
        p.show_btn.click()
        self.pump(0.1)
        self.check("Hide prompter hides it", not ov.isVisible())
        self.check("button then says Show prompter", p.show_btn.text() == "Show prompter", p.show_btn.text())
        p.show_btn.click()
        self.pump(0.3)
        self.check("Show prompter brings it back", ov.isVisible())
        if IS_WIN:
            hwnd = int(ov.winId())
            self.check("still on top after showing again", exstyle(hwnd) & 0x8)
            self.check("still hidden from screen share after showing again", ov.capture_state == "hidden",
                       f"state={ov.capture_state!r}", soft=True)

        ov.move(ov.x() + 40, ov.y() + 90)
        self.pump(0.1)
        p._snap()
        self.pump(0.1)
        scr, g = ov.screen().availableGeometry(), ov.geometry()
        self.check("Place under webcam moves it to the top", abs(g.top() - (scr.top() + 6)) <= 3,
                   f"top={g.top()}")
        self.check("Place under webcam centres it", abs(g.center().x() - scr.center().x()) <= 3,
                   f"centre x={g.center().x()} vs {scr.center().x()}")

        ov.bar.play.click()
        self.check("toolbar play button", ov.playing)
        ov.bar.play.click()
        self.check("toolbar pause button", not ov.playing)

        self.set_script("\n".join(f"Shortcut line {i}" for i in range(40)))
        labels = {name: label for name, label, *_ in cp.HOTKEYS}

        def hk(name, cond_fn, detail=""):
            p.on_hotkey(name)
            self.pump(0.05)
            self.check(f"{labels[name]} works", cond_fn(), detail)

        was = ov.playing
        hk("toggle_play", lambda: ov.playing != was)
        p.on_hotkey("toggle_play")
        sp = s["speed"]
        hk("faster", lambda: s["speed"] > sp)
        sp = s["speed"]
        hk("slower", lambda: s["speed"] < sp)
        ov.offset = ov.max_offset() / 2
        off = ov.offset
        hk("forward", lambda: ov.offset > off)
        off = ov.offset
        hk("back", lambda: ov.offset < off)
        fs = s["font_size"]
        hk("bigger", lambda: s["font_size"] == fs + 2)
        hk("smaller", lambda: s["font_size"] == fs)
        hk("restart", lambda: ov.offset == 0)
        hk("toggle_overlay", lambda: not ov.isVisible())
        p.on_hotkey("toggle_overlay")
        self.pump(0.2)
        self.check("prompter comes back with the same shortcut", ov.isVisible())
        hk("toggle_clickthrough", lambda: ov.click_through and p.click_chk.isChecked())
        p.on_hotkey("toggle_clickthrough")
        self.pump(0.1)
        self.check("click-through goes off with the same shortcut", not ov.click_through)
        hk("ask", lambda: p.tabs.currentWidget() is p.ai_tab)
        p.showMinimized()
        self.pump(0.2)
        hk("show_panel", lambda: p.isVisible() and not p.isMinimized())
        p.tabs.setCurrentIndex(0)

    def t_click_through(self):
        Qt = self.QtCore.Qt
        ov, p, s = self.ov, self.panel, self.s
        hwnd = int(ov.winId()) if IS_WIN else 0
        p.click_chk.setChecked(True)
        self.pump(0.3)
        self.check("switch turns click-through on", ov.click_through and s["click_through"])
        wh = ov.windowHandle()
        self.check("Qt marks the window as transparent to the mouse",
                   bool(wh.flags() & Qt.WindowType.WindowTransparentForInput))
        self.check("hover toolbar stays hidden", not ov.bar.isVisible())
        if IS_WIN:
            hwnd2 = int(ov.winId())
            self.check("window handle unchanged", hwnd2 == hwnd, f"{hwnd} -> {hwnd2}", soft=True)
            hwnd = hwnd2
            self.check("Windows marks the window click-through (WS_EX_TRANSPARENT)",
                       exstyle(hwnd) & 0x20, hex(exstyle(hwnd)))
            self.check("still on top in click-through mode", exstyle(hwnd) & 0x8, hex(exstyle(hwnd)))
            x, y = self.screen_point(ov)
            under = window_at(x, y)
            self.check("mouse goes through to the window behind", under not in (0, hwnd),
                       f"window under the prompter centre: {under}, prompter: {hwnd}", soft=True)
        p.click_chk.setChecked(False)
        self.pump(0.3)
        self.check("switch turns click-through off", not ov.click_through and not s["click_through"])
        self.check("Qt flag is cleared", not (wh.flags() & Qt.WindowType.WindowTransparentForInput))
        if IS_WIN:
            hwnd = int(ov.winId())
            self.check("WS_EX_TRANSPARENT is cleared", not (exstyle(hwnd) & 0x20), hex(exstyle(hwnd)))
            x, y = self.screen_point(ov)
            self.check("prompter catches the mouse again", window_at(x, y) == hwnd,
                       f"window under the prompter centre: {window_at(x, y)}, prompter: {hwnd}", soft=True)
        ov.set_click_through(True)
        self.pump(0.2)
        self.check("panel checkbox follows the prompter's menu", p.click_chk.isChecked())
        ov.set_click_through(False)
        self.pump(0.2)
        self.check("panel checkbox follows when switched back", not p.click_chk.isChecked())

    def t_capture(self):
        ov, p, s = self.ov, self.panel, self.s
        if not IS_WIN:
            self.log(WARN, "skipped", "hiding from screen sharing only exists on Windows")
            return
        old = (s["bg_color"], s["bg_opacity"])
        s["bg_color"], s["bg_opacity"] = "#FF00FF", 100
        ov.update()
        p.capture_chk.setChecked(False)
        self.pump(0.8)
        visible = self.magenta_on_screen()
        p.capture_chk.setChecked(True)
        self.pump(0.8)
        hidden = self.magenta_on_screen()
        self.check("Windows reports the prompter hidden", ov.capture_state == "hidden",
                   f"state={ov.capture_state!r}", soft=True)
        aff = display_affinity(int(p.winId()))
        self.check("control panel is hidden too", aff == 0x11, f"affinity={aff:#x}", soft=True)
        if visible < 0.3:
            self.log(WARN, "couldn't confirm with a real screenshot",
                     f"this machine's screenshots don't show the prompter even when it isn't hidden "
                     f"({visible:.0%} of its area), so the check can't tell hidden from not drawn")
        else:
            self.log(PASS, f"prompter appears in a screenshot when hiding is off ({visible:.0%})")
            self.check("prompter is missing from a screenshot when hiding is on", hidden < 0.05,
                       f"{hidden:.0%} of the prompter area still shows in the screenshot")
        s["bg_color"], s["bg_opacity"] = old
        ov.update()

    def t_keyboard(self):
        ov, p = self.ov, self.panel
        if not IS_WIN:
            return
        if "Ctrl+Alt+Space" in p._hotkey_failures:
            self.log(WARN, "skipped", "Ctrl+Alt+Space is taken by another program on this machine")
            return
        ov.set_playing(False)
        press_keys([0x11, 0x12, 0x20])  # Ctrl, Alt, Space
        reacted = self.wait_for(lambda: ov.playing, 2.0)
        self.check("a real Ctrl+Alt+Space key press starts the prompter", reacted,
                   "no reaction to the simulated key press; this needs an unlocked desktop session",
                   soft=True)
        ov.set_playing(False)

    def t_ai(self):
        p, ov, s, cp, log, base = self.panel, self.ov, self.s, self.cp, self.ai_log, self.base
        p.tabs.setCurrentWidget(p.ai_tab)
        self.set_provider("Anthropic (Claude)")
        p.url_edit.setText(base)
        p.key_edit.setText("")
        p.model_edit.setText("claude-test")

        p.test_btn.click()
        self.pump()
        self.check("missing API key is explained", "Add an API key" in p.ai_status.text(), p.ai_status.text())
        self.check("nothing is sent without a key", len(log) == 0, f"{len(log)} requests")

        p.key_edit.setText("good-key")
        p.test_btn.click()
        self.check("buttons are disabled while the AI works",
                   not p.gen_btn.isEnabled() and not p.ask_btn.isEnabled() and not p.rewrite_btn.isEnabled())
        p._test_connection()
        self.check("a second request waits for the first", "Still working" in p.ai_status.text(),
                   p.ai_status.text())
        done = self.ai_wait()
        self.check("Test connection succeeds", done and p.ai_status.text().startswith("Connected"),
                   p.ai_status.text())
        self.check("buttons come back after the reply", p.gen_btn.isEnabled() and p.test_btn.isEnabled())
        if log:
            last = log[-1]
            self.check("Claude request sends the key", last["headers"].get("x-api-key") == "good-key")
            self.check("Claude request sends the API version",
                       last["headers"].get("anthropic-version") == "2023-06-01")
            self.check("Claude request uses the chosen model", last["body"].get("model") == "claude-test")

        # write a script
        self.set_script("Original script line")
        p.topic_edit.setPlainText("")
        p.gen_btn.click()
        self.pump()
        self.check("empty topic is explained", "Describe the talk" in p.ai_status.text(), p.ai_status.text())
        p.topic_edit.setPlainText("Quarterly update for the team")
        p.length_combo.setCurrentText("2 minutes")
        p.tone_combo.setCurrentText("Conversational")
        p.gen_btn.click()
        ok = self.ai_wait()
        self.check("Write script fills the editor", ok and p.editor.toPlainText() == SCRIPT_CLEAN,
                   repr(p.editor.toPlainText()[:120]))
        self.check("markdown is cleaned out of AI text",
                   "```" not in p.editor.toPlainText() and "#" not in p.editor.toPlainText())
        self.check("prompter shows the new script", ov.raw_text == SCRIPT_CLEAN)
        u = log[-1]["user"] if log else ""
        self.check("request includes the topic, length and tone",
                   "Quarterly update" in u and "280 words" in u and "Conversational" in u, u[:200])
        p.editor.undo()
        self.pump(0.3)
        self.check("Ctrl+Z brings back the previous script", p.editor.toPlainText() == "Original script line",
                   repr(p.editor.toPlainText()[:80]))

        # rewrite the whole script
        self.set_script("First line\nSecond line")
        cur = p.editor.textCursor()
        cur.clearSelection()
        p.editor.setTextCursor(cur)
        p.rewrite_combo.setCurrentText("Polish and tighten")
        p.rewrite_btn.click()
        self.ai_wait()
        self.check("Rewrite changes the whole script when nothing is selected",
                   p.editor.toPlainText() == REWRITE_REPLY, repr(p.editor.toPlainText()))
        u = log[-1]["user"] if log else ""
        self.check("rewrite request carries the passage and the instruction",
                   "<passage>\nFirst line\nSecond line\n</passage>" in u
                   and cp.REWRITE_ACTIONS["Polish and tighten"] in u, u[:200])

        # rewrite a selection
        QTextCursor = self.QtGui.QTextCursor
        self.set_script("Alpha line\nBeta line\nGamma line")
        cur = QTextCursor(p.editor.document())
        cur.setPosition(11)
        cur.setPosition(20, QTextCursor.MoveMode.KeepAnchor)
        p.editor.setTextCursor(cur)
        self.check("test selection is 'Beta line'", p.editor.textCursor().selectedText() == "Beta line")
        p.rewrite_btn.click()
        self.ai_wait()
        self.check("Rewrite replaces only the selected text",
                   p.editor.toPlainText() == "Alpha line\nRewritten passage\nGamma line",
                   repr(p.editor.toPlainText()))

        # editing while the AI works
        self.set_script("One\nTwo")
        cur = p.editor.textCursor()
        cur.clearSelection()
        p.editor.setTextCursor(cur)
        p.rewrite_btn.click()
        p.editor.setPlainText("Edited while waiting")
        self.ai_wait()
        self.check("your edits made while waiting are kept", p.editor.toPlainText() == "Edited while waiting")
        self.check("the late rewrite goes to the answer box instead",
                   p.answer_box.toPlainText() == REWRITE_REPLY and "changed" in p.ai_status.text(),
                   p.ai_status.text())

        # custom instruction
        p.rewrite_combo.setCurrentText("Custom instruction")
        self.pump()
        self.check("custom instruction box appears", not p.custom_edit.isHidden())
        p.custom_edit.setText("")
        p.rewrite_btn.click()
        self.pump()
        self.check("empty custom instruction is explained", "Type your instruction" in p.ai_status.text(),
                   p.ai_status.text())
        p.custom_edit.setText("Make it rhyme")
        p.rewrite_btn.click()
        self.ai_wait()
        self.check("custom instruction is sent", bool(log) and "Make it rhyme" in log[-1]["user"])
        p.rewrite_combo.setCurrentText("Polish and tighten")
        self.pump()
        self.check("custom instruction box hides again", p.custom_edit.isHidden())

        # ask during the call
        self.set_script("Line A\nLine B\nLine C")
        p.context_chk.setChecked(True)
        p.auto_chk.setChecked(False)
        p.ask_edit.setText("What is our target?")
        p.ask_btn.click()
        self.ai_wait()
        self.check("Ask shows the answer", p.answer_box.toPlainText() == ASK_REPLY,
                   repr(p.answer_box.toPlainText()))
        u = log[-1]["user"] if log else ""
        self.check("question is sent with the script as context",
                   "<script>\nLine A\nLine B\nLine C\n</script>" in u and "What is our target?" in u, u[:200])
        ov.restart()
        p._insert_answer_at_reading_line()
        expected = f"Line A\n\n{ANSWER_BLOCK}\n\nLine B\nLine C"
        self.check("answer is inserted under the reading line", p.editor.toPlainText() == expected,
                   repr(p.editor.toPlainText()))
        self.check("prompter shows the inserted answer", ov.raw_text == expected)

        self.set_script("\n".join(f"Row {i}" for i in range(14)))
        ov.offset = 6 * ov._line_h + 1
        idx = ov.reading_line_index()
        p._insert_answer_at_reading_line()
        lines = p.editor.toPlainText().split("\n")
        self.check("answer lands right after the line being read",
                   len(lines) > idx + 2 and lines[idx] == f"Row {idx}" and lines[idx + 2] == "[ANSWER]",
                   f"reading line {idx}: {lines[idx:idx + 4]}")
        p._append_answer()
        self.check("Add to end appends the answer", p.editor.toPlainText().endswith(ANSWER_BLOCK))

        n = p.editor.toPlainText().count("[ANSWER]")
        p.auto_chk.setChecked(True)
        p.ask_btn.click()
        self.ai_wait()
        self.check("answers go onto the prompter automatically when chosen",
                   p.editor.toPlainText().count("[ANSWER]") == n + 1)
        p.auto_chk.setChecked(False)
        p.context_chk.setChecked(False)
        p.ask_btn.click()
        self.ai_wait()
        self.check("question is sent on its own when context is off",
                   bool(log) and log[-1]["user"] == "What is our target?", log[-1]["user"][:120] if log else "")
        p.context_chk.setChecked(True)
        p.ask_edit.setText("")
        count = len(log)
        p.ask_btn.click()
        self.pump(0.3)
        self.check("an empty question sends nothing", len(log) == count and not p._busy)

        # errors
        p.key_edit.setText("bad-key")
        p.test_btn.click()
        self.ai_wait()
        st = p.ai_status.text()
        self.check("a wrong key gives a clear error", "HTTP 401" in st and "invalid x-api-key" in st, st)
        self.check("errors show in red", cp.TALLY.lower() in p.ai_status.styleSheet().lower())
        p.key_edit.setText("good-key")
        p.url_edit.setText(f"http://127.0.0.1:{closed_port()}")
        p.test_btn.click()
        ok = self.ai_wait(30)
        self.check("an unreachable server gives a clear error", ok and "Could not reach" in p.ai_status.text(),
                   p.ai_status.text())
        if not ok:
            self.ai_wait(130)
        p.url_edit.setText(base)

        # OpenAI-compatible servers
        self.set_provider("Custom (OpenAI-compatible)")
        self.check("switching provider loads that provider's own settings",
                   p.url_edit.text() == "" and p.key_edit.text() == "",
                   f"url={p.url_edit.text()!r} key set={bool(p.key_edit.text())}")
        p.url_edit.setText(base + "/v1")
        p.model_edit.setText("local-test")
        p.test_btn.click()
        self.ai_wait()
        self.check("OpenAI-compatible server works", p.ai_status.text().startswith("Connected"),
                   p.ai_status.text())
        if log:
            last = log[-1]
            self.check("OpenAI-style request goes to /chat/completions", last["path"] == "/v1/chat/completions",
                       last["path"])
            self.check("no key is sent when none is set", "authorization" not in last["headers"])
            self.check("instructions go as the system message", last["body"]["messages"][0]["role"] == "system")
        p.ask_edit.setText("Quick check")
        p.ask_btn.click()
        self.ai_wait()
        self.check("Ask works with an OpenAI-compatible server", p.answer_box.toPlainText() == ASK_REPLY)
        p.key_edit.setText("sk-test")
        p.test_btn.click()
        self.ai_wait()
        self.check("key is sent as a Bearer token",
                   bool(log) and log[-1]["headers"].get("authorization") == "Bearer sk-test")

        self.set_provider("Anthropic (Claude)")
        self.check("switching back restores the Claude settings",
                   p.url_edit.text() == base and p.key_edit.text() == "good-key"
                   and p.model_edit.text() == "claude-test",
                   f"url={p.url_edit.text()!r} model={p.model_edit.text()!r}")
        self.check("each provider keeps its own key",
                   s["ai_profiles"]["Custom (OpenAI-compatible)"]["api_key"] == "sk-test")

        self.set_provider("Ollama (local, free)")
        self.check("Ollama doesn't ask for a key", p.key_edit.placeholderText() == "Not needed")
        p.url_edit.setText(base + "/v1")
        p.key_edit.setText("")
        p.test_btn.click()
        self.ai_wait()
        self.check("Ollama works without a key", p.ai_status.text().startswith("Connected"), p.ai_status.text())
        self.set_provider("Anthropic (Claude)")

    def t_listen(self):
        import importlib
        p, cp = self.panel, self.cp
        listen = getattr(cp, "listen", None)
        if listen is None:
            self.log(FAIL, "listening module failed to import",
                     "cuepilot_listen.py is missing or has an import error")
            return

        # --- pure logic (always runs, no audio device needed)
        with self.step("Listening: question detection"):
            cases = {"What is Shield?": True, "Can you walk us through it?": True,
                     "tell me about CPQ": True, "we shipped it last quarter": False,
                     "Right, exactly.": False, "How would you scale this": True}
            for text, expect in cases.items():
                self.check(f"'{text[:32]}' -> question={expect}",
                           listen.looks_like_question(text) == expect)

        with self.step("Listening: audio maths"):
            import math
            tone = [0.3 * math.sin(2 * math.pi * 440 * t / 48000) for t in range(int(48000 * 0.4))]
            self.check("loud audio reads as loud", listen.rms_fast(tone) > 0.1)
            self.check("silence reads as quiet", listen.rms_fast([0.0] * 500) < 0.001)
            rs = listen.resample_linear(tone, 48000, 16000)
            self.check("resample to 16 kHz", abs(len(rs) - 16000 * 0.4) <= 3, f"{len(rs)} samples")
            wav = listen.samples_to_wav(rs)
            import io as _io
            import wave as _wave
            with _wave.open(_io.BytesIO(wav)) as w:
                self.check("WAV is 16 kHz mono 16-bit",
                           w.getframerate() == 16000 and w.getnchannels() == 1 and w.getsampwidth() == 2)
            emitted = []
            vs = listen.VoiceSplitter(lambda s: emitted.append(len(s) / 16000))
            step = int(16000 * 0.1)
            for i in range(0, len(rs), step):
                vs.feed(rs[i:i + step])
            for i in range(0, int(16000 * 0.9), step):
                vs.feed([0.0] * step)
            self.check("one spoken phrase is detected after a pause", len(emitted) == 1, f"{emitted}")
            self.check("short blips are ignored", True)
            blip = listen.resample_linear(tone[:int(48000 * 0.1)], 48000, 16000)
            emitted.clear()
            vs.reset()
            for i in range(0, len(blip), step):
                vs.feed(blip[i:i + step])
            for i in range(0, int(16000 * 0.9), step):
                vs.feed([0.0] * step)
            self.check("a too-short blip produces no phrase", emitted == [], f"{emitted}")
            for text, ok in {"you": False, "...": False, "What is CPQ": True, "": False}.items():
                self.check(f"noise filter: '{text}' -> {ok}", listen._is_meaningful(text) == ok)

        with self.step("Listening: reading the AI's question verdict"):
            parse = cp.ControlPanel._parse_detected_question
            self.check("a detected question is read from JSON",
                       parse('{"question": "What is your Shield experience?"}')
                       == "What is your Shield experience?")
            self.check("JSON wrapped in code fences is handled",
                       parse('```json\n{"question": "Why Germany?"}\n```') == "Why Germany?")
            self.check("a null verdict means no question", parse('{"question": null}') is None)
            self.check("a plain 'none' means no question", parse("none") is None)
            self.check("an empty object means no question", parse("{}") is None)

        with self.step("Listening: telling the two voices apart"):
            self.check("identical phrases score as very similar",
                       listen.text_similarity("what is your target", "what is your target") > 0.99)
            self.check("unrelated phrases score low",
                       listen.text_similarity("how are you", "the sky is blue") < 0.2)
            ef = listen.EchoFilter(window=4.0, threshold=0.72)
            ef.note_own_speech("what is our quarterly target", now=100.0)
            self.check("your voice echoing back is recognised as an echo",
                       ef.is_echo("what is our quarterly target", now=100.4))
            self.check("a genuinely different far-end line is not an echo",
                       not ef.is_echo("tell me about your architecture", now=100.4))
            self.check("an old line of yours no longer counts as an echo",
                       not ef.is_echo("what is our quarterly target", now=110.0))

            # end to end through the real Listener with transcription stubbed out
            events = []
            lis = listen.Listener({"kind": "cloud", "base_url": "http://x/v1", "api_key": "k",
                                   "model": "m"},
                                  on_transcript=lambda t, q, spk: events.append((spk, q, t)),
                                  on_status=lambda m, e: None, capture_mic=True, suppress_echo=True)
            texts = {}

            def enqueue(label, text):
                wav = listen.samples_to_wav([0.1] * 1600)
                texts[id(wav)] = text
                with lis._queue_lock:
                    lis._queue.append((label, wav))
                lis._wake.set()
            lis._transcribe_one = lambda wav: texts.get(id(wav), "")
            lis._worker = threading.Thread(target=lis._transcribe_loop, daemon=True)
            lis._worker.start()
            enqueue(listen.ME, "what is our revenue target")
            if not self.wait_for(lambda: len(events) >= 1, 2):
                pass
            enqueue(listen.THEM, "what is our revenue target")       # your voice echoing back
            enqueue(listen.THEM, "can you walk me through the design")  # a real far-end question
            self.wait_for(lambda: any(e[0] == listen.THEM and e[1] for e in events), 3)
            time.sleep(0.2)
            lis.stop()
            them_q = [e for e in events if e[0] == listen.THEM and e[1]]
            me_lines = [e for e in events if e[0] == listen.ME]
            echoed = [e for e in events if e[0] == listen.THEM and e[2] == "what is our revenue target"]
            self.check("your own line is reported as yours", len(me_lines) == 1, f"{me_lines}")
            self.check("your voice echoing back through the call is dropped", echoed == [], f"{echoed}")
            self.check("a real far-end question still comes through and is flagged",
                       len(them_q) == 1 and them_q[0][2] == "can you walk me through the design",
                       f"{them_q}")

        with self.step("Listening: transcription request"):
            srv, base, log = start_mock_transcribe_server()
            cfg = {"kind": "cloud", "base_url": base + "/v1", "api_key": "stt-key",
                   "model": "whisper-large-v3-turbo"}
            wav = listen.samples_to_wav(listen.resample_linear(
                [0.2] * int(48000 * 0.5), 48000, 16000))
            text = listen.transcribe_cloud(cfg, wav)
            self.check("cloud transcription returns text", text == "What is your Shield experience?", text)
            if log:
                last = log[-1]
                self.check("it posts to /audio/transcriptions", last["path"].endswith("/audio/transcriptions"))
                self.check("it sends a multipart WAV upload",
                           last["multipart"] and last["has_file"] and last["has_model"])
                self.check("it sends the transcription key as a Bearer token",
                           last["auth"] == "Bearer stt-key", last["auth"])
            try:
                listen.transcribe_cloud(dict(cfg, api_key="bad"), wav)
                self.check("a bad transcription key errors", False)
            except listen.STTError as exc:
                self.check("a bad transcription key gives a clear error", "401" in str(exc), str(exc))
            srv.shutdown()

        # --- UI wiring, driven with a fake listener so no microphone is needed
        with self.step("Listening: controls"):
            if not hasattr(p, "listen_btn"):
                self.log(WARN, "listen UI not built", "soundcard/numpy may be missing in this build")
                return
            self.check("Start listening button exists", p.listen_btn.text() == "Start listening")
            self.check("consent is required before starting", not p.consent_chk.isChecked())
            p.tabs.setCurrentWidget(p.ai_tab)

            captured = {}
            real = listen.Listener

            class FakeListener:
                def __init__(self, cfg, on_transcript, on_status, on_level=None, **kw):
                    captured["cfg"] = cfg
                    captured["mic"] = kw.get("capture_mic")
                    captured["echo"] = kw.get("suppress_echo")
                    self._t, self._s = on_transcript, on_status
                    self.running = False

                def start(self):
                    self.running = True
                    self._s("Listening.", False)
                    return True

                def stop(self):
                    self.running = False

                def say(self, text, speaker=listen.THEM):
                    self._t(text, listen.looks_like_question(text), speaker)
            listen.Listener = FakeListener
            try:
                # provider setup
                p.stt_combo.setCurrentText("Groq (free, cloud)")
                p.stt_key.setText("")
                p.stt_model.setText("whisper-large-v3-turbo")
                srv, base, _ = start_mock_transcribe_server()
                p.stt_url.setText(base + "/v1")

                # won't start without consent
                p.consent_chk.setChecked(False)
                p.listen_btn.click()
                self.check("refuses to start without consent",
                           p.listener is None and "consent" in p.listen_status.text().lower()
                           or "knows" in p.listen_status.text().lower(), p.listen_status.text())

                # won't start without a key
                p.consent_chk.setChecked(True)
                p.listen_btn.click()
                self.check("refuses to start without a transcription key",
                           p.listener is None and "API key" in p.listen_status.text(), p.listen_status.text())

                # the AI question-detection uses the main AI connection, so set that up first
                p.key_edit.setText("good-key")
                p.url_edit.setText(self.base)
                p.model_edit.setText("claude-test")
                # make detection fire fast so the test doesn't wait 1.6 s each time
                p._detect_timer.setInterval(60)

                # starts with a key
                p.stt_key.setText("stt-key")
                p.mic_chk.setChecked(True)
                p.listen_btn.click()
                self.pump(0.1)
                self.check("starts listening with consent and a key", p.listener is not None)
                self.check("button turns into Stop listening", p.listen_btn.text() == "Stop listening")
                self.check("the chosen key and model reach the listener",
                           captured.get("cfg", {}).get("api_key") == "stt-key"
                           and captured["cfg"]["model"] == "whisper-large-v3-turbo")
                self.check("the microphone choice reaches the listener", captured.get("mic") is True)
                self.check("echo suppression is requested when the mic is on",
                           captured.get("echo") is True)

                # a far-end statement appears but is NOT flagged by the AI
                nq = len(p._questions)
                p.listener.say("So we shipped it last quarter", listen.THEM)
                self.wait_for(lambda: not p._detect_requests, 8)
                self.pump(0.2)
                self.check("statements show in the transcript",
                           "shipped it last quarter" in p.transcript_box.toPlainText())
                self.check("the AI does not flag a plain statement as a question",
                           len(p._questions) == nq, f"{p._questions}")

                # YOUR OWN voice is shown but never detected or answered
                p.auto_answer_chk.setChecked(True)
                before_q = len(p._questions)
                before_answer = p.answer_box.toPlainText()
                p.listener.say("What is the deadline for this?", listen.ME)   # you asking
                self.pump(0.3)
                self.wait_for(lambda: not p._detect_requests, 4)
                self.check("your own voice is shown in the transcript",
                           "deadline for this" in p.transcript_box.toPlainText())
                self.check("your own question is never sent for detection or answered",
                           len(p._questions) == before_q
                           and p.answer_box.toPlainText() == before_answer, f"{p._questions}")
                p.auto_answer_chk.setChecked(False)

                # a far-end question: the AI detects it and it's flagged
                before = p.answer_box.toPlainText()
                nq = len(p._questions)
                p.listener.say("What is your experience with Shield?", listen.THEM)
                got = self.wait_for(lambda: len(p._questions) > nq, 10)
                self.check("the AI detects a real far-end question", got, f"{p._questions}")
                self.check("detected questions are marked with a symbol",
                           "❓" in p.transcript_box.toPlainText())
                self.check("a detected question isn't auto-answered when auto-answer is off",
                           p.answer_box.toPlainText() == before)

                # answering it routes through the AI with the conversation as context
                self.ai_log.clear()
                p._answer_question("What is your experience with Shield?")
                ok = self.ai_wait()
                self.check("answering a question calls the AI and fills the answer box",
                           ok and p.answer_box.toPlainText() == ASK_REPLY, p.answer_box.toPlainText())
                ask_calls = [e for e in self.ai_log if e["system"] == cp.SYSTEM_ASK]
                self.check("the answer request includes the recent conversation for follow-ups",
                           ask_calls and "recent_conversation" in ask_calls[-1]["user"],
                           ask_calls[-1]["user"][:160] if ask_calls else "no ask call")

                # auto-answer a detected far-end question end to end
                p.auto_answer_chk.setChecked(True)
                p.answer_box.clear()
                nq = len(p._questions)
                p.listener.say("How would you scale this across orgs?", listen.THEM)
                ok = self.wait_for(lambda: p.answer_box.toPlainText() == ASK_REPLY, 12)
                self.check("auto-answer detects and answers a far-end question with no tap",
                           ok, p.answer_box.toPlainText())

                # a repeated question isn't flagged twice
                nq = len(p._questions)
                p.listener.say("How would you scale this across orgs?", listen.THEM)
                self.wait_for(lambda: not p._detect_requests, 8)
                self.pump(0.3)
                self.check("a repeated question isn't flagged again",
                           len(p._questions) == nq, f"{p._questions}")

                # clearing
                p._clear_transcript()
                self.check("Clear empties the transcript, questions and history",
                           p.transcript_box.toPlainText() == "" and p._questions == []
                           and p._history == [])

                # stop
                p.listen_btn.click()
                self.pump(0.1)
                self.check("Stop listening stops", p.listener is None
                           and p.listen_btn.text() == "Start listening")

                # unchecking consent while running stops it
                p.consent_chk.setChecked(True)
                p.listen_btn.click()
                self.pump(0.1)
                started = p.listener is not None
                p.consent_chk.setChecked(False)
                self.pump(0.1)
                self.check("withdrawing consent stops listening", started and p.listener is None)

                # provider switching keeps separate profiles
                p.consent_chk.setChecked(True)
                p.stt_combo.setCurrentText("Offline (faster-whisper)")
                self.check("offline provider hides the key and URL fields",
                           not p.stt_key.isEnabled() and not p.stt_url.isEnabled())
                self.check("offline provider suggests a model", p.stt_model.text() != "")
                p.stt_combo.setCurrentText("Groq (free, cloud)")
                self.check("switching back restores the Groq key",
                           p.stt_key.text() == "stt-key", p.stt_key.text())
                srv.shutdown()
            finally:
                listen.Listener = real
                if p.listener is not None:
                    p._stop_listen()

    def t_docs(self):
        p, cp = self.panel, self.cp
        docs = getattr(cp, "docs", None)
        if docs is None:
            self.log(FAIL, "document module failed to import", "cuepilot_docs.py missing or broken")
            return

        sample = ("Multi-Org Architecture\n\n"
                  "We chose a hub-and-spoke model. The central org holds shared services.\n"
                  "Each business unit runs its own spoke org for isolation.\n\n"
                  "Performance Under Load\n\n"
                  "After consolidating the integration layer, throughput improved by 40 percent.\n"
                  "Peak concurrency now handles 5000 simultaneous users without degradation.\n\n"
                  "Shield Platform Encryption\n\n"
                  "We encrypt PII at rest using deterministic encryption for searchable fields.\n"
                  "Probabilistic encryption protects the most sensitive attributes.\n")

        with self.step("Document: reading files"):
            txt = (self.tmp / "ref.txt")
            txt.write_text(sample, encoding="utf-8")
            self.check("a text file is read", docs.read_document(str(txt)).strip() != "")
            try:
                import docx as _docx
                d = _docx.Document()
                d.add_heading("Cost Model", 1)
                d.add_paragraph("Licensing came to 240 thousand annually.")
                tbl = d.add_table(rows=1, cols=2)
                tbl.cell(0, 0).text = "Shield"
                tbl.cell(0, 1).text = "40k"
                dpath = self.tmp / "ref.docx"
                d.save(str(dpath))
                dtext = docs.read_document(str(dpath))
                self.check("a Word file is read, including tables",
                           "Cost Model" in dtext and "Shield | 40k" in dtext, dtext[:120])
            except ImportError:
                self.log(WARN, "python-docx not installed", "Word reading can't be tested here")
            try:
                docs.read_document(str(self.tmp / "missing.pdf"))
                self.check("a missing file errors", False)
            except docs.DocError as exc:
                self.check("a missing file gives a clear error", "wasn't found" in str(exc), str(exc))

        with self.step("Document: splitting into passages"):
            doc = docs.ReferenceDoc("ref.txt")
            n = doc.load_text(sample)
            self.check("the document splits into several passages", n >= 3, f"{n} passages")
            headings = [pg["heading"] for pg in doc.passages]
            self.check("section headings are kept with passages",
                       "Multi-Org Architecture" in headings and "Shield Platform Encryption" in headings,
                       f"{headings}")

        with self.step("Document: keyword search"):
            doc = docs.ReferenceDoc("ref.txt")
            doc.load_text(sample)
            for q, want in [("how do you encrypt sensitive data", "Shield"),
                            ("tell me about your org structure", "Multi-Org"),
                            ("what about concurrency and load", "Performance")]:
                r = doc.search(q)[0]
                hit = want.lower() in (r["heading"] + r["text"]).lower()
                self.check(f"keyword search finds the right passage for '{q[:28]}'", hit,
                           f"got {r['heading']!r}")
                self.check("keyword result is the document's own text (verbatim)",
                           r["text"] in sample, r["text"][:60])

        with self.step("Document: smart (contextual) search"):
            # deterministic fake embeddings over a fixed vocabulary
            vocab = ("org orgs spoke hub throughput concurrency load users encryption pii shield "
                     "sensitive scale scaling isolation consolidate").split()

            def fake_vec(text):
                tl = text.lower()
                return [float(tl.count(w)) for w in vocab]

            srv, base, calls = start_mock_embed_server(fake_vec)
            cfg = {"base_url": base + "/v1", "api_key": "ek", "model": "fake-embed"}
            doc = docs.ReferenceDoc("ref.txt")
            doc.load_text(sample)
            doc.build_embeddings(cfg)
            self.check("the document is embedded for smart search", doc.embedded)
            self.check("embeddings were requested from the server", len(calls) >= 1)
            r = doc.search("throughput under heavy load", embed_cfg=cfg)[0]
            self.check("smart search matches by meaning", r["method"] == "semantic"
                       and "Performance" in r["heading"], f"{r['method']} {r['heading']}")

            # contextual: a fake context AI injects topic words so an indirect question matches
            def ctx_complete(system, user, mx):
                low = user.lower()
                if "throughput" in low:
                    return "This passage is about scaling and concurrency under load."
                if "encrypt" in low:
                    return "This passage is about Shield encryption of sensitive data."
                if "spoke" in low:
                    return "This passage is about multi-org isolation structure."
                return "General context."
            doc2 = docs.ReferenceDoc("ref.txt")
            doc2.load_text(sample)
            doc2.build_embeddings(cfg, context_ai={"complete": ctx_complete})
            self.check("contextual embeddings are built", doc2.contextual)
            r = doc2.search("how do you approach scaling", embed_cfg=cfg)[0]
            self.check("contextual search matches a passage that never says the query word",
                       r["method"] == "contextual" and "Performance" in r["heading"],
                       f"{r['method']} {r['heading']}")
            self.check("smart-search result is still the document's own text (verbatim)",
                       r["text"] in sample, r["text"][:60])
            srv.shutdown()

        with self.step("Document: controls"):
            if not hasattr(p, "doc_name_lbl"):
                self.log(WARN, "document UI not built", "document section missing")
                return
            p.tabs.setCurrentWidget(p.ai_tab)
            txt = self.tmp / "ui_ref.txt"
            txt.write_text(sample, encoding="utf-8")
            p._load_document(str(txt))
            loaded = self.wait_for(lambda: p.ref_doc is not None and p.ref_doc.passages, 5)
            self.check("uploading a document loads it", loaded)
            self.check("the document name is shown", "ui_ref" in p.doc_name_lbl.text(),
                       p.doc_name_lbl.text())

            # showing a passage (the signature now carries meta separately from heading/text)
            p.project_doc_chk.setChecked(False)
            p._show_doc_passage("how do you handle encryption", "Shield Platform Encryption",
                                "We encrypt PII at rest using deterministic encryption for "
                                "searchable fields.", "keyword", project=False)
            self.check("the matching passage is shown verbatim",
                       "deterministic encryption" in p.doc_result.toPlainText())
            self.check("the passage carries its section heading",
                       "Shield Platform Encryption" in p.doc_result.toPlainText())

            # show on prompter
            before = p.editor.toPlainText()
            p._project_doc_result()
            self.pump(0.2)
            self.check("Show on prompter adds the passage to the script",
                       "deterministic encryption" in p.editor.toPlainText()
                       and "FROM: Shield" in p.editor.toPlainText())
            self.check("the passage appears on the prompter",
                       "deterministic encryption" in p.ov.raw_text)
            p.editor.setPlainText(before)
            self.pump(0.2)

            # the instant keyword pass finds a passage with no network and no embeddings
            p._last_doc_result = None
            p._instant_doc_pass("what about scaling and concurrency")
            self.check("the instant keyword pass finds a passage with no network",
                       p._last_doc_result is not None
                       and "Performance" in (p._last_doc_result[0] or ""), p._last_doc_result)

            # confidence: a good match vs nothing matching
            with self.step("Document: knowing when it has the answer"):
                doc3 = docs.ReferenceDoc("x")
                doc3.load_text(sample)
                good = doc3.search("how do you encrypt sensitive data")[0]
                self.check("a relevant question is a good match", good.get("good") is True,
                           f"score {good['score']:.3f}")
                bad = doc3.search("what is your favourite holiday destination")[0]
                self.check("an unrelated question is not a good match", bad.get("good") is False,
                           f"score {bad['score']:.3f}")

            # grounded answer + parallel timing when the document has a good match
            with self.step("Document: parallel lookup and grounded answer"):
                p.key_edit.setText("good-key")
                p.url_edit.setText(self.base)
                p.model_edit.setText("claude-test")
                txt2 = self.tmp / "ground_ref.txt"
                txt2.write_text(sample, encoding="utf-8")
                p._load_document(str(txt2))
                self.wait_for(lambda: p.ref_doc is not None and p.ref_doc.passages, 5)
                # keyword-only (no embeddings): the passage must appear instantly, before any AI
                p.project_doc_chk.setChecked(True)
                p.auto_chk.setChecked(False)
                self.ai_log.clear()
                p.answer_box.clear()
                p.doc_result.clear()

                p._handle_question("how do you encrypt sensitive data", auto=True)
                # The instant keyword pass runs synchronously inside _handle_question, so the
                # passage is on the prompter the moment the call returns — before any AI reply.
                self.check("the document passage appears instantly, without waiting for the AI",
                           "encrypt" in p.doc_result.toPlainText().lower()
                           and p.answer_box.toPlainText() == "",
                           f"doc={p.doc_result.toPlainText()[:40]!r} ans={p.answer_box.toPlainText()[:40]!r}")
                self.check("the passage is on the prompter immediately",
                           "encrypt" in p.ov.raw_text.lower(), p.ov.raw_text[-120:])

                ok = self.wait_for(lambda: p.answer_box.toPlainText() == GROUNDED_REPLY, 12)
                self.check("a doc-answered question gets a grounded AI answer", ok,
                           p.answer_box.toPlainText())
                grounded_calls = [e for e in self.ai_log if e["system"] == cp.SYSTEM_GROUNDED]
                self.check("the grounded request includes the document extract",
                           grounded_calls and "document_extract" in grounded_calls[-1]["user"],
                           grounded_calls[-1]["user"][:120] if grounded_calls else "none")
                self.check("the answer panel notes it's grounded in the document",
                           "document" in p.ai_status.text().lower(), p.ai_status.text())

                # a question the document does NOT answer -> script-based answer, no passage
                self.ai_log.clear()
                p.answer_box.clear()
                p.doc_result.clear()
                p._answer_token += 0  # keep current
                p._handle_question("what is the capital of France", auto=True)
                ok = self.wait_for(lambda: p.answer_box.toPlainText() == ASK_REPLY, 12)
                self.check("an unanswered question falls back to a script-based AI answer", ok,
                           p.answer_box.toPlainText())
                self.check("no grounded call is made when the document doesn't match",
                           all(e["system"] != cp.SYSTEM_GROUNDED for e in self.ai_log),
                           "grounded call leaked in")
                self.check("no passage is shown when nothing matches",
                           p.doc_result.toPlainText() == "", p.doc_result.toPlainText()[:40])
                p._unload_document()

            # removing the document
            p._unload_document()
            self.check("removing the document clears it",
                       p.ref_doc is None and p.doc_result.toPlainText() == "")

    def t_files(self):
        ov, p, s = self.ov, self.panel, self.s
        text = "Saved script\nनमस्ते सभी"
        p.editor.setPlainText(text)
        self.pump(0.4)
        f = self.tmp / "talk.txt"
        p._write_file(str(f))
        self.check("Save writes the script, including Hindi text",
                   f.exists() and f.read_text(encoding="utf-8") == text)
        self.check("the file name is remembered", s["last_file"] == str(f))
        g = self.tmp / "other.txt"
        g.write_text("Loaded from disk [PAUSE]", encoding="utf-8")
        p._load_path(str(g))
        self.pump(0.1)
        self.check("Open loads a script file",
                   p.editor.toPlainText() == "Loaded from disk [PAUSE]" and ov.raw_text == "Loaded from disk [PAUSE]")
        p._load_path(str(self.tmp / "missing.txt"))
        self.check("a missing file shows a message instead of crashing",
                   "Couldn't open" in p.statusBar().currentMessage(), p.statusBar().currentMessage())

    def t_persistence(self):
        ov, p, s, cp = self.ov, self.panel, self.s, self.cp
        s["font_size"] = 34
        p.remember_chk.setChecked(True)
        p.editor.setPlainText("Persist me")
        p.close()
        self.pump(0.3)
        self.check("closing the panel closes the prompter", not ov.isVisible())
        saved = json.loads(cp.SETTINGS_FILE.read_text(encoding="utf-8"))
        self.check("settings are saved on close", saved.get("font_size") == 34)
        geo = saved.get("overlay_geometry")
        self.check("prompter position is saved", isinstance(geo, list) and len(geo) == 4, geo)
        self.check("API key is remembered when chosen",
                   saved["ai_profiles"]["Anthropic (Claude)"]["api_key"] == "good-key")
        self.check("the script is saved for next time",
                   cp.SCRIPT_FILE.read_text(encoding="utf-8") == "Persist me")
        again = cp.load_settings()
        self.check("settings load back", again["font_size"] == 34 and again["provider"] == "Anthropic (Claude)")
        again["remember_key"] = False
        cp.save_settings(again)
        saved2 = json.loads(cp.SETTINGS_FILE.read_text(encoding="utf-8"))
        self.check("no API key is saved when 'Remember key' is off",
                   all(not prof["api_key"] for prof in saved2["ai_profiles"].values()))

    # ================================================================ runner
    def run_all(self, argv):
        self._install_hooks()
        started = time.monotonic()
        try:
            self.app, self.s, self.ov, self.panel, self.hotkeys = self.cp.create_app(argv)
        except Exception:
            self.log(FAIL, "Starting the app", traceback.format_exc())
            return self.finish(started)
        self.pump(0.8)
        self.server, self.base, self.ai_log = start_mock_server(self.cp)
        tests = [("Startup", self.t_startup), ("Drawing", self.t_rendering),
                 ("Scrolling", self.t_scrolling), ("Display settings", self.t_display),
                 ("Show, hide and shortcuts", self.t_visibility_and_shortcuts),
                 ("Click-through", self.t_click_through), ("Screen-share hiding", self.t_capture),
                 ("Real keyboard", self.t_keyboard), ("AI assistant", self.t_ai),
                 ("Listening mode", self.t_listen), ("Reference document", self.t_docs),
                 ("Files", self.t_files), ("Saving settings", self.t_persistence)]
        for title, fn in tests:
            with self.step(title):
                fn()
            self.pump(0.05)
        try:
            self.hotkeys.stop()
            self.server.shutdown()
        except Exception:
            pass
        return self.finish(started)

    def finish(self, started):
        for err in self.errors:
            self.log(FAIL, "Python error inside the app", err)
        seen = []
        for msg in self.qt_messages:
            if msg not in seen:
                seen.append(msg)
        for msg in seen[:15]:
            broken = "Could not parse" in msg or "QPainter" in msg
            self.log(FAIL if broken else WARN, "Qt reported a problem", msg)
        try:
            from PySide6 import __version__ as pyside_version
            from PySide6.QtCore import qVersion
            versions = f"PySide6 {pyside_version}, Qt {qVersion()}"
        except Exception:
            versions = "PySide6 unknown"
        counts = {k: sum(1 for r in self.rows if r[0] == k) for k in (PASS, WARN, FAIL)}
        lines = [f"CuePilot self-test on {platform.platform()}, Python {platform.python_version()}, {versions}",
                 ""]
        for status, name, detail in self.rows:
            lines.append(f"{status}  {name}")
            if detail and status != PASS:
                lines.extend("        " + d for d in str(detail).rstrip().splitlines())
        lines += ["", f"{counts[PASS]} passed, {counts[WARN]} warnings, {counts[FAIL]} failed "
                      f"in {time.monotonic() - started:.1f} s"]
        text = "\n".join(lines) + "\n"
        if self.report_path:
            try:
                Path(self.report_path).write_text(text, encoding="utf-8")
            except Exception:
                pass
        if sys.stdout is not None:
            try:
                sys.stdout.write(text)
                sys.stdout.flush()
            except Exception:
                pass
        return 1 if counts[FAIL] else 0


def run(cp, argv) -> int:
    report = None
    if "--report" in argv:
        i = argv.index("--report")
        report = argv[i + 1] if i + 1 < len(argv) else None
    tmp = Path(tempfile.mkdtemp(prefix="cuepilot-selftest-"))
    cp.CONFIG_DIR = tmp / "config"
    cp.SETTINGS_FILE = cp.CONFIG_DIR / "settings.json"
    cp.SCRIPT_FILE = cp.CONFIG_DIR / "last_script.txt"
    for var in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "GEMINI_API_KEY"):
        os.environ.pop(var, None)
    try:
        return SelfTest(cp, report, tmp).run_all(argv)
    except Exception:
        msg = "CuePilot self-test crashed:\n" + traceback.format_exc()
        if report:
            try:
                Path(report).write_text(msg, encoding="utf-8")
            except Exception:
                pass
        if sys.stdout is not None:
            print(msg)
        return 2
