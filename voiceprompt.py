#!/usr/bin/env python3
"""
VoicePrompt v3.0 — macOS voice dictation
- Button UI + optional hotkey (Cmd+Shift+Space)
- Live voice meter
- Microphone selector
- History panel with pin, copy-all, export
- Model switcher
- Wake word (experimental)
"""

import os, sys, json, time, queue, signal, logging, tempfile
import threading, subprocess, struct, wave, io, math, random
from pathlib import Path
from datetime import datetime

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
PINNED_FILE  = LOG_DIR / "pinned.json"
MAX_HISTORY  = 100

# ── Config ─────────────────────────────────────────────────────────────────────
CONFIG_PATH = LOG_DIR / "config.json"
DEFAULT_CONFIG = {
    "whisper_model":  "base",
    "auto_type":      True,
    "sample_rate":    16000,
    "channels":       1,
    "chunk_size":     1024,
    "hotkey_enabled": False,
    "clear_on_start": True,
    "mic_index":      None,   # None = default mic
    "wake_word":      "",     # "" = disabled
}

def load_config():
    if CONFIG_PATH.exists():
        try:
            with open(CONFIG_PATH) as f:
                return {**DEFAULT_CONFIG, **json.load(f)}
        except Exception:
            pass
    cfg = dict(DEFAULT_CONFIG)
    with open(CONFIG_PATH, "w") as f:
        json.dump(cfg, f, indent=2)
    return cfg

def save_config(cfg):
    with open(CONFIG_PATH, "w") as f:
        json.dump(cfg, f, indent=2)

# ── History / Pinned ───────────────────────────────────────────────────────────
def load_json(path, default):
    if path.exists():
        try:
            with open(path) as f:
                return json.load(f)
        except Exception:
            pass
    return default

def save_json(path, data):
    with open(path, "w") as f:
        json.dump(data, f, indent=2)

# ── Whisper ────────────────────────────────────────────────────────────────────
WHISPER_MODELS = ["base", "small", "medium", "large-v3"]
_whisper_cache = {}
_whisper_lock  = threading.Lock()

def get_whisper_model(name="base"):
    with _whisper_lock:
        if name not in _whisper_cache:
            log.info(f"Loading Whisper {name}…")
            import whisper
            _whisper_cache[name] = whisper.load_model(name)
            log.info(f"Whisper {name} ready.")
    return _whisper_cache[name]

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

# ── Mic enumeration ────────────────────────────────────────────────────────────
def list_mics():
    """Returns list of (index, name) for input devices."""
    try:
        import pyaudio
        pa = pyaudio.PyAudio()
        mics = []
        for i in range(pa.get_device_count()):
            info = pa.get_device_info_by_index(i)
            if info.get("maxInputChannels", 0) > 0:
                mics.append((i, info["name"]))
        pa.terminate()
        return mics
    except Exception:
        return []

# ── Audio ──────────────────────────────────────────────────────────────────────
class AudioRecorder:
    def __init__(self, sr=16000, ch=1, chunk=1024, mic_index=None):
        self.sr, self.ch, self.chunk = sr, ch, chunk
        self.mic_index = mic_index
        self._pa = self._stream = None
        self._chunks = []
        self._recording = False
        self.on_level = None

    def set_mic(self, index):
        self.mic_index = index

    def _init_pa(self):
        if self._pa is None:
            import pyaudio
            self._pa = pyaudio.PyAudio()

    def start(self):
        self._init_pa()
        import pyaudio
        self._chunks = []
        self._recording = True
        kwargs = dict(
            format=pyaudio.paInt16, channels=self.ch, rate=self.sr,
            input=True, frames_per_buffer=self.chunk,
            stream_callback=self._cb)
        if self.mic_index is not None:
            kwargs["input_device_index"] = self.mic_index
        self._stream = self._pa.open(**kwargs)
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

def transcribe(wav_bytes, model_name, initial_prompt=""):
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
        f.write(wav_bytes); tmp = f.name
    try:
        t0 = time.time()
        kw = dict(language="en", fp16=False)
        if initial_prompt:
            kw["initial_prompt"] = initial_prompt
        result = get_whisper_model(model_name).transcribe(tmp, **kw)
        text = result["text"].strip()
        log.info(f"Transcribed in {time.time()-t0:.2f}s: {text!r}")
        return text
    finally:
        os.unlink(tmp)

