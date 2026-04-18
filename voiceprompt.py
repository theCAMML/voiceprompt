#!/usr/bin/env python3
"""
VoicePrompt v4.1 — Futuristic macOS voice dictation
Dark HUD theme · neon cyan · spectrum meter · full feature set
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
    "mic_index":      None,
    "wake_word":      "",
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

def list_mics():
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
                self.on_level(min(rms * 10, 1.0))
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

# ── Colors ─────────────────────────────────────────────────────────────────────
def rgba(r, g, b, a=1.0):
    return AppKit.NSColor.colorWithCalibratedRed_green_blue_alpha_(r, g, b, a)

# Palette
C_BG       = rgba(0.06, 0.06, 0.09)        # main background
C_CARD     = rgba(0.09, 0.10, 0.14)        # card/panel fill
C_BORDER   = rgba(0.00, 0.80, 0.90, 0.30)  # cyan border dim
C_CYAN     = rgba(0.00, 0.85, 0.95)        # neon cyan — labels, status ready
C_CYAN2    = rgba(0.00, 0.55, 0.65)        # muted cyan — sublabels
C_WHITE    = rgba(0.88, 0.94, 1.00)        # near-white body text
C_DIM      = rgba(0.35, 0.42, 0.52)        # dim/placeholder text
C_RED      = rgba(1.00, 0.22, 0.32)        # recording red
C_AMBER    = rgba(1.00, 0.62, 0.10)        # transcribing amber
C_GREEN    = rgba(0.08, 0.95, 0.55)        # success green
C_BAR_OFF  = rgba(0.10, 0.12, 0.18)        # inactive meter bar
C_SEP      = rgba(0.00, 0.75, 0.88, 0.25)  # separator line

def mono(size):
    f = AppKit.NSFont.fontWithName_size_("SF Mono", size)
    if not f:
        f = AppKit.NSFont.fontWithName_size_("Menlo", size)
    if not f:
        f = AppKit.NSFont.monospacedSystemFontOfSize_weight_(
            size, AppKit.NSFontWeightRegular)
    return f

# ── Widget factories ───────────────────────────────────────────────────────────
def lbl(parent, x, y, w, h, text, size=12, bold=False,
        color=None, align=None, use_mono=False):
    """Make a non-editable label and add to parent."""
    tf = AppKit.NSTextField.alloc().initWithFrame_(
        AppKit.NSMakeRect(x, y, w, h))
    tf.setStringValue_(text)
    if use_mono:
        tf.setFont_(mono(size))
    elif bold:
        tf.setFont_(AppKit.NSFont.boldSystemFontOfSize_(size))
    else:
        tf.setFont_(AppKit.NSFont.systemFontOfSize_(size))
    tf.setTextColor_(color if color is not None else C_WHITE)
    tf.setAlignment_(align if align is not None else AppKit.NSTextAlignmentLeft)
    tf.setBezeled_(False)
    tf.setDrawsBackground_(False)
    tf.setEditable_(False)
    tf.setSelectable_(False)
    parent.addSubview_(tf)
    return tf

def sep(parent, x, y, w):
    """Thin horizontal separator."""
    box = AppKit.NSBox.alloc().initWithFrame_(AppKit.NSMakeRect(x, y, w, 1))
    box.setBoxType_(AppKit.NSBoxCustom)
    box.setFillColor_(C_SEP)
    box.setBorderWidth_(0)
    parent.addSubview_(box)
    return box

def card(parent, x, y, w, h, radius=8):
    """Dark rounded card."""
    box = AppKit.NSBox.alloc().initWithFrame_(AppKit.NSMakeRect(x, y, w, h))
    box.setBoxType_(AppKit.NSBoxCustom)
    box.setFillColor_(C_CARD)
    box.setBorderColor_(C_BORDER)
    box.setBorderWidth_(1.0)
    box.setCornerRadius_(radius)
    parent.addSubview_(box)
    return box

def btn(parent, x, y, w, h, title, size=12, enabled=True):
    """Standard rounded button."""
    b = AppKit.NSButton.alloc().initWithFrame_(AppKit.NSMakeRect(x, y, w, h))
    b.setTitle_(title)
    b.setBezelStyle_(AppKit.NSBezelStyleRounded)
    b.setFont_(AppKit.NSFont.systemFontOfSize_(size))
    b.setEnabled_(enabled)
    parent.addSubview_(b)
    return b

def scrolltv(parent, x, y, w, h, placeholder="", size=12, use_mono=False):
    """Dark scrollable text view."""
    sv = AppKit.NSScrollView.alloc().initWithFrame_(AppKit.NSMakeRect(x, y, w, h))
    sv.setBorderType_(AppKit.NSNoBorder)
    sv.setHasVerticalScroller_(True)
    sv.setAutohidesScrollers_(True)
    sv.setDrawsBackground_(False)
    tv = AppKit.NSTextView.alloc().initWithFrame_(AppKit.NSMakeRect(0, 0, w, h))
    tv.setEditable_(False)
    tv.setSelectable_(True)
    tv.setDrawsBackground_(False)
    tv.setFont_(mono(size) if use_mono else AppKit.NSFont.systemFontOfSize_(size))
    tv.setTextColor_(C_WHITE)
    if placeholder:
        tv.setString_(placeholder)
    sv.setDocumentView_(tv)
    parent.addSubview_(sv)
    return sv, tv

def popup(parent, x, y, w, h, items, selected=0, size=11):
    p = AppKit.NSPopUpButton.alloc().initWithFrame_(AppKit.NSMakeRect(x, y, w, h))
    for item in items:
        p.addItemWithTitle_(item)
    try:
        p.selectItemAtIndex_(selected)
    except Exception:
        pass
    p.setFont_(AppKit.NSFont.systemFontOfSize_(size))
    parent.addSubview_(p)
    return p

# ── Window ─────────────────────────────────────────────────────────────────────
def build_window(mics, cfg):
    W, H = 500, 700
    PAD  = 14   # horizontal padding
    IW   = W - PAD * 2  # inner width

    style = (AppKit.NSWindowStyleMaskTitled |
             AppKit.NSWindowStyleMaskClosable |
             AppKit.NSWindowStyleMaskMiniaturizable |
             AppKit.NSWindowStyleMaskResizable)
    win = AppKit.NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
        AppKit.NSMakeRect(0, 0, W, H), style,
        AppKit.NSBackingStoreBuffered, False)
    win.setTitle_("VoicePrompt")
    win.setTitlebarAppearsTransparent_(True)
    win.setTitleVisibility_(AppKit.NSWindowTitleHidden)
    win.setBackgroundColor_(C_BG)
    win.center()
    win.setLevel_(AppKit.NSFloatingWindowLevel)
    win.setMinSize_(AppKit.NSMakeSize(420, 580))
    cv = win.contentView()

    # ── Layout: cursor starts at top, decrements ──────────────────────────────
    # y = distance from BOTTOM of window (AppKit is bottom-up)
    y = H  # start at top

    # ── Header ────────────────────────────────────────────────────────────────
    y -= 10
    y -= 22
    lbl(cv, PAD, y, 200, 22, "⚡  VOICE PROMPT",
        size=15, bold=True, color=C_CYAN, use_mono=True)
    lbl(cv, PAD+200, y+4, IW-200, 14, "v4.1  ·  local whisper",
        size=10, color=C_DIM, use_mono=True,
        align=AppKit.NSTextAlignmentRight)

    y -= 6
    sep(cv, PAD, y, IW)

    # ── Status ────────────────────────────────────────────────────────────────
    y -= 26
    lbl_status = lbl(cv, PAD, y, IW, 20,
                     "INITIALIZING…",
                     size=12, color=C_AMBER, use_mono=True,
                     align=AppKit.NSTextAlignmentCenter)

    y -= 8
    sep(cv, PAD, y, IW)

    # ── Record button + toolbar ───────────────────────────────────────────────
    # Toolbar row
    y -= 36
    btn_copyall = btn(cv, PAD,        y, 92,  28, "COPY ALL",  size=11, enabled=False)
    btn_export  = btn(cv, PAD+96,     y, 78,  28, "EXPORT",    size=11, enabled=False)
    btn_clear   = btn(cv, W-PAD-80,   y, 80,  28, "CLEAR",     size=11)

    # Record button — centered, bigger
    y -= 48
    btn_rec = btn(cv, W//2-100, y, 200, 40,
                  "⏺   START RECORDING", size=14, enabled=False)

    y -= 10
    sep(cv, PAD, y, IW)

    # ── Spectrum meter (center-up bars, like audio visualizer) ───────────────
    METER_H = 56
    y -= METER_H + 10
    meter_y = y  # bottom of meter zone
    NBARS, BW, GAP = 48, 6, 2
    total_bar_w = NBARS * (BW + GAP) - GAP
    bx = (W - total_bar_w) // 2
    bars = []
    for i in range(NBARS):
        bar = AppKit.NSBox.alloc().initWithFrame_(
            AppKit.NSMakeRect(bx + i*(BW+GAP), meter_y, BW, METER_H))
        bar.setBoxType_(AppKit.NSBoxCustom)
        bar.setFillColor_(C_BAR_OFF)
        bar.setBorderWidth_(0)
        bar.setCornerRadius_(2)
        cv.addSubview_(bar)
        bars.append(bar)

    y -= 10
    sep(cv, PAD, y, IW)

    # ── Settings row ─────────────────────────────────────────────────────────
    y -= 28
    lbl(cv, PAD,     y+6, 28, 14, "MIC",   size=9, color=C_CYAN2, use_mono=True)
    mic_items = ["Default"] + [name[:24] for _, name in mics]
    sel_mic = 0
    if cfg.get("mic_index") is not None:
        for j, (idx, _) in enumerate(mics):
            if idx == cfg["mic_index"]:
                sel_mic = j + 1
                break
    mic_popup = popup(cv, PAD+30, y, 158, 24, mic_items, selected=sel_mic)

    lbl(cv, PAD+196, y+6, 44, 14, "MODEL",  size=9, color=C_CYAN2, use_mono=True)
    sel_model = WHISPER_MODELS.index(cfg.get("whisper_model", "base")) \
                if cfg.get("whisper_model") in WHISPER_MODELS else 0
    model_popup = popup(cv, PAD+244, y, 102, 24, WHISPER_MODELS, selected=sel_model)

    chk_hotkey = AppKit.NSButton.alloc().initWithFrame_(
        AppKit.NSMakeRect(W-PAD-118, y+2, 118, 20))
    chk_hotkey.setButtonType_(AppKit.NSButtonTypeSwitch)
    chk_hotkey.setTitle_("⌘⇧Space hotkey")
    chk_hotkey.setFont_(AppKit.NSFont.systemFontOfSize_(10))
    chk_hotkey.setState_(1 if cfg.get("hotkey_enabled") else 0)
    cv.addSubview_(chk_hotkey)

    y -= 6
    sep(cv, PAD, y, IW)

    # ── Latest output ─────────────────────────────────────────────────────────
    y -= 18
    lbl(cv, PAD, y, 110, 14, "LATEST OUTPUT",
        size=9, color=C_CYAN, use_mono=True)
    btn_pin         = btn(cv, W-PAD-56,  y-2, 56, 20, "PIN",  size=10, enabled=False)
    btn_copy_latest = btn(cv, W-PAD-116, y-2, 56, 20, "COPY", size=10, enabled=False)

    LATEST_H = 60
    y -= LATEST_H + 4
    card(cv, PAD, y, IW, LATEST_H)
    _, latest_tv = scrolltv(cv, PAD+5, y+3, IW-10, LATEST_H-6,
                            "—", size=13, use_mono=True)
    latest_tv.setTextColor_(C_WHITE)

    # ── Pinned ────────────────────────────────────────────────────────────────
    y -= 20
    lbl(cv, PAD, y, 60, 14, "PINNED",
        size=9, color=C_CYAN, use_mono=True)
    btn_clear_pins = btn(cv, W-PAD-90, y-2, 90, 20, "CLEAR PINS", size=10)
    sep(cv, PAD+56, y+6, IW-56-96)

    PINNED_H = 44
    y -= PINNED_H + 4
    card(cv, PAD, y, IW, PINNED_H)
    _, pinned_tv = scrolltv(cv, PAD+5, y+3, IW-10, PINNED_H-6,
                            "No pinned items.", size=10, use_mono=True)
    pinned_tv.setTextColor_(C_DIM)

    # ── History ───────────────────────────────────────────────────────────────
    y -= 20
    lbl(cv, PAD, y, 65, 14, "HISTORY",
        size=9, color=C_CYAN, use_mono=True)
    sep(cv, PAD+62, y+6, IW-62)

    # History fills the rest of the window down to bottom margin
    BOTTOM_PAD = 10
    hist_h = y - BOTTOM_PAD
    hist_y  = BOTTOM_PAD
    card(cv, PAD, hist_y, IW, hist_h)
    hist_scroll, hist_tv = scrolltv(cv, PAD+5, hist_y+3, IW-10, hist_h-6,
                                    "No history yet.", size=11, use_mono=True)
    hist_tv.setTextColor_(C_WHITE)

    win.makeKeyAndOrderFront_(None)
    return (win, lbl_status, btn_rec, bars,
            latest_tv, btn_copy_latest, btn_pin,
            btn_copyall, btn_export, btn_clear,
            hist_tv, pinned_tv,
            mic_popup, model_popup, chk_hotkey, mics,
            btn_clear_pins)


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
         _mics, self._btn_clear_pins) = build_window(self._mics, self._cfg)

        for target, action in [
            (self._btn_rec,          "onRecord:"),
            (self._btn_copy_latest,  "onCopyLatest:"),
            (self._btn_pin,          "onPin:"),
            (self._btn_copyall,      "onCopyAll:"),
            (self._btn_export,       "onExport:"),
            (self._btn_clear,        "onClear:"),
            (self._btn_clear_pins,   "onClearPins:"),
            (self._mic_popup,        "onMicChange:"),
            (self._model_popup,      "onModelChange:"),
            (self._chk_hotkey,       "onHotkeyToggle:"),
        ]:
            target.setTarget_(self)
            target.setAction_(action)

        self._refresh_history()
        self._refresh_pinned()

        NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
            0.05, self, "tick:", None, True)
        return self

    # ── Button actions ────────────────────────────────────────────────────────
    def onRecord_(self, sender):
        if _app_ref["recording"]:
            _app_ref["stop_evt"].set()
        else:
            threading.Thread(target=_app_ref["do_record"], daemon=True).start()

    def onCopyLatest_(self, sender):
        if self._history:
            copy_to_clipboard(self._history[-1]["text"])
            ui("status", text="COPIED ✓", color="green")
            self._reset_status_after(2)

    def onPin_(self, sender):
        if self._history:
            entry = self._history[-1]
            if not any(p["text"] == entry["text"] for p in self._pinned):
                self._pinned.append(entry)
                save_json(PINNED_FILE, self._pinned)
                self._refresh_pinned()
            ui("status", text="PINNED ✓", color="cyan")
            self._reset_status_after(2)

    def onCopyAll_(self, sender):
        if self._history:
            combined = "\n\n".join(e["text"] for e in self._history)
            copy_to_clipboard(combined)
            ui("status", text=f"ALL {len(self._history)} ENTRIES COPIED ✓", color="green")
            self._reset_status_after(2)

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
        ui("status", text="EXPORTED TO DESKTOP ✓", color="green")
        self._reset_status_after(2)

    def onClear_(self, sender):
        self._history = []
        save_json(HISTORY_FILE, [])
        self._latest_tv.setString_("—")
        self._btn_copy_latest.setEnabled_(False)
        self._btn_pin.setEnabled_(False)
        self._btn_copyall.setEnabled_(False)
        self._btn_export.setEnabled_(False)
        self._hist_tv.setString_("No history yet.")

    def onClearPins_(self, sender):
        self._pinned = []
        save_json(PINNED_FILE, [])
        self._refresh_pinned()
        ui("status", text="PINS CLEARED", color="amber")
        self._reset_status_after(2)

    def onMicChange_(self, sender):
        idx = self._mic_popup.indexOfSelectedItem()
        if idx == 0:
            self._cfg["mic_index"] = None
            _app_ref["recorder"].set_mic(None)
        elif idx - 1 < len(self._mics):
            mic_idx, _ = self._mics[idx - 1]
            self._cfg["mic_index"] = mic_idx
            _app_ref["recorder"].set_mic(mic_idx)
        save_config(self._cfg)

    def onModelChange_(self, sender):
        model = WHISPER_MODELS[self._model_popup.indexOfSelectedItem()]
        self._cfg["whisper_model"] = model
        save_config(self._cfg)
        _app_ref["model"][0] = model
        threading.Thread(
            target=lambda: (
                ui("status", text=f"LOADING {model.upper()}…", color="amber"),
                get_whisper_model(model),
                ui("status", text="READY  ·  CLICK TO RECORD", color="cyan")
            ), daemon=True).start()

    def onHotkeyToggle_(self, sender):
        enabled = bool(self._chk_hotkey.state())
        self._cfg["hotkey_enabled"] = enabled
        save_config(self._cfg)
        _app_ref["hotkey_enabled"][0] = enabled
        if enabled and not _app_ref["hotkey_running"][0]:
            threading.Thread(target=_app_ref["start_hotkey"], daemon=True).start()

    # ── Helpers ───────────────────────────────────────────────────────────────
    @objc.python_method
    def _reset_status_after(self, seconds):
        threading.Thread(
            target=lambda: (time.sleep(seconds),
                            ui("status", text="READY  ·  CLICK TO RECORD", color="cyan")),
            daemon=True).start()

    @objc.python_method
    def _refresh_history(self):
        if not self._history:
            self._hist_tv.setString_("No history yet.")
            return
        lines = [f"[{e.get('ts','')}]  {e['text']}"
                 for e in reversed(self._history[-50:])]
        self._hist_tv.setString_("\n\n".join(lines))
        self._hist_tv.scrollToBeginningOfDocument_(None)

    @objc.python_method
    def _refresh_pinned(self):
        if not self._pinned:
            self._pinned_tv.setString_("No pinned items.")
            self._pinned_tv.setTextColor_(C_DIM)
            return
        lines = [f"[{e.get('ts','')}]  {e['text']}"
                 for e in reversed(self._pinned)]
        self._pinned_tv.setString_("\n\n".join(lines))
        self._pinned_tv.setTextColor_(C_WHITE)

    # ── Timer tick ────────────────────────────────────────────────────────────
    def tick_(self, _):
        # Drain UI queue
        try:
            while True:
                msg = _ui_q.get_nowait()
                k = msg["kind"]
                if k == "status":
                    self._lbl.setStringValue_(msg["text"])
                    c = {"red": C_RED, "green": C_GREEN,
                         "amber": C_AMBER, "cyan": C_CYAN
                         }.get(msg.get("color"), C_WHITE)
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
                    self._lbl.setStringValue_("READY  ·  CLICK TO RECORD")
                    self._lbl.setTextColor_(C_CYAN)
        except queue.Empty:
            pass

        # ── Spectrum meter — center-mirrored bars ─────────────────────────────
        # Bars grow from the vertical center outward, creating a classic
        # spectrum / equalizer effect. Peak bars flash white-cyan.
        n   = len(self._bars)
        mid = n / 2
        lvl = self._level
        noise_seed = time.time()

        for i, bar in enumerate(self._bars):
            # Distance from center (0=edge, 1=center)
            dist_from_center = 1.0 - abs(i - mid) / mid
            # Each bar gets its own threshold — center bars light first
            thresh = 1.0 - dist_from_center
            noise  = random.uniform(-0.06, 0.06)
            active = (lvl + noise) > thresh and lvl > 0.03

            if active:
                # How "hot" is this bar?
                heat = min((lvl + noise - thresh) / 0.5, 1.0)
                if heat < 0.5:
                    # blue → cyan
                    t = heat * 2
                    c = rgba(0.0, 0.35 + 0.50*t, 0.70 + 0.25*t)
                else:
                    # cyan → white
                    t = (heat - 0.5) * 2
                    c = rgba(t*0.85, 0.85 + 0.10*t, 0.95 + 0.05*t)
            else:
                c = C_BAR_OFF

            bar.setFillColor_(c)

        self._level = max(0.0, lvl - 0.04)


# ── Recording state ────────────────────────────────────────────────────────────
_app_ref = {
    "recording":      False,
    "stop_evt":       threading.Event(),
    "do_record":      None,
    "recorder":       None,
    "model":          ["base"],
    "cfg":            None,
    "hotkey_enabled": [False],
    "hotkey_running": [False],
    "start_hotkey":   None,
}

def make_record_fn(cfg, recorder):
    def do_record():
        if _app_ref["recording"]:
            return
        _app_ref["recording"] = True
        _app_ref["stop_evt"].clear()

        ui("status", text="● REC  ·  CLICK STOP WHEN DONE", color="red")
        ui("btn_rec", title="⏹   STOP RECORDING", enabled=True)
        recorder.on_level = lambda v: ui("level", v=v)
        recorder.start()
        _app_ref["stop_evt"].wait()

        wav = recorder.stop()
        recorder.on_level = None
        ui("level", v=0.0)
        ui("btn_rec", title="⏺   START RECORDING", enabled=True)
        ui("status", text="⏳  TRANSCRIBING…", color="amber")
        _app_ref["recording"] = False

        if len(wav) < 5000:
            ui("status", text="TOO SHORT — TRY AGAIN", color="amber")
            time.sleep(1.5)
            ui("status", text="READY  ·  CLICK TO RECORD", color="cyan")
            return

        try:
            model = _app_ref["model"][0]
            text  = transcribe(wav, model)
            if not text:
                ui("status", text="NOTHING HEARD — TRY AGAIN", color="amber")
                time.sleep(2)
                ui("status", text="READY  ·  CLICK TO RECORD", color="cyan")
                return

            copy_to_clipboard(text)
            ui("transcript", text=text)

            if cfg.get("auto_type") and has_accessibility():
                if auto_type(text):
                    ui("status", text="✓  TYPED INTO APP  ·  ⌘V TO PASTE", color="green")
                else:
                    ui("status", text="✓  COPIED  ·  PRESS ⌘V TO PASTE", color="green")
            else:
                ui("status", text="✓  COPIED  ·  PRESS ⌘V TO PASTE", color="green")

            time.sleep(3)
            ui("status", text="READY  ·  CLICK TO RECORD", color="cyan")

        except Exception as e:
            log.error(f"Error: {e}")
            ui("status", text=f"ERROR: {e}", color="red")
            time.sleep(3)
            ui("status", text="READY  ·  CLICK TO RECORD", color="cyan")

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


# ── Entry point ────────────────────────────────────────────────────────────────
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

    threading.Thread(
        target=lambda: (get_whisper_model(cfg["whisper_model"]), ui("ready")),
        daemon=True).start()

    if cfg.get("hotkey_enabled"):
        threading.Thread(target=_app_ref["start_hotkey"], daemon=True).start()

    app = AppKit.NSApplication.sharedApplication()
    app.setActivationPolicy_(AppKit.NSApplicationActivationPolicyAccessory)
    _wc_ref[0] = VPController.alloc().init()
    app.run()

if __name__ == "__main__":
    main()
