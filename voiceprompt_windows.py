#!/usr/bin/env python3
"""
VoicePrompt v4.1 — Windows port
Dark HUD theme · neon cyan · spectrum meter · full feature set

Replaces macOS-only deps:
  AppKit/objc    → tkinter
  pbcopy         → pyperclip (+ clip fallback)
  osascript      → pyautogui (Ctrl+V paste, opt-in)
  LaunchAgent    → startup folder shortcut (optional)
  Cmd+Shift+Spc  → Ctrl+Shift+Space
"""

import os, sys, json, time, queue, signal, logging
import threading, subprocess, struct, wave, io, math, random
import tkinter as tk
from tkinter import ttk
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
    "whisper_model":   "base",
    "auto_type":       False,   # opt-in: pastes via Ctrl+V after a delay
    "auto_type_delay": 2.0,     # seconds to switch to target window
    "sample_rate":     16000,
    "channels":        1,
    "chunk_size":      1024,
    "hotkey_enabled":  False,
    "clear_on_start":  True,
    "mic_index":       None,
    "wake_word":       "",
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
    try:
        import pyperclip
        pyperclip.copy(text)
    except ImportError:
        subprocess.run("clip", input=text.encode("utf-16-le"), shell=True, check=True)
    log.info("Copied to clipboard.")

def has_accessibility():
    try:
        import pyautogui  # noqa: F401
        return True
    except ImportError:
        return False

def auto_type(text, delay=2.0):
    """Wait `delay` seconds (so user can switch windows), then paste via Ctrl+V."""
    try:
        import pyautogui
        time.sleep(delay)
        pyautogui.hotkey("ctrl", "v")
        return True
    except Exception as e:
        log.warning(f"auto_type failed: {e}")
        return False

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
                rms = math.sqrt(sum(s * s for s in shorts) / len(shorts)) / 32768
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
            wf.setnchannels(self.ch)
            wf.setsampwidth(2)
            wf.setframerate(self.sr)
            wf.writeframes(raw)
        return buf.getvalue()

    def cleanup(self):
        if self._pa:
            self._pa.terminate()
            self._pa = None

def transcribe(wav_bytes, model_name, initial_prompt=""):
    import numpy as np
    buf = io.BytesIO(wav_bytes)
    with wave.open(buf, "rb") as wf:
        frames = wf.readframes(wf.getnframes())
    audio = np.frombuffer(frames, dtype=np.int16).astype(np.float32) / 32768.0

    t0 = time.time()
    kw = dict(language="en", fp16=False)
    if initial_prompt:
        kw["initial_prompt"] = initial_prompt
    result = get_whisper_model(model_name).transcribe(audio, **kw)
    text = result["text"].strip()
    log.info(f"Transcribed in {time.time()-t0:.2f}s: {text!r}")
    return text

# ── Palette ────────────────────────────────────────────────────────────────────
C_BG      = "#0F0F17"
C_CARD    = "#17181F"
C_BORDER  = "#004D57"
C_CYAN    = "#00D9F2"
C_CYAN2   = "#008CA6"
C_WHITE   = "#E0F0FF"
C_DIM     = "#59697A"
C_RED     = "#FF3852"
C_AMBER   = "#FF9E1A"
C_GREEN   = "#14F28C"
C_BAR_OFF = "#1A1E2E"

FONT_MONO    = ("Consolas", 11)
FONT_MONO_SM = ("Consolas", 9)
FONT_MONO_LG = ("Consolas", 13)
FONT_SYS_SM  = ("Segoe UI", 9)

# ── UI message queue ───────────────────────────────────────────────────────────
_ui_q = queue.Queue()

def ui(kind, **kw):
    _ui_q.put({"kind": kind, **kw})