# ── AppKit ─────────────────────────────────────────────────────────────────────
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

def make_scroll_textview(parent, frame, placeholder="", font_size=12):
    scroll = AppKit.NSScrollView.alloc().initWithFrame_(frame)
    scroll.setBorderType_(AppKit.NSBezelBorder)
    scroll.setHasVerticalScroller_(True)
    scroll.setAutohidesScrollers_(True)
    tv = AppKit.NSTextView.alloc().initWithFrame_(
        AppKit.NSMakeRect(0, 0, frame.size.width, frame.size.height))
    tv.setEditable_(False)
    tv.setSelectable_(True)
    tv.setFont_(AppKit.NSFont.systemFontOfSize_(font_size))
    if placeholder:
        tv.setString_(placeholder)
    scroll.setDocumentView_(tv)
    parent.addSubview_(scroll)
    return scroll, tv

def make_button(parent, frame, title, font_size=12, enabled=True):
    btn = AppKit.NSButton.alloc().initWithFrame_(frame)
    btn.setTitle_(title)
    btn.setBezelStyle_(AppKit.NSBezelStyleRounded)
    btn.setFont_(AppKit.NSFont.systemFontOfSize_(font_size))
    btn.setEnabled_(enabled)
    parent.addSubview_(btn)
    return btn

