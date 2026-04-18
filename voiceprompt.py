#!/usr/bin/env python3
"""
VoicePrompt v4.0 — Futuristic macOS voice dictation
- Dark HUD-style UI with neon cyan accents
- Glowing animated record button
- Spectrum meter with gradient bars
- Mic selector, model switcher, hotkey toggle
- History panel with pin, copy-all, export
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

# ── Color palette ──────────────────────────────────────────────────────────────
def rgb(r, g, b, a=1.0):
    return AppKit.NSColor.colorWithRed_green_blue_alpha_(r, g, b, a)

# HUD color constants (defined as callables to avoid pre-init issues)
def col_bg():          return rgb(0.07, 0.07, 0.10)          # near-black
def col_card():        return rgb(0.10, 0.10, 0.15)          # dark card
def col_border():      return rgb(0.00, 0.80, 0.90, 0.35)    # cyan border dim
def col_cyan():        return rgb(0.00, 0.85, 0.95)          # neon cyan
def col_cyan_dim():    return rgb(0.00, 0.55, 0.65)          # muted cyan
def col_text():        return rgb(0.85, 0.92, 0.98)          # near-white
def col_text_dim():    return rgb(0.40, 0.50, 0.60)          # muted
def col_red():         return rgb(1.00, 0.25, 0.35)          # neon red
def col_orange():      return rgb(1.00, 0.60, 0.10)          # amber
def col_green():       return rgb(0.10, 0.95, 0.55)          # neon green
def col_bar_off():     return rgb(0.12, 0.14, 0.20)          # inactive bar
def col_bar_low():     return rgb(0.00, 0.40, 0.70)          # low level
def col_bar_mid():     return rgb(0.00, 0.75, 0.95)          # mid level (cyan)
def col_bar_peak():    return rgb(0.80, 0.95, 1.00)          # peak (white-cyan)

# ── Themed helpers ─────────────────────────────────────────────────────────────
def mono(size=12):
    return AppKit.NSFont.fontWithName_size_("SF Mono", size) or \
           AppKit.NSFont.fontWithName_size_("Menlo", size) or \
           AppKit.NSFont.monospacedSystemFontOfSize_weight_(size, AppKit.NSFontWeightRegular)

def make_label(parent, frame, text, size=13, bold=False, color=None,
               align=AppKit.NSTextAlignmentLeft, mono_font=False):
    lbl = AppKit.NSTextField.alloc().initWithFrame_(frame)
    lbl.setStringValue_(text)
    if mono_font:
        lbl.setFont_(mono(size))
    elif bold:
        lbl.setFont_(AppKit.NSFont.boldSystemFontOfSize_(size))
    else:
        lbl.setFont_(AppKit.NSFont.systemFontOfSize_(size))
    lbl.setTextColor_(color if color else col_text())
    lbl.setAlignment_(align)
    lbl.setBezeled_(False)
    lbl.setDrawsBackground_(False)
    lbl.setEditable_(False)
    lbl.setSelectable_(False)
    parent.addSubview_(lbl)
    return lbl

def make_scroll_textview(parent, frame, placeholder="", font_size=12,
                         use_mono=False, bg=None, text_color=None):
    scroll = AppKit.NSScrollView.alloc().initWithFrame_(frame)
    scroll.setBorderType_(AppKit.NSNoBorder)
    scroll.setHasVerticalScroller_(True)
    scroll.setAutohidesScrollers_(True)
    scroll.setDrawsBackground_(False)
    tv = AppKit.NSTextView.alloc().initWithFrame_(
        AppKit.NSMakeRect(0, 0, frame.size.width, frame.size.height))
    tv.setEditable_(False)
    tv.setSelectable_(True)
    tv.setFont_(mono(font_size) if use_mono else AppKit.NSFont.systemFontOfSize_(font_size))
    tv.setTextColor_(text_color if text_color else col_text())
    if bg:
        tv.setBackgroundColor_(bg)
    else:
        tv.setDrawsBackground_(False)
    if placeholder:
        tv.setString_(placeholder)
    scroll.setDocumentView_(tv)
    parent.addSubview_(scroll)
    return scroll, tv

def make_card(parent, frame, corner=10):
    """Dark rounded card with subtle cyan border."""
    box = AppKit.NSBox.alloc().initWithFrame_(frame)
    box.setBoxType_(AppKit.NSBoxCustom)
    box.setFillColor_(col_card())
    box.setBorderColor_(col_border())
    box.setBorderWidth_(1.0)
    box.setCornerRadius_(corner)
    parent.addSubview_(box)
    return box

def make_hud_button(parent, frame, title, font_size=12, enabled=True, accent=False):
    """Pill-shaped HUD-style button."""
    btn = AppKit.NSButton.alloc().initWithFrame_(frame)
    btn.setTitle_(title)
    btn.setBezelStyle_(AppKit.NSBezelStyleRounded)
    if accent:
        btn.setFont_(AppKit.NSFont.boldSystemFontOfSize_(font_size))
    else:
        btn.setFont_(AppKit.NSFont.systemFontOfSize_(font_size))
    btn.setEnabled_(enabled)
    parent.addSubview_(btn)
    return btn

def make_separator(parent, frame):
    """Thin cyan separator line."""
    sep = AppKit.NSBox.alloc().initWithFrame_(frame)
    sep.setBoxType_(AppKit.NSBoxCustom)
    sep.setFillColor_(col_border())
    sep.setBorderWidth_(0)
    sep.setCornerRadius_(0)
    parent.addSubview_(sep)
    return sep

# ── Window builder ─────────────────────────────────────────────────────────────
def build_window(mics, cfg):
    W, H = 540, 720
    style = (AppKit.NSWindowStyleMaskTitled |
             AppKit.NSWindowStyleMaskClosable |
             AppKit.NSWindowStyleMaskMiniaturizable |
             AppKit.NSWindowStyleMaskResizable)
    win = AppKit.NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
        AppKit.NSMakeRect(0, 0, W, H), style,
        AppKit.NSBackingStoreBuffered, False)
    win.setTitle_("VoicePrompt")
    win.center()
    win.setLevel_(AppKit.NSFloatingWindowLevel)
    win.setMinSize_(AppKit.NSMakeSize(440, 600))

    # Dark window background
    win.setBackgroundColor_(col_bg())
    win.setTitlebarAppearsTransparent_(True)
    win.setTitleVisibility_(AppKit.NSWindowTitleHidden)

    cv = win.contentView()
    PAD = 16

    # ── Title bar area ────────────────────────────────────────────────────────
    # Logo / title
    title_lbl = make_label(cv, AppKit.NSMakeRect(PAD+4, H-42, 220, 30),
                           "⚡ VOICE PROMPT", size=15, bold=True,
                           color=col_cyan(), mono_font=True)
    # Version tag
    make_label(cv, AppKit.NSMakeRect(PAD+4, H-58, 120, 16),
               "v4.0  ·  Whisper", size=10,
               color=col_text_dim(), mono_font=True)

    # Thin separator under title
    make_separator(cv, AppKit.NSMakeRect(PAD, H-64, W-PAD*2, 1))

    # ── Status line ───────────────────────────────────────────────────────────
    lbl_status = make_label(cv, AppKit.NSMakeRect(PAD, H-94, W-PAD*2, 22),
                            "INITIALIZING — loading Whisper…", size=12,
                            color=col_orange(), mono_font=True,
                            align=AppKit.NSTextAlignmentCenter)

    # ── Record button card ────────────────────────────────────────────────────
    rec_card = make_card(cv, AppKit.NSMakeRect(PAD, H-158, W-PAD*2, 58), corner=12)
    btn_rec = make_hud_button(rec_card.contentView() if hasattr(rec_card, 'contentView') else cv,
                              AppKit.NSMakeRect(W//2-100, H-152, 200, 46),
                              "⏺   START RECORDING", font_size=14, enabled=False, accent=True)

    # Toolbar: Copy All | Export | Clear (left/right of rec card)
    btn_copyall = make_hud_button(cv, AppKit.NSMakeRect(PAD,     H-152, 100, 32),
                                  "COPY ALL", font_size=11, enabled=False)
    btn_export  = make_hud_button(cv, AppKit.NSMakeRect(PAD+106, H-152, 80,  32),
                                  "EXPORT",   font_size=11, enabled=False)
    btn_clear   = make_hud_button(cv, AppKit.NSMakeRect(W-PAD-88,H-152, 88,  32),
                                  "CLEAR",    font_size=11)

    # ── Spectrum meter ────────────────────────────────────────────────────────
    make_separator(cv, AppKit.NSMakeRect(PAD, H-168, W-PAD*2, 1))
    bars = []
    n, bw, gap = 44, 7, 2
    sx = (W - (n*(bw+gap)-gap)) // 2
    meter_y = H - 240
    for i in range(n):
        bar = AppKit.NSBox.alloc().initWithFrame_(
            AppKit.NSMakeRect(sx+i*(bw+gap), meter_y, bw, 62))
        bar.setBoxType_(AppKit.NSBoxCustom)
        bar.setFillColor_(col_bar_off())
        bar.setBorderWidth_(0)
        bar.setCornerRadius_(2)
        cv.addSubview_(bar)
        bars.append(bar)
    make_separator(cv, AppKit.NSMakeRect(PAD, meter_y-8, W-PAD*2, 1))

    # ── Settings row ─────────────────────────────────────────────────────────
    settings_y = meter_y - 38
    make_label(cv, AppKit.NSMakeRect(PAD, settings_y+4, 28, 16),
               "MIC", size=9, color=col_cyan_dim(), mono_font=True)
    mic_popup = AppKit.NSPopUpButton.alloc().initWithFrame_(
        AppKit.NSMakeRect(PAD+30, settings_y, 168, 24))
    mic_popup.addItemWithTitle_("Default Input")
    for idx, name in mics:
        mic_popup.addItemWithTitle_(name[:26])
    mic_popup.setFont_(AppKit.NSFont.systemFontOfSize_(11))
    cv.addSubview_(mic_popup)

    make_label(cv, AppKit.NSMakeRect(PAD+210, settings_y+4, 44, 16),
               "MODEL", size=9, color=col_cyan_dim(), mono_font=True)
    model_popup = AppKit.NSPopUpButton.alloc().initWithFrame_(
        AppKit.NSMakeRect(PAD+258, settings_y, 106, 24))
    for m in WHISPER_MODELS:
        model_popup.addItemWithTitle_(m)
    try:
        model_popup.selectItemAtIndex_(WHISPER_MODELS.index(cfg["whisper_model"]))
    except Exception:
        pass
    model_popup.setFont_(AppKit.NSFont.systemFontOfSize_(11))
    cv.addSubview_(model_popup)

    chk_hotkey = AppKit.NSButton.alloc().initWithFrame_(
        AppKit.NSMakeRect(W-PAD-120, settings_y+2, 120, 20))
    chk_hotkey.setButtonType_(AppKit.NSButtonTypeSwitch)
    chk_hotkey.setTitle_("Hotkey  ⌘⇧Space")
    chk_hotkey.setFont_(AppKit.NSFont.systemFontOfSize_(10))
    chk_hotkey.setState_(1 if cfg.get("hotkey_enabled") else 0)
    cv.addSubview_(chk_hotkey)

    make_separator(cv, AppKit.NSMakeRect(PAD, settings_y-12, W-PAD*2, 1))

    # ── Latest output ─────────────────────────────────────────────────────────
    latest_y = settings_y - 22
    make_label(cv, AppKit.NSMakeRect(PAD, latest_y, 80, 14),
               "LATEST OUTPUT", size=9, color=col_cyan(), mono_font=True)
    btn_copy_latest = make_hud_button(cv, AppKit.NSMakeRect(W-PAD-140, latest_y-2, 66, 20),
                                      "COPY", font_size=10, enabled=False)
    btn_pin = make_hud_button(cv, AppKit.NSMakeRect(W-PAD-68, latest_y-2, 52, 20),
                              "PIN", font_size=10, enabled=False)

    latest_tv_y = latest_y - 72
    latest_card = make_card(cv, AppKit.NSMakeRect(PAD, latest_tv_y, W-PAD*2, 68), corner=8)
    _, latest_tv = make_scroll_textview(
        cv, AppKit.NSMakeRect(PAD+6, latest_tv_y+4, W-PAD*2-12, 60),
        "—", font_size=13, use_mono=True,
        bg=col_card(), text_color=col_text())

    # ── Pinned ────────────────────────────────────────────────────────────────
    pinned_label_y = latest_tv_y - 24
    make_label(cv, AppKit.NSMakeRect(PAD, pinned_label_y, 80, 14),
               "PINNED", size=9, color=col_cyan(), mono_font=True)
    make_separator(cv, AppKit.NSMakeRect(PAD+58, pinned_label_y+6, W-PAD*2-58, 1))

    pinned_y = pinned_label_y - 50
    pinned_card = make_card(cv, AppKit.NSMakeRect(PAD, pinned_y, W-PAD*2, 46), corner=8)
    _, pinned_tv = make_scroll_textview(
        cv, AppKit.NSMakeRect(PAD+6, pinned_y+4, W-PAD*2-12, 38),
        "No pinned items.", font_size=10, use_mono=True,
        bg=col_card(), text_color=col_text_dim())

    # ── History ───────────────────────────────────────────────────────────────
    hist_label_y = pinned_y - 24
    make_label(cv, AppKit.NSMakeRect(PAD, hist_label_y, 80, 14),
               "HISTORY", size=9, color=col_cyan(), mono_font=True)
    make_separator(cv, AppKit.NSMakeRect(PAD+62, hist_label_y+6, W-PAD*2-62, 1))

    hist_y = 12
    hist_h = hist_label_y - 12 - 8
    hist_card = make_card(cv, AppKit.NSMakeRect(PAD, hist_y, W-PAD*2, hist_h), corner=8)
    hist_scroll, hist_tv = make_scroll_textview(
        cv, AppKit.NSMakeRect(PAD+6, hist_y+4, W-PAD*2-12, hist_h-8),
        "No history yet.", font_size=11, use_mono=True,
        bg=col_card(), text_color=col_text())

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
    @objc.python_method
    def _flash_status(self, text, delay=2):
        threading.Thread(target=lambda: (
            time.sleep(delay),
            ui("status", text="READY  ·  CLICK TO RECORD", color="cyan")
        ), daemon=True).start()

    def onRecord_(self, sender):
        if _app_ref["recording"]:
            _app_ref["stop_evt"].set()
        else:
            threading.Thread(target=_app_ref["do_record"], daemon=True).start()

    def onCopyLatest_(self, sender):
        if self._history:
            copy_to_clipboard(self._history[-1]["text"])
            ui("status", text="COPIED TO CLIPBOARD", color="green")
            self._flash_status("READY  ·  CLICK TO RECORD")

    def onPin_(self, sender):
        if self._history:
            entry = self._history[-1]
            if not any(p["text"] == entry["text"] for p in self._pinned):
                self._pinned.append(entry)
                save_json(PINNED_FILE, self._pinned)
                self._refresh_pinned()
                ui("status", text="PINNED ✓", color="cyan")
                self._flash_status("READY  ·  CLICK TO RECORD")

    def onCopyAll_(self, sender):
        if self._history:
            combined = "\n\n".join(e["text"] for e in self._history)
            copy_to_clipboard(combined)
            ui("status", text=f"ALL {len(self._history)} ENTRIES COPIED", color="green")
            self._flash_status("READY  ·  CLICK TO RECORD")

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
        self._flash_status("READY  ·  CLICK TO RECORD")

    def onClear_(self, sender):
        self._history = []
        save_json(HISTORY_FILE, [])
        self._latest_tv.setString_("—")
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
        threading.Thread(
            target=lambda: (
                ui("status", text=f"LOADING {model.upper()}…", color="orange"),
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
        log.info(f"Hotkey {'enabled' if enabled else 'disabled'}")

    # ── Refresh helpers ───────────────────────────────────────────────────────
    @objc.python_method
    def _refresh_history(self):
        if not self._history:
            self._hist_tv.setString_("No history yet.")
            return
        lines = []
        for e in reversed(self._history[-50:]):
            lines.append(f"[{e.get('ts','')}]  {e['text']}")
        self._hist_tv.setString_("\n\n".join(lines))
        self._hist_tv.scrollToBeginningOfDocument_(None)

    @objc.python_method
    def _refresh_pinned(self):
        if not self._pinned:
            self._pinned_tv.setString_("No pinned items.")
            return
        lines = [f"[{e.get('ts','')}]  {e['text']}" for e in reversed(self._pinned)]
        self._pinned_tv.setString_("\n\n".join(lines))

    # ── Timer tick ────────────────────────────────────────────────────────────
    def tick_(self, _):
        try:
            while True:
                msg = _ui_q.get_nowait()
                k = msg["kind"]
                if k == "status":
                    self._lbl.setStringValue_(msg["text"])
                    c = {
                        "red":    col_red(),
                        "green":  col_green(),
                        "orange": col_orange(),
                        "cyan":   col_cyan(),
                    }.get(msg.get("color"), col_text())
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
                    self._lbl.setTextColor_(col_cyan())
        except queue.Empty:
            pass

        # Spectrum meter animation
        n = len(self._bars)
        for i, bar in enumerate(self._bars):
            pos    = i / n                          # 0..1 position
            thresh = pos                            # bars fill from left
            noise  = random.uniform(-0.04, 0.04)
            lvl    = self._level + noise
            active = lvl > thresh and self._level > 0.02

            if active:
                # gradient: blue → cyan → white at peak
                t = min(lvl * 1.5, 1.0)
                if t < 0.5:
                    c = rgb(0.0, 0.40 + 0.35*t*2, 0.70 + 0.25*t*2)  # blue→cyan
                else:
                    tt = (t - 0.5) * 2
                    c  = rgb(tt*0.80, 0.75 + 0.20*tt, 0.95 + 0.05*tt)  # cyan→white
            else:
                c = col_bar_off()
            bar.setFillColor_(c)

        self._level = max(0.0, self._level - 0.035)


# ── Recording logic ────────────────────────────────────────────────────────────
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
        ui("status", text="⏳  TRANSCRIBING…", color="orange")
        _app_ref["recording"] = False

        if len(wav) < 5000:
            ui("status", text="TOO SHORT — TRY AGAIN", color="orange")
            time.sleep(1.5)
            ui("status", text="READY  ·  CLICK TO RECORD", color="cyan")
            return

        try:
            model = _app_ref["model"][0]
            text  = transcribe(wav, model)
            if not text:
                ui("status", text="NOTHING HEARD — TRY AGAIN", color="orange")
                time.sleep(2)
                ui("status", text="READY  ·  CLICK TO RECORD", color="cyan")
                return

            copy_to_clipboard(text)
            ui("transcript", text=text)

            if cfg.get("auto_type") and has_accessibility():
                if auto_type(text):
                    ui("status", text="✓  TYPED INTO APP  ·  HISTORY SAVED", color="green")
                else:
                    ui("status", text="✓  COPIED  ·  PRESS ⌘V  ·  HISTORY SAVED", color="green")
            else:
                ui("status", text="✓  COPIED  ·  PRESS ⌘V  ·  HISTORY SAVED", color="green")

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
