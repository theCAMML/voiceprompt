#!/usr/bin/env python3
"""VoicePrompt — macOS voice dictation with history panel."""

import os, sys, json, time, queue, signal, logging, tempfile
import threading, subprocess, struct, wave, io, math
from pathlib import Path

# ── Logging ────────────────────────────────────────────────────────────────────
LOG_DIR = Path.home() / ".voiceprompt"
LOG_DIR.mkdir(parents=True, exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(LOG_DIR / "voiceprompt.log"),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger("voiceprompt")

HISTORY_FILE = LOG_DIR / "history.json"
MAX_HISTORY  = 50

# ── Config ─────────────────────────────────────────────────────────────────────
CONFIG_PATH = LOG_DIR / "config.json"
DEFAULT_CONFIG = {"whisper_model": "base", "sample_rate": 16000,
                  "channels": 1, "chunk_size": 1024, "auto_type": True}

def load_config():
    if CONFIG_PATH.exists():
        try:
            with open(CONFIG_PATH) as f:
                return {**DEFAULT_CONFIG, **json.load(f)}
        except Exception:
            pass
    with open(CONFIG_PATH, "w") as f:
        json.dump(DEFAULT_CONFIG, f, indent=2)
    return dict(DEFAULT_CONFIG)

# ── History ────────────────────────────────────────────────────────────────────
def load_history():
    if HISTORY_FILE.exists():
        try:
            with open(HISTORY_FILE) as f:
                return json.load(f)
        except Exception:
            pass
    return []

def save_history(entries):
    with open(HISTORY_FILE, "w") as f:
        json.dump(entries[-MAX_HISTORY:], f, indent=2)

# ── Whisper ────────────────────────────────────────────────────────────────────
_whisper_model = None
_whisper_lock  = threading.Lock()

def get_whisper_model(name="base"):
    global _whisper_model
    with _whisper_lock:
        if _whisper_model is None:
            log.info(f"Loading Whisper {name}…")
            import whisper
            _whisper_model = whisper.load_model(name)
            log.info("Whisper ready.")
    return _whisper_model

# ── Clipboard / typing ─────────────────────────────────────────────────────────
def copy_to_clipboard(text):
    subprocess.run(["pbcopy"], input=text.encode(), check=True)
    log.info("Copied to clipboard.")

def has_accessibility():
    try:
        r = subprocess.run(
            ["osascript", "-e",
             'tell application "System Events" to return UI elements enabled'],
            capture_output=True, text=True, timeout=3)
        return r.stdout.strip().lower() == "true"
    except Exception:
        return False

def auto_type(text):
    escaped = text.replace("\\","\\\\").replace('"','\\"')
    r = subprocess.run(
        ["osascript", "-e",
         f'tell application "System Events" to keystroke "{escaped}"'],
        capture_output=True, timeout=10)
    return r.returncode == 0

# ── Audio ──────────────────────────────────────────────────────────────────────
class AudioRecorder:
    def __init__(self, sr=16000, ch=1, chunk=1024):
        self.sr, self.ch, self.chunk = sr, ch, chunk
        self._pa = self._stream = None
        self._chunks = []
        self._recording = False
        self.on_level = None

    def _init_pa(self):
        if self._pa is None:
            import pyaudio
            self._pa = pyaudio.PyAudio()

    def start(self):
        self._init_pa()
        import pyaudio
        self._chunks = []
        self._recording = True
        self._stream = self._pa.open(
            format=pyaudio.paInt16, channels=self.ch, rate=self.sr,
            input=True, frames_per_buffer=self.chunk,
            stream_callback=self._cb)
        self._stream.start_stream()

    def _cb(self, data, fc, ti, st):
        import pyaudio
        if self._recording:
            self._chunks.append(data)
            if self.on_level:
                shorts = struct.unpack(f"{len(data)//2}h", data)
                rms = math.sqrt(sum(s*s for s in shorts)/len(shorts))/32768
                self.on_level(min(rms*10, 1.0))
        return (None, pyaudio.paContinue)

    def stop(self):
        self._recording = False
        if self._stream:
            self._stream.stop_stream()
            self._stream.close()
            self._stream = None
        raw = b"".join(self._chunks)
        buf = io.BytesIO()
        with wave.open(buf, "wb") as wf:
            wf.setnchannels(self.ch); wf.setsampwidth(2)
            wf.setframerate(self.sr); wf.writeframes(raw)
        return buf.getvalue()

    def cleanup(self):
        if self._pa:
            self._pa.terminate(); self._pa = None

def transcribe(wav_bytes, model_name):
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
        f.write(wav_bytes); tmp = f.name
    try:
        t0 = time.time()
        result = get_whisper_model(model_name).transcribe(tmp, language="en", fp16=False)
        text = result["text"].strip()
        log.info(f"Transcribed in {time.time()-t0:.2f}s: {text!r}")
        return text
    finally:
        os.unlink(tmp)

# ── AppKit UI ──────────────────────────────────────────────────────────────────
import AppKit, objc
from Foundation import NSObject, NSTimer

_ui_q   = queue.Queue()
_wc_ref = [None]

def ui(kind, **kw):
    _ui_q.put({"kind": kind, **kw})

def make_label(parent, frame, text, size=13, bold=False, color=None,
               align=AppKit.NSTextAlignmentLeft):
    lbl = AppKit.NSTextField.alloc().initWithFrame_(frame)
    lbl.setStringValue_(text)
    lbl.setFont_(AppKit.NSFont.boldSystemFontOfSize_(size) if bold
                 else AppKit.NSFont.systemFontOfSize_(size))
    if color:
        lbl.setTextColor_(color)
    lbl.setAlignment_(align)
    lbl.setBezeled_(False)
    lbl.setDrawsBackground_(False)
    lbl.setEditable_(False)
    lbl.setSelectable_(False)
    parent.addSubview_(lbl)
    return lbl

def build_window():
    W, H = 480, 580
    style = (AppKit.NSWindowStyleMaskTitled |
             AppKit.NSWindowStyleMaskClosable |
             AppKit.NSWindowStyleMaskMiniaturizable |
             AppKit.NSWindowStyleMaskResizable)
    win = AppKit.NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
        AppKit.NSMakeRect(0, 0, W, H), style,
        AppKit.NSBackingStoreBuffered, False)
    win.setTitle_("🎙  VoicePrompt")
    win.center()
    win.setLevel_(AppKit.NSFloatingWindowLevel)
    win.setMinSize_(AppKit.NSMakeSize(380, 480))
    cv = win.contentView()

    # ── Status label ─────────────────────────────────────────────────────────
    lbl_status = make_label(cv, AppKit.NSMakeRect(20, H-45, W-40, 28),
                            "Loading Whisper…", size=14, bold=True,
                            align=AppKit.NSTextAlignmentCenter)

    # ── Record button ─────────────────────────────────────────────────────────
    btn = AppKit.NSButton.alloc().initWithFrame_(
        AppKit.NSMakeRect(W//2-85, H-100, 170, 44))
    btn.setTitle_("⏺  Start Recording")
    btn.setBezelStyle_(AppKit.NSBezelStyleRounded)
    btn.setFont_(AppKit.NSFont.boldSystemFontOfSize_(14))
    btn.setEnabled_(False)
    cv.addSubview_(btn)

    # ── Meter bars ────────────────────────────────────────────────────────────
    bars = []
    n, bw, gap = 36, 9, 2
    sx = (W - (n*(bw+gap)-gap)) // 2
    for i in range(n):
        bar = AppKit.NSBox.alloc().initWithFrame_(
            AppKit.NSMakeRect(sx+i*(bw+gap), H-190, bw, 70))
        bar.setBoxType_(AppKit.NSBoxCustom)
        bar.setFillColor_(AppKit.NSColor.colorWithRed_green_blue_alpha_(0.15,0.15,0.18,1))
        bar.setBorderWidth_(0)
        bar.setCornerRadius_(3)
        cv.addSubview_(bar)
        bars.append(bar)

    # ── Latest transcript ─────────────────────────────────────────────────────
    make_label(cv, AppKit.NSMakeRect(20, H-215, W-40, 18),
               "Latest — click Copy to grab it again",
               size=11, color=AppKit.NSColor.secondaryLabelColor())

    # latest text view
    latest_scroll = AppKit.NSScrollView.alloc().initWithFrame_(
        AppKit.NSMakeRect(20, H-305, W-40, 80))
    latest_scroll.setBorderType_(AppKit.NSBezelBorder)
    latest_scroll.setHasVerticalScroller_(True)
    latest_scroll.setAutohidesScrollers_(True)
    latest_tv = AppKit.NSTextView.alloc().initWithFrame_(
        AppKit.NSMakeRect(0, 0, W-40, 80))
    latest_tv.setEditable_(False)
    latest_tv.setSelectable_(True)
    latest_tv.setFont_(AppKit.NSFont.systemFontOfSize_(13))
    latest_tv.setString_("…")
    latest_scroll.setDocumentView_(latest_tv)
    cv.addSubview_(latest_scroll)

    # copy latest button
    btn_copy = AppKit.NSButton.alloc().initWithFrame_(
        AppKit.NSMakeRect(W-120, H-325, 100, 28))
    btn_copy.setTitle_("📋 Copy")
    btn_copy.setBezelStyle_(AppKit.NSBezelStyleRounded)
    btn_copy.setFont_(AppKit.NSFont.systemFontOfSize_(12))
    btn_copy.setEnabled_(False)
    cv.addSubview_(btn_copy)

    # ── History panel ─────────────────────────────────────────────────────────
    make_label(cv, AppKit.NSMakeRect(20, H-355, 120, 18),
               "History", size=12, bold=True)

    btn_clear = AppKit.NSButton.alloc().initWithFrame_(
        AppKit.NSMakeRect(W-100, H-358, 80, 24))
    btn_clear.setTitle_("🗑 Clear")
    btn_clear.setBezelStyle_(AppKit.NSBezelStyleRounded)
    btn_clear.setFont_(AppKit.NSFont.systemFontOfSize_(11))
    cv.addSubview_(btn_clear)

    hist_scroll = AppKit.NSScrollView.alloc().initWithFrame_(
        AppKit.NSMakeRect(20, 20, W-40, H-375))
    hist_scroll.setBorderType_(AppKit.NSBezelBorder)
    hist_scroll.setHasVerticalScroller_(True)
    hist_scroll.setAutohidesScrollers_(True)

    hist_tv = AppKit.NSTextView.alloc().initWithFrame_(
        AppKit.NSMakeRect(0, 0, W-40, H-375))
    hist_tv.setEditable_(False)
    hist_tv.setSelectable_(True)
    hist_tv.setFont_(AppKit.NSFont.systemFontOfSize_(12))
    hist_tv.setString_("No history yet.")
    hist_scroll.setDocumentView_(hist_tv)
    cv.addSubview_(hist_scroll)

    win.makeKeyAndOrderFront_(None)
    return win, lbl_status, btn, bars, latest_tv, btn_copy, hist_tv, btn_clear


# ── Controller ─────────────────────────────────────────────────────────────────
class VPController(NSObject):
    def init(self):
        self = objc.super(VPController, self).init()
        if self is None: return None
        self._level   = 0.0
        self._history = load_history()   # list of {ts, text}
        (self._win, self._lbl, self._btn, self._bars,
         self._latest_tv, self._btn_copy,
         self._hist_tv, self._btn_clear) = build_window()

        self._btn.setTarget_(self)
        self._btn.setAction_("onRecord:")
        self._btn_copy.setTarget_(self)
        self._btn_copy.setAction_("onCopy:")
        self._btn_clear.setTarget_(self)
        self._btn_clear.setAction_("onClear:")

        self._refresh_history()

        NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
            0.05, self, "tick:", None, True)
        return self

    def onRecord_(self, sender):
        if _app_ref["recording"]:
            _app_ref["stop_evt"].set()
        else:
            threading.Thread(target=_app_ref["do_record"], daemon=True).start()

    def onCopy_(self, sender):
        entries = self._history
        if entries:
            copy_to_clipboard(entries[-1]["text"])
            self._lbl.setStringValue_("📋 Copied to clipboard!")
            self._lbl.setTextColor_(AppKit.NSColor.systemGreenColor())
            threading.Thread(
                target=lambda: (time.sleep(2), ui("status", text="Ready — click button to record")),
                daemon=True).start()

    def onClear_(self, sender):
        self._history = []
        save_history([])
        self._hist_tv.setString_("No history yet.")
        self._latest_tv.setString_("…")
        self._btn_copy.setEnabled_(False)

    def _refresh_history(self):
        if not self._history:
            self._hist_tv.setString_("No history yet.")
            return
        lines = []
        for i, e in enumerate(reversed(self._history), 1):
            ts   = e.get("ts", "")
            text = e.get("text", "")
            lines.append(f"[{ts}]\n{text}\n")
        self._hist_tv.setString_("\n".join(lines))
        self._hist_tv.scrollToBeginningOfDocument_(None)

    def tick_(self, _):
        import random
        try:
            while True:
                msg = _ui_q.get_nowait()
                k = msg["kind"]
                if k == "status":
                    self._lbl.setStringValue_(msg["text"])
                    c = {"red":    AppKit.NSColor.systemRedColor(),
                         "green":  AppKit.NSColor.systemGreenColor(),
                         "orange": AppKit.NSColor.systemOrangeColor()}.get(
                             msg.get("color"), AppKit.NSColor.labelColor())
                    self._lbl.setTextColor_(c)
                elif k == "transcript":
                    text = msg["text"]
                    ts   = time.strftime("%H:%M:%S")
                    self._latest_tv.setString_(text)
                    self._btn_copy.setEnabled_(True)
                    # add to history
                    self._history.append({"ts": ts, "text": text})
                    save_history(self._history)
                    self._refresh_history()
                elif k == "level":
                    self._level = msg["v"]
                elif k == "btn":
                    self._btn.setTitle_(msg["title"])
                    if "enabled" in msg:
                        self._btn.setEnabled_(msg["enabled"])
                elif k == "ready":
                    self._btn.setEnabled_(True)
                    self._lbl.setStringValue_("Ready — click button to record")
                    self._lbl.setTextColor_(AppKit.NSColor.labelColor())
        except queue.Empty:
            pass

        # animate meter bars
        n = len(self._bars)
        for i, bar in enumerate(self._bars):
            thresh = i / n
            noise  = random.uniform(-0.06, 0.06)
            active = (self._level + noise) > thresh and self._level > 0.02
            if active:
                mid   = 1 - abs(i - n/2) / (n/2)
                color = AppKit.NSColor.colorWithRed_green_blue_alpha_(
                    0.05, 0.35+0.55*mid, 0.18, 1.0)
            else:
                color = AppKit.NSColor.colorWithRed_green_blue_alpha_(
                    0.15, 0.15, 0.18, 1.0)
            bar.setFillColor_(color)
        self._level = max(0.0, self._level - 0.05)


# ── Recording logic ────────────────────────────────────────────────────────────
_app_ref = {"recording": False, "stop_evt": threading.Event(), "do_record": None}

def make_record_fn(cfg, recorder):
    def do_record():
        if _app_ref["recording"]:
            return
        _app_ref["recording"] = True
        _app_ref["stop_evt"].clear()

        ui("status", text="🔴  Recording…  click Stop when done", color="red")
        ui("btn", title="⏹  Stop Recording", enabled=True)
        recorder.on_level = lambda v: ui("level", v=v)
        recorder.start()
        _app_ref["stop_evt"].wait()

        wav = recorder.stop()
        recorder.on_level = None
        ui("level", v=0.0)
        ui("btn", title="⏺  Start Recording", enabled=True)
        ui("status", text="⏳  Transcribing…", color="orange")
        _app_ref["recording"] = False

        if len(wav) < 5000:
            ui("status", text="Too short — try again.")
            time.sleep(1.5)
            ui("status", text="Ready — click button to record")
            return

        try:
            text = transcribe(wav, cfg["whisper_model"])
            if not text:
                ui("status", text="Nothing heard — try again.")
                time.sleep(2)
                ui("status", text="Ready — click button to record")
                return

            # always copy latest to clipboard immediately
            copy_to_clipboard(text)
            ui("transcript", text=text)

            if cfg.get("auto_type") and has_accessibility():
                if auto_type(text):
                    ui("status", text="✅  Typed into app  ·  history saved", color="green")
                else:
                    ui("status", text="✅  Copied — press ⌘V to paste  ·  history saved", color="green")
            else:
                ui("status", text="✅  Copied — press ⌘V to paste  ·  history saved", color="green")

            time.sleep(3)
            ui("status", text="Ready — click button to record")

        except Exception as e:
            log.error(f"Error: {e}")
            ui("status", text=f"Error: {e}", color="red")
            time.sleep(3)
            ui("status", text="Ready — click button to record")

    return do_record


# ── Main ───────────────────────────────────────────────────────────────────────
def main():
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    cfg      = load_config()
    recorder = AudioRecorder(cfg["sample_rate"], cfg["channels"], cfg["chunk_size"])
    _app_ref["do_record"] = make_record_fn(cfg, recorder)
    _app_ref["stop_evt"]  = threading.Event()

    threading.Thread(
        target=lambda: (get_whisper_model(cfg["whisper_model"]), ui("ready")),
        daemon=True).start()

    app = AppKit.NSApplication.sharedApplication()
    app.setActivationPolicy_(AppKit.NSApplicationActivationPolicyAccessory)
    _wc_ref[0] = VPController.alloc().init()
    app.run()

if __name__ == "__main__":
    main()