def build_window(mics, cfg):
    W, H = 520, 680
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
    win.setMinSize_(AppKit.NSMakeSize(420, 560))
    cv = win.contentView()
    y = H

    # ── Status ───────────────────────────────────────────────────────────────
    y -= 42
    lbl_status = make_label(cv, AppKit.NSMakeRect(20, y, W-40, 28),
                            "Loading Whisper…", size=14, bold=True,
                            align=AppKit.NSTextAlignmentCenter)
    y -= 48
    # ── Record button ─────────────────────────────────────────────────────────
    btn_rec = make_button(cv, AppKit.NSMakeRect(W//2-90, y, 180, 44),
                          "⏺  Start Recording", font_size=14, enabled=False)

    # ── Toolbar row: Copy All | Export | Clear ────────────────────────────────
    btn_copyall = make_button(cv, AppKit.NSMakeRect(20, y+8, 100, 28),
                              "📋 Copy All", font_size=11, enabled=False)
    btn_export  = make_button(cv, AppKit.NSMakeRect(125, y+8, 90, 28),
                              "💾 Export", font_size=11, enabled=False)
    btn_clear   = make_button(cv, AppKit.NSMakeRect(W-110, y+8, 90, 28),
                              "🗑 Clear", font_size=11)
    y -= 8

    # ── Meter bars ────────────────────────────────────────────────────────────
    y -= 82
    bars = []
    n, bw, gap = 40, 8, 2
    sx = (W - (n*(bw+gap)-gap)) // 2
    for i in range(n):
        bar = AppKit.NSBox.alloc().initWithFrame_(
            AppKit.NSMakeRect(sx+i*(bw+gap), y, bw, 70))
        bar.setBoxType_(AppKit.NSBoxCustom)
        bar.setFillColor_(AppKit.NSColor.colorWithRed_green_blue_alpha_(0.15,0.15,0.18,1))
        bar.setBorderWidth_(0); bar.setCornerRadius_(3)
        cv.addSubview_(bar); bars.append(bar)
    y -= 6

    # ── Settings row ─────────────────────────────────────────────────────────
    y -= 26
    # Mic dropdown
    make_label(cv, AppKit.NSMakeRect(20, y+3, 30, 18), "Mic:", size=11)
    mic_popup = AppKit.NSPopUpButton.alloc().initWithFrame_(
        AppKit.NSMakeRect(50, y, 180, 24))
    mic_popup.addItemWithTitle_("Default")
    for idx, name in mics:
        mic_popup.addItemWithTitle_(f"{name[:28]}")
    mic_popup.setFont_(AppKit.NSFont.systemFontOfSize_(11))
    cv.addSubview_(mic_popup)

    # Model dropdown
    make_label(cv, AppKit.NSMakeRect(240, y+3, 42, 18), "Model:", size=11)
    model_popup = AppKit.NSPopUpButton.alloc().initWithFrame_(
        AppKit.NSMakeRect(284, y, 110, 24))
    for m in WHISPER_MODELS:
        model_popup.addItemWithTitle_(m)
    try:
        model_popup.selectItemAtIndex_(WHISPER_MODELS.index(cfg["whisper_model"]))
    except Exception:
        pass
    model_popup.setFont_(AppKit.NSFont.systemFontOfSize_(11))
    cv.addSubview_(model_popup)

    # Hotkey checkbox
    chk_hotkey = AppKit.NSButton.alloc().initWithFrame_(
        AppKit.NSMakeRect(405, y+2, 110, 20))
    chk_hotkey.setButtonType_(AppKit.NSButtonTypeSwitch)
    chk_hotkey.setTitle_("Hotkey")
    chk_hotkey.setFont_(AppKit.NSFont.systemFontOfSize_(11))
    chk_hotkey.setState_(1 if cfg.get("hotkey_enabled") else 0)
    cv.addSubview_(chk_hotkey)
    y -= 6

    # ── Hint ─────────────────────────────────────────────────────────────────
    y -= 18
    lbl_hint = make_label(cv, AppKit.NSMakeRect(20, y, W-40, 16),
                          "Transcript copies to clipboard automatically · ⌘V to paste",
                          size=10, color=AppKit.NSColor.secondaryLabelColor(),
                          align=AppKit.NSTextAlignmentCenter)

    # ── Latest label + copy button ────────────────────────────────────────────
    y -= 22
    make_label(cv, AppKit.NSMakeRect(20, y, 80, 16), "Latest:", size=11, bold=True)
    btn_copy_latest = make_button(cv, AppKit.NSMakeRect(W-110, y-2, 90, 22),
                                  "📋 Copy", font_size=11, enabled=False)
    btn_pin = make_button(cv, AppKit.NSMakeRect(W-205, y-2, 90, 22),
                          "📌 Pin", font_size=11, enabled=False)
    y -= 4

    # Latest text view
    y -= 70
    _, latest_tv = make_scroll_textview(cv, AppKit.NSMakeRect(20, y, W-40, 66),
                                        "…", font_size=13)

    # ── History ───────────────────────────────────────────────────────────────
    y -= 26
    make_label(cv, AppKit.NSMakeRect(20, y+4, 80, 16), "History:", size=11, bold=True)
    make_label(cv, AppKit.NSMakeRect(100, y+4, W-200, 16),
               "(click any entry to copy it)",
               size=10, color=AppKit.NSColor.secondaryLabelColor())

    # Pinned section label
    y -= 4
    lbl_pinned_hdr = make_label(cv, AppKit.NSMakeRect(20, y-16, 120, 14),
                                "📌 Pinned:", size=10, bold=True)

    y -= 18
    _, pinned_tv = make_scroll_textview(cv, AppKit.NSMakeRect(20, y-50, W-40, 48),
                                        "No pinned items.", font_size=11)
    y -= 56

    make_label(cv, AppKit.NSMakeRect(20, y-2, 120, 14), "📋 Recent:", size=10, bold=True)
    y -= 6
    hist_scroll, hist_tv = make_scroll_textview(
        cv, AppKit.NSMakeRect(20, 20, W-40, y-24), "No history yet.", font_size=12)

    win.makeKeyAndOrderFront_(None)
    return (win, lbl_status, btn_rec, bars,
            latest_tv, btn_copy_latest, btn_pin,
            btn_copyall, btn_export, btn_clear,
            hist_tv, pinned_tv,
            mic_popup, model_popup, chk_hotkey, mics)


# ── Controller ─────────────────────────────────────────────────────────────────
class VPController(NSObject):
    def init(self):
        self = objc.super(VPController, self).init()
        if self is None: return None
        self._level   = 0.0
        self._history = load_json(HISTORY_FILE, [])
        self._pinned  = load_json(PINNED_FILE, [])
        self._cfg     = load_config()
        self._mics    = list_mics()

        (self._win, self._lbl, self._btn_rec, self._bars,
         self._latest_tv, self._btn_copy_latest, self._btn_pin,
         self._btn_copyall, self._btn_export, self._btn_clear,
         self._hist_tv, self._pinned_tv,
         self._mic_popup, self._model_popup, self._chk_hotkey,
         mics) = build_window(self._mics, self._cfg)

        self._btn_rec.setTarget_(self)
        self._btn_rec.setAction_("onRecord:")
        self._btn_copy_latest.setTarget_(self)
        self._btn_copy_latest.setAction_("onCopyLatest:")
        self._btn_pin.setTarget_(self)
        self._btn_pin.setAction_("onPin:")
        self._btn_copyall.setTarget_(self)
        self._btn_copyall.setAction_("onCopyAll:")
        self._btn_export.setTarget_(self)
        self._btn_export.setAction_("onExport:")
        self._btn_clear.setTarget_(self)
        self._btn_clear.setAction_("onClear:")
        self._mic_popup.setTarget_(self)
        self._mic_popup.setAction_("onMicChange:")
        self._model_popup.setTarget_(self)
        self._model_popup.setAction_("onModelChange:")
        self._chk_hotkey.setTarget_(self)
        self._chk_hotkey.setAction_("onHotkeyToggle:")

        self._refresh_history()
        self._refresh_pinned()

        NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
            0.05, self, "tick:", None, True)
        return self

    # ── Actions ───────────────────────────────────────────────────────────────
    def onRecord_(self, sender):
        if _app_ref["recording"]:
            _app_ref["stop_evt"].set()
        else:
            threading.Thread(target=_app_ref["do_record"], daemon=True).start()

    def onCopyLatest_(self, sender):
        if self._history:
            copy_to_clipboard(self._history[-1]["text"])
            ui("status", text="📋 Latest copied!", color="green")
            threading.Thread(target=lambda: (time.sleep(2),
                ui("status", text="Ready — click button to record")), daemon=True).start()

    def onPin_(self, sender):
        if self._history:
            entry = self._history[-1]
            if not any(p["text"] == entry["text"] for p in self._pinned):
                self._pinned.append(entry)
                save_json(PINNED_FILE, self._pinned)
                self._refresh_pinned()
                ui("status", text="📌 Pinned!", color="green")
                threading.Thread(target=lambda: (time.sleep(2),
                    ui("status", text="Ready — click button to record")), daemon=True).start()

    def onCopyAll_(self, sender):
        if self._history:
            combined = "\n\n".join(e["text"] for e in self._history)
            copy_to_clipboard(combined)
            ui("status", text=f"📋 All {len(self._history)} entries copied!", color="green")
            threading.Thread(target=lambda: (time.sleep(2),
                ui("status", text="Ready — click button to record")), daemon=True).start()

    def onExport_(self, sender):
        if not self._history:
            return
        ts  = datetime.now().strftime("%Y%m%d-%H%M%S")
        out = Path.home() / "Desktop" / f"voiceprompt-{ts}.txt"
        lines = []
        if self._pinned:
            lines.append("=== PINNED ===")
            for e in self._pinned:
                lines.append(f"[{e.get('ts','')}] {e['text']}\n")
        lines.append("=== HISTORY ===")
        for e in reversed(self._history):
            lines.append(f"[{e.get('ts','')}] {e['text']}\n")
        out.write_text("\n".join(lines))
        subprocess.run(["open", str(out)])
        ui("status", text=f"💾 Exported to Desktop!", color="green")
        threading.Thread(target=lambda: (time.sleep(2),
            ui("status", text="Ready — click button to record")), daemon=True).start()

    def onClear_(self, sender):
        self._history = []
        save_json(HISTORY_FILE, [])
        self._latest_tv.setString_("…")
        self._btn_copy_latest.setEnabled_(False)
        self._btn_pin.setEnabled_(False)
        self._btn_copyall.setEnabled_(False)
        self._btn_export.setEnabled_(False)
        self._hist_tv.setString_("No history yet.")

    def onMicChange_(self, sender):
        idx = self._mic_popup.indexOfSelectedItem()
        if idx == 0:
            self._cfg["mic_index"] = None
            _app_ref["recorder"].set_mic(None)
        else:
            mic_idx, _ = self._mics[idx-1]
            self._cfg["mic_index"] = mic_idx
            _app_ref["recorder"].set_mic(mic_idx)
        save_config(self._cfg)
        log.info(f"Mic changed to index {self._cfg['mic_index']}")

    def onModelChange_(self, sender):
        model = WHISPER_MODELS[self._model_popup.indexOfSelectedItem()]
        self._cfg["whisper_model"] = model
        save_config(self._cfg)
        _app_ref["model"][0] = model
        # pre-load in background
        threading.Thread(
            target=lambda: (ui("status", text=f"⏳ Loading {model}…", color="orange"),
                            get_whisper_model(model),
                            ui("status", text="Ready — click button to record")),
            daemon=True).start()

    def onHotkeyToggle_(self, sender):
        enabled = bool(self._chk_hotkey.state())
        self._cfg["hotkey_enabled"] = enabled
        save_config(self._cfg)
        _app_ref["hotkey_enabled"][0] = enabled
        if enabled and not _app_ref["hotkey_running"][0]:
            threading.Thread(target=_app_ref["start_hotkey"], daemon=True).start()
        log.info(f"Hotkey {'enabled' if enabled else 'disabled'}")

    # ── Refresh helpers ───────────────────────────────────────────────────────
    def _refresh_history(self):
        if not self._history:
            self._hist_tv.setString_("No history yet.")
            return
        lines = []
        for e in reversed(self._history[-50:]):
            lines.append(f"[{e.get('ts','')}]  {e['text']}")
        self._hist_tv.setString_("\n\n".join(lines))
        self._hist_tv.scrollToBeginningOfDocument_(None)

    def _refresh_pinned(self):
        if not self._pinned:
            self._pinned_tv.setString_("No pinned items.")
            return
        lines = [f"[{e.get('ts','')}]  {e['text']}" for e in reversed(self._pinned)]
        self._pinned_tv.setString_("\n\n".join(lines))

    # ── Timer ─────────────────────────────────────────────────────────────────
    def tick_(self, _):
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
                    ts   = datetime.now().strftime("%H:%M:%S")
                    self._latest_tv.setString_(text)
                    self._btn_copy_latest.setEnabled_(True)
                    self._btn_pin.setEnabled_(True)
                    self._btn_copyall.setEnabled_(True)
                    self._btn_export.setEnabled_(True)
                    self._history.append({"ts": ts, "text": text})
                    save_json(HISTORY_FILE, self._history[-MAX_HISTORY:])
                    self._refresh_history()
                elif k == "level":
                    self._level = msg["v"]
                elif k == "btn_rec":
                    self._btn_rec.setTitle_(msg["title"])
                    if "enabled" in msg:
                        self._btn_rec.setEnabled_(msg["enabled"])
                elif k == "ready":
                    self._btn_rec.setEnabled_(True)
                    self._lbl.setStringValue_("Ready — click button to record")
                    self._lbl.setTextColor_(AppKit.NSColor.labelColor())
        except queue.Empty:
            pass

        # animate meter
        n = len(self._bars)
        for i, bar in enumerate(self._bars):
            thresh = i / n
            noise  = random.uniform(-0.05, 0.05)
            active = (self._level + noise) > thresh and self._level > 0.02
            if active:
                mid   = 1 - abs(i - n/2) / (n/2)
                color = AppKit.NSColor.colorWithRed_green_blue_alpha_(
                    0.05, 0.35+0.55*mid, 0.18, 1.0)
            else:
                color = AppKit.NSColor.colorWithRed_green_blue_alpha_(
                    0.15, 0.15, 0.18, 1.0)
            bar.setFillColor_(color)
        self._level = max(0.0, self._level - 0.04)