# ── Main window ────────────────────────────────────────────────────────────────
class VoicePromptApp:
    def __init__(self, root: tk.Tk):
        self.root = root
        self._level   = 0.0
        self._history = load_json(HISTORY_FILE, [])
        self._pinned  = load_json(PINNED_FILE, [])
        self._cfg     = load_config()
        self._mics    = list_mics()
        self._tray    = None

        self._setup_window()
        self._build_ui()
        self._refresh_history()
        self._refresh_pinned()
        self._tick()

    # ── Window setup ──────────────────────────────────────────────────────────
    def _setup_window(self):
        self.root.title("VoicePrompt")
        self.root.configure(bg=C_BG)
        self.root.geometry("500x720")
        self.root.minsize(420, 580)
        self.root.resizable(True, True)
        self.root.attributes("-topmost", True)
        self.root.update_idletasks()
        sw = self.root.winfo_screenwidth()
        sh = self.root.winfo_screenheight()
        self.root.geometry(f"500x720+{(sw-500)//2}+{(sh-720)//2}")
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

    # ── UI construction ───────────────────────────────────────────────────────
    def _build_ui(self):
        PAD = 14

        # Header
        hdr = tk.Frame(self.root, bg=C_BG)
        hdr.pack(fill="x", padx=PAD, pady=(12, 4))
        tk.Label(hdr, text="⚡  VOICE PROMPT",
                 font=("Consolas", 14, "bold"), fg=C_CYAN, bg=C_BG).pack(side="left")
        tk.Label(hdr, text="v4.1  ·  local whisper",
                 font=FONT_MONO_SM, fg=C_DIM, bg=C_BG).pack(side="right", pady=4)

        self._sep()

        # Status
        self._lbl_status = tk.Label(self.root, text="INITIALIZING…",
                                    font=FONT_MONO, fg=C_AMBER, bg=C_BG)
        self._lbl_status.pack(pady=(4, 0))

        self._sep()

        # Toolbar
        tb = tk.Frame(self.root, bg=C_BG)
        tb.pack(fill="x", padx=PAD, pady=(6, 2))
        self._btn_copyall = self._btn(tb, "COPY ALL", self._on_copy_all, state="disabled")
        self._btn_copyall.pack(side="left", padx=(0, 4))
        self._btn_export = self._btn(tb, "EXPORT", self._on_export, state="disabled")
        self._btn_export.pack(side="left", padx=(0, 4))
        self._btn_clear = self._btn(tb, "CLEAR", self._on_clear)
        self._btn_clear.pack(side="right")

        # Record button
        rec_frame = tk.Frame(self.root, bg=C_BG)
        rec_frame.pack(fill="x", padx=PAD, pady=(6, 6))
        self._btn_rec = tk.Button(
            rec_frame, text="⏺   START RECORDING",
            font=("Consolas", 13, "bold"),
            fg=C_CYAN, bg=C_CARD,
            activeforeground=C_BG, activebackground=C_CYAN,
            relief="flat", bd=0,
            highlightbackground=C_BORDER, highlightthickness=1,
            padx=20, pady=10,
            state="disabled", command=self._on_record)
        self._btn_rec.pack(fill="x")

        self._sep()

        # Spectrum meter
        self._meter = tk.Canvas(self.root, height=60, bg=C_BG,
                                highlightthickness=0, bd=0)
        self._meter.pack(fill="x", padx=PAD, pady=(6, 6))

        self._sep()

        # Settings row
        sf = tk.Frame(self.root, bg=C_BG)
        sf.pack(fill="x", padx=PAD, pady=(4, 4))

        tk.Label(sf, text="MIC", font=FONT_MONO_SM, fg=C_CYAN2, bg=C_BG).pack(
            side="left", padx=(0, 4))
        mic_names = ["Default"] + [n[:24] for _, n in self._mics]
        self._mic_var = tk.StringVar(value=mic_names[0])
        if self._cfg.get("mic_index") is not None:
            for j, (idx, _) in enumerate(self._mics):
                if idx == self._cfg["mic_index"]:
                    self._mic_var.set(mic_names[j + 1])
                    break
        mic_cb = ttk.Combobox(sf, textvariable=self._mic_var, values=mic_names,
                              state="readonly", width=22, font=FONT_SYS_SM)
        mic_cb.pack(side="left", padx=(0, 8))
        mic_cb.bind("<<ComboboxSelected>>", self._on_mic_change)

        tk.Label(sf, text="MODEL", font=FONT_MONO_SM, fg=C_CYAN2, bg=C_BG).pack(
            side="left", padx=(0, 4))
        self._model_var = tk.StringVar(value=self._cfg.get("whisper_model", "base"))
        model_cb = ttk.Combobox(sf, textvariable=self._model_var, values=WHISPER_MODELS,
                                state="readonly", width=10, font=FONT_SYS_SM)
        model_cb.pack(side="left", padx=(0, 8))
        model_cb.bind("<<ComboboxSelected>>", self._on_model_change)

        self._hotkey_var = tk.BooleanVar(value=bool(self._cfg.get("hotkey_enabled")))
        tk.Checkbutton(sf, text="Ctrl+Shift+Space",
                       variable=self._hotkey_var, font=FONT_SYS_SM,
                       fg=C_WHITE, bg=C_BG, selectcolor=C_CARD,
                       activebackground=C_BG, activeforeground=C_CYAN,
                       command=self._on_hotkey_toggle).pack(side="right")

        self._sep()

        # Latest output
        lat_hdr = tk.Frame(self.root, bg=C_BG)
        lat_hdr.pack(fill="x", padx=PAD, pady=(4, 2))
        tk.Label(lat_hdr, text="LATEST OUTPUT", font=FONT_MONO_SM,
                 fg=C_CYAN, bg=C_BG).pack(side="left")
        self._btn_pin = self._btn(lat_hdr, "PIN", self._on_pin,
                                  state="disabled", small=True)
        self._btn_pin.pack(side="right", padx=(4, 0))
        self._btn_copy_latest = self._btn(lat_hdr, "COPY", self._on_copy_latest,
                                          state="disabled", small=True)
        self._btn_copy_latest.pack(side="right", padx=(4, 0))

        card_lat = tk.Frame(self.root, bg=C_CARD,
                            highlightbackground=C_BORDER, highlightthickness=1,
                            height=65)
        card_lat.pack(fill="x", padx=PAD, pady=(0, 4))
        card_lat.pack_propagate(False)
        self._latest_tv = tk.Text(card_lat, font=FONT_MONO_LG, fg=C_WHITE,
                                  bg=C_CARD, relief="flat", bd=4,
                                  wrap="word", state="disabled", height=3)
        self._latest_tv.pack(fill="both", expand=True)

        # Pinned
        pin_hdr = tk.Frame(self.root, bg=C_BG)
        pin_hdr.pack(fill="x", padx=PAD, pady=(2, 2))
        tk.Label(pin_hdr, text="PINNED", font=FONT_MONO_SM,
                 fg=C_CYAN, bg=C_BG).pack(side="left")
        self._btn_clear_pins = self._btn(pin_hdr, "CLEAR PINS",
                                         self._on_clear_pins, small=True)
        self._btn_clear_pins.pack(side="right")

        card_pin = tk.Frame(self.root, bg=C_CARD,
                            highlightbackground=C_BORDER, highlightthickness=1,
                            height=48)
        card_pin.pack(fill="x", padx=PAD, pady=(0, 4))
        card_pin.pack_propagate(False)
        self._pinned_tv = tk.Text(card_pin, font=FONT_MONO_SM, fg=C_DIM,
                                  bg=C_CARD, relief="flat", bd=4,
                                  wrap="word", state="disabled", height=2)
        self._pinned_tv.pack(fill="both", expand=True)

        # History (fills remaining space)
        hist_hdr = tk.Frame(self.root, bg=C_BG)
        hist_hdr.pack(fill="x", padx=PAD, pady=(2, 2))
        tk.Label(hist_hdr, text="HISTORY", font=FONT_MONO_SM,
                 fg=C_CYAN, bg=C_BG).pack(side="left")

        card_hist = tk.Frame(self.root, bg=C_CARD,
                             highlightbackground=C_BORDER, highlightthickness=1)
        card_hist.pack(fill="both", expand=True, padx=PAD, pady=(0, 10))
        self._hist_tv = tk.Text(card_hist, font=FONT_MONO_SM, fg=C_WHITE,
                                bg=C_CARD, relief="flat", bd=4,
                                wrap="word", state="disabled")
        hist_scroll = tk.Scrollbar(card_hist, command=self._hist_tv.yview,
                                   bg=C_CARD, troughcolor=C_BG)
        self._hist_tv.configure(yscrollcommand=hist_scroll.set)
        hist_scroll.pack(side="right", fill="y")
        self._hist_tv.pack(fill="both", expand=True)

        # Style comboboxes dark
        style = ttk.Style()
        style.theme_use("clam")
        style.configure("TCombobox",
                        fieldbackground=C_CARD, background=C_CARD,
                        foreground=C_WHITE, selectbackground=C_CYAN,
                        selectforeground=C_BG, bordercolor=C_BORDER,
                        arrowcolor=C_CYAN)

    def _sep(self):
        tk.Frame(self.root, bg=C_BORDER, height=1).pack(fill="x", padx=14, pady=2)

    def _btn(self, parent, text, cmd, state="normal", small=False):
        font = FONT_MONO_SM if small else ("Consolas", 10)
        return tk.Button(parent, text=text, font=font,
                         fg=C_CYAN, bg=C_CARD,
                         activeforeground=C_BG, activebackground=C_CYAN,
                         relief="flat", bd=0,
                         highlightbackground=C_BORDER, highlightthickness=1,
                         padx=8, pady=3,
                         state=state, command=cmd)

    # ── Button actions ─────────────────────────────────────────────────────────
    def _on_record(self):
        if _app_ref["recording"]:
            _app_ref["stop_evt"].set()
        else:
            threading.Thread(target=_app_ref["do_record"], daemon=True).start()

    def _on_copy_latest(self):
        if self._history:
            copy_to_clipboard(self._history[-1]["text"])
            ui("status", text="COPIED ✓", color="green")
            self._reset_status_after(2)

    def _on_pin(self):
        if self._history:
            entry = self._history[-1]
            if not any(p["text"] == entry["text"] for p in self._pinned):
                self._pinned.append(entry)
                save_json(PINNED_FILE, self._pinned)
                self._refresh_pinned()
            ui("status", text="PINNED ✓", color="cyan")
            self._reset_status_after(2)

    def _on_copy_all(self):
        if self._history:
            combined = "\n\n".join(e["text"] for e in self._history)
            copy_to_clipboard(combined)
            ui("status", text=f"ALL {len(self._history)} ENTRIES COPIED ✓", color="green")
            self._reset_status_after(2)

    def _on_export(self):
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
        out.write_text("\n".join(lines), encoding="utf-8")
        os.startfile(str(out))
        ui("status", text="EXPORTED TO DESKTOP ✓", color="green")
        self._reset_status_after(2)

    def _on_clear(self):
        self._history = []
        save_json(HISTORY_FILE, [])
        self._set_text(self._latest_tv, "—")
        self._btn_copy_latest.config(state="disabled")
        self._btn_pin.config(state="disabled")
        self._btn_copyall.config(state="disabled")
        self._btn_export.config(state="disabled")
        self._set_text(self._hist_tv, "No history yet.")

    def _on_clear_pins(self):
        self._pinned = []
        save_json(PINNED_FILE, [])
        self._refresh_pinned()
        ui("status", text="PINS CLEARED", color="amber")
        self._reset_status_after(2)

    def _on_mic_change(self, _=None):
        val = self._mic_var.get()
        mic_names = ["Default"] + [n[:24] for _, n in self._mics]
        idx = mic_names.index(val)
        if idx == 0:
            self._cfg["mic_index"] = None
            _app_ref["recorder"].set_mic(None)
        elif idx - 1 < len(self._mics):
            mic_idx, _ = self._mics[idx - 1]
            self._cfg["mic_index"] = mic_idx
            _app_ref["recorder"].set_mic(mic_idx)
        save_config(self._cfg)

    def _on_model_change(self, _=None):
        model = self._model_var.get()
        self._cfg["whisper_model"] = model
        save_config(self._cfg)
        _app_ref["model"][0] = model
        threading.Thread(
            target=lambda: (
                ui("status", text=f"LOADING {model.upper()}…", color="amber"),
                get_whisper_model(model),
                ui("status", text="READY  ·  CLICK TO RECORD", color="cyan"),
            ), daemon=True).start()

    def _on_hotkey_toggle(self):
        enabled = self._hotkey_var.get()
        self._cfg["hotkey_enabled"] = enabled
        save_config(self._cfg)
        _app_ref["hotkey_enabled"][0] = enabled
        if enabled and not _app_ref["hotkey_running"][0]:
            threading.Thread(target=_app_ref["start_hotkey"], daemon=True).start()

    def _on_close(self):
        if self._tray:
            self.root.withdraw()
        else:
            self._quit()

    def _quit(self):
        if self._tray:
            self._tray.stop()
        self.root.destroy()
        sys.exit(0)

    # ── Helpers ───────────────────────────────────────────────────────────────
    def _reset_status_after(self, seconds):
        threading.Thread(
            target=lambda: (
                time.sleep(seconds),
                ui("status", text="READY  ·  CLICK TO RECORD", color="cyan"),
            ), daemon=True).start()

    def _set_text(self, widget, text):
        widget.config(state="normal")
        widget.delete("1.0", "end")
        widget.insert("1.0", text)
        widget.config(state="disabled")

    def _refresh_history(self):
        if not self._history:
            self._set_text(self._hist_tv, "No history yet.")
            return
        lines = [f"[{e.get('ts','')}]  {e['text']}"
                 for e in reversed(self._history[-50:])]
        self._set_text(self._hist_tv, "\n\n".join(lines))

    def _refresh_pinned(self):
        if not self._pinned:
            self._set_text(self._pinned_tv, "No pinned items.")
            self._pinned_tv.config(fg=C_DIM)
            return
        lines = [f"[{e.get('ts','')}]  {e['text']}"
                 for e in reversed(self._pinned)]
        self._set_text(self._pinned_tv, "\n\n".join(lines))
        self._pinned_tv.config(fg=C_WHITE)

    # ── Spectrum meter (canvas rectangles, center-mirrored) ───────────────────
    def _draw_meter(self):
        canvas = self._meter
        w = canvas.winfo_width()
        h = canvas.winfo_height()
        if w <= 1:
            return
        canvas.delete("all")

        NBARS = 48
        GAP   = 2
        bw    = max(4, (w - GAP * (NBARS - 1)) // NBARS)
        total = NBARS * (bw + GAP) - GAP
        ox    = (w - total) // 2
        mid   = NBARS / 2
        lvl   = self._level

        for i in range(NBARS):
            dist   = 1.0 - abs(i - mid) / mid
            thresh = 1.0 - dist
            noise  = random.uniform(-0.06, 0.06)
            active = (lvl + noise) > thresh and lvl > 0.03

            if active:
                heat  = min((lvl + noise - thresh) / 0.5, 1.0)
                bar_h = max(4, int(h * (0.3 + heat * 0.7)))
                if heat < 0.5:
                    t = heat * 2
                    r, g, b = 0, int((0.35 + 0.50*t)*255), int((0.70 + 0.25*t)*255)
                else:
                    t = (heat - 0.5) * 2
                    r, g, b = int(t*0.85*255), int((0.85+0.10*t)*255), int((0.95+0.05*t)*255)
                color = f"#{r:02x}{g:02x}{b:02x}"
            else:
                bar_h = 4
                color = C_BAR_OFF

            x1 = ox + i * (bw + GAP)
            y1 = (h - bar_h) // 2
            canvas.create_rectangle(x1, y1, x1 + bw, y1 + bar_h, fill=color, outline="")

        self._level = max(0.0, lvl - 0.04)

    # ── Tick: drain UI queue + redraw meter ───────────────────────────────────
    def _tick(self):
        try:
            while True:
                msg = _ui_q.get_nowait()
                k   = msg["kind"]
                if k == "status":
                    self._lbl_status.config(text=msg["text"])
                    c = {"red": C_RED, "green": C_GREEN,
                         "amber": C_AMBER, "cyan": C_CYAN}.get(msg.get("color"), C_WHITE)
                    self._lbl_status.config(fg=c)
                elif k == "transcript":
                    text = msg["text"]
                    ts   = datetime.now().strftime("%H:%M:%S")
                    self._set_text(self._latest_tv, text)
                    self._btn_copy_latest.config(state="normal")
                    self._btn_pin.config(state="normal")
                    self._btn_copyall.config(state="normal")
                    self._btn_export.config(state="normal")
                    self._history.append({"ts": ts, "text": text})
                    save_json(HISTORY_FILE, self._history[-MAX_HISTORY:])
                    self._refresh_history()
                elif k == "level":
                    self._level = msg["v"]
                elif k == "btn_rec":
                    self._btn_rec.config(text=msg["title"])
                    if "enabled" in msg:
                        self._btn_rec.config(state="normal" if msg["enabled"] else "disabled")
                elif k == "ready":
                    self._btn_rec.config(state="normal")
                    self._lbl_status.config(text="READY  ·  CLICK TO RECORD", fg=C_CYAN)
        except queue.Empty:
            pass

        self._draw_meter()
        self.root.after(50, self._tick)

    def set_tray(self, tray):
        self._tray = tray


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
                delay = cfg.get("auto_type_delay", 2.0)
                ui("status",
                   text=f"⌨  TYPING IN {delay:.0f}s — SWITCH WINDOW NOW…",
                   color="amber")
                if auto_type(text, delay=delay):
                    ui("status", text="✓  PASTED  ·  CTRL+V ALSO WORKS", color="green")
                else:
                    ui("status", text="✓  COPIED  ·  PRESS CTRL+V TO PASTE", color="green")
            else:
                ui("status", text="✓  COPIED  ·  PRESS CTRL+V TO PASTE", color="green")

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
            pressed_keys = set()
            triggered = [False]

            def on_press(key):
                pressed_keys.add(key)
                ctrl  = keyboard.Key.ctrl_l  in pressed_keys or \
                        keyboard.Key.ctrl_r  in pressed_keys
                shift = keyboard.Key.shift   in pressed_keys or \
                        keyboard.Key.shift_r in pressed_keys
                space = keyboard.Key.space   in pressed_keys
                if ctrl and shift and space:
                    if not triggered[0] and not _app_ref["recording"]:
                        triggered[0] = True
                        threading.Thread(target=_app_ref["do_record"],
                                         daemon=True).start()

            def on_release(key):
                pressed_keys.discard(key)
                if key == keyboard.Key.space and triggered[0]:
                    triggered[0] = False
                    if _app_ref["recording"]:
                        _app_ref["stop_evt"].set()

            with keyboard.Listener(on_press=on_press, on_release=on_release) as listener:
                log.info("Hotkey listener active (Ctrl+Shift+Space).")
                listener.join()
        except Exception as e:
            log.warning(f"Hotkey unavailable: {e}")
        _app_ref["hotkey_running"][0] = False

    return start_hotkey


# ── System tray (optional — needs pystray + Pillow) ────────────────────────────
def _make_tray_icon(app: VoicePromptApp):
    try:
        from PIL import Image, ImageDraw
        import pystray

        img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
        d   = ImageDraw.Draw(img)
        d.ellipse([18, 4, 46, 36], fill="#00D9F2")   # mic capsule
        d.rectangle([26, 36, 38, 50], fill="#00D9F2") # mic stem
        d.rectangle([16, 50, 48, 54], fill="#00D9F2") # mic base

        def show(icon, item):
            app.root.after(0, app.root.deiconify)
            app.root.after(0, lambda: app.root.attributes("-topmost", True))

        def quit_(icon, item):
            icon.stop()
            app.root.after(0, app._quit)

        tray = pystray.Icon(
            "VoicePrompt", img, "VoicePrompt",
            menu=pystray.Menu(
                pystray.MenuItem("Show", show, default=True),
                pystray.MenuItem("Quit", quit_),
            ))
        app.set_tray(tray)
        threading.Thread(target=tray.run, daemon=True).start()
        log.info("System tray icon active.")
    except ImportError:
        log.info("pystray/Pillow not found — no tray icon (close button exits).")


# ── Auto-start helpers ─────────────────────────────────────────────────────────
def install_startup(script_path: Path, python_path: Path):
    """Add VoicePrompt to Windows startup via the startup folder shortcut."""
    try:
        import winreg
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                             r"Software\Microsoft\Windows\CurrentVersion\Run",
                             0, winreg.KEY_SET_VALUE)
        cmd = f'"{python_path}" "{script_path}"'
        winreg.SetValueEx(key, "VoicePrompt", 0, winreg.REG_SZ, cmd)
        winreg.CloseKey(key)
        log.info("Added to Windows startup (registry).")
    except Exception as e:
        log.warning(f"Could not add to startup: {e}")