# ── Recording logic ────────────────────────────────────────────────────────────
_app_ref = {
    "recording":     False,
    "stop_evt":      threading.Event(),
    "do_record":     None,
    "recorder":      None,
    "model":         ["base"],
    "cfg":           None,
    "hotkey_enabled":  [False],
    "hotkey_running":  [False],
    "start_hotkey":  None,
}

def make_record_fn(cfg, recorder):
    def do_record():
        if _app_ref["recording"]:
            return
        _app_ref["recording"] = True
        _app_ref["stop_evt"].clear()

        if cfg.get("clear_on_start"):
            ui("transcript", text="…") if False else None  # don't add to history

        ui("status", text="🔴  Recording…  click Stop when done", color="red")
        ui("btn_rec", title="⏹  Stop Recording", enabled=True)
        recorder.on_level = lambda v: ui("level", v=v)
        recorder.start()
        _app_ref["stop_evt"].wait()

        wav = recorder.stop()
        recorder.on_level = None
        ui("level", v=0.0)
        ui("btn_rec", title="⏺  Start Recording", enabled=True)
        ui("status", text="⏳  Transcribing…", color="orange")
        _app_ref["recording"] = False

        if len(wav) < 5000:
            ui("status", text="Too short — try again.")
            time.sleep(1.5)
            ui("status", text="Ready — click button to record")
            return

        try:
            model = _app_ref["model"][0]
            text  = transcribe(wav, model)
            if not text:
                ui("status", text="Nothing heard — try again.")
                time.sleep(2)
                ui("status", text="Ready — click button to record")
                return

            copy_to_clipboard(text)
            ui("transcript", text=text)

            if cfg.get("auto_type") and has_accessibility():
                if auto_type(text):
                    ui("status", text="✅  Typed into app  ·  history saved", color="green")
                else:
                    ui("status", text="✅  Copied — press ⌘V  ·  history saved", color="green")
            else:
                ui("status", text="✅  Copied — press ⌘V  ·  history saved", color="green")

            time.sleep(3)
            ui("status", text="Ready — click button to record")

        except Exception as e:
            log.error(f"Error: {e}")
            ui("status", text=f"Error: {e}", color="red")
            time.sleep(3)
            ui("status", text="Ready — click button to record")

    return do_record


def make_hotkey_fn():
    def start_hotkey():
        _app_ref["hotkey_running"][0] = True
        try:
            from pynput import keyboard
            pressed = [False]

            def on_press(key):
                try:
                    if (key == keyboard.Key.space and
                            listener.pressed(keyboard.Key.cmd) and
                            listener.pressed(keyboard.Key.shift)):
                        if not pressed[0] and not _app_ref["recording"]:
                            pressed[0] = True
                            threading.Thread(
                                target=_app_ref["do_record"], daemon=True).start()
                except Exception:
                    pass

            def on_release(key):
                if key == keyboard.Key.space and pressed[0]:
                    pressed[0] = False
                    if _app_ref["recording"]:
                        _app_ref["stop_evt"].set()

            listener = keyboard.Listener(on_press=on_press, on_release=on_release)
            listener.start()
            log.info("Hotkey listener active (Cmd+Shift+Space).")
            listener.join()
        except Exception as e:
            log.warning(f"Hotkey unavailable: {e}")
        _app_ref["hotkey_running"][0] = False

    return start_hotkey


# ── Main ───────────────────────────────────────────────────────────────────────
def main():
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    cfg      = load_config()
    recorder = AudioRecorder(cfg["sample_rate"], cfg["channels"],
                             cfg["chunk_size"], cfg.get("mic_index"))

    _app_ref["do_record"]    = make_record_fn(cfg, recorder)
    _app_ref["stop_evt"]     = threading.Event()
    _app_ref["recorder"]     = recorder
    _app_ref["model"]        = [cfg["whisper_model"]]
    _app_ref["cfg"]          = cfg
    _app_ref["start_hotkey"] = make_hotkey_fn()
    _app_ref["hotkey_enabled"][0] = cfg.get("hotkey_enabled", False)

    # warm up Whisper
    threading.Thread(
        target=lambda: (get_whisper_model(cfg["whisper_model"]), ui("ready")),
        daemon=True).start()

    # start hotkey if enabled
    if cfg.get("hotkey_enabled"):
        threading.Thread(target=_app_ref["start_hotkey"], daemon=True).start()

    app = AppKit.NSApplication.sharedApplication()
    app.setActivationPolicy_(AppKit.NSApplicationActivationPolicyAccessory)
    _wc_ref[0] = VPController.alloc().init()
    app.run()

if __name__ == "__main__":
    main()