def remove_startup():
    try:
        import winreg
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                             r"Software\Microsoft\Windows\CurrentVersion\Run",
                             0, winreg.KEY_SET_VALUE)
        winreg.DeleteValue(key, "VoicePrompt")
        winreg.CloseKey(key)
        log.info("Removed from Windows startup.")
    except FileNotFoundError:
        pass
    except Exception as e:
        log.warning(f"Could not remove from startup: {e}")


# ── Entry point ────────────────────────────────────────────────────────────────
def main():
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))

    cfg      = load_config()
    recorder = AudioRecorder(cfg["sample_rate"], cfg["channels"],
                             cfg["chunk_size"], cfg.get("mic_index"))

    _app_ref["do_record"]         = make_record_fn(cfg, recorder)
    _app_ref["stop_evt"]          = threading.Event()
    _app_ref["recorder"]          = recorder
    _app_ref["model"]             = [cfg["whisper_model"]]
    _app_ref["cfg"]               = cfg
    _app_ref["start_hotkey"]      = make_hotkey_fn()
    _app_ref["hotkey_enabled"][0] = cfg.get("hotkey_enabled", False)

    root = tk.Tk()
    app  = VoicePromptApp(root)

    _make_tray_icon(app)

    threading.Thread(
        target=lambda: (get_whisper_model(cfg["whisper_model"]), ui("ready")),
        daemon=True).start()

    if cfg.get("hotkey_enabled"):
        threading.Thread(target=_app_ref["start_hotkey"], daemon=True).start()

    root.mainloop()


if __name__ == "__main__":
    main()
