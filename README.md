# VoicePrompt 🎙

**Free, open-source macOS voice dictation — powered by OpenAI Whisper, runs 100% locally.**

Click the button → speak → click stop → transcript appears in the window and copies to clipboard.

No subscription. No cloud. No hotkey required. Private by default.

---

## Features

- **Live voice meter** — animated bars show your mic input in real time
- **One-click recording** — big Start/Stop button, no hotkeys or permissions required to get started
- **Instant transcription** — Whisper `base` model transcribes in under 1 second on Apple Silicon
- **History panel** — every transcript saved with timestamp; click 📋 Copy to re-grab any entry
- **Persistent history** — survives restarts (`~/.voiceprompt/history.json`)
- **Auto-type** — optionally types directly into the focused app (requires Accessibility permission)
- **Clipboard fallback** — if auto-type isn't enabled, text goes to clipboard; press ⌘V to paste
- **Auto-start on login** — LaunchAgent keeps it running in the background
- **Privacy first** — audio never leaves your Mac

---

## Requirements

- macOS 12 (Monterey) or later
- Python 3.10+
- ~200 MB disk (Whisper base model)
- Microphone access

---

## Quick Install

```bash
git clone https://github.com/theCAMML/voiceprompt.git
cd voiceprompt
chmod +x install.sh
./install.sh
```

The installer will:
1. Check for Python 3.10+
2. Create a virtual environment (`.venv/`)
3. Install all dependencies
4. Download the Whisper base model (~140 MB, one-time)
5. Set up `~/.voiceprompt/` with config and history
6. Install and load the LaunchAgent (auto-start on login)

VoicePrompt will launch automatically when the installer finishes.

---

## Usage

1. The VoicePrompt window floats above other apps
2. Click **⏺ Start Recording** and speak
3. Click **⏹ Stop Recording** when done
4. Transcript appears in the **Latest** box and is copied to clipboard
5. Press **⌘V** in any app to paste
6. Every recording is saved in the **History** panel — click **📋 Copy** to re-grab any entry

---

## Configuration

Edit `~/.voiceprompt/config.json`:

```json
{
  "whisper_model": "base",
  "auto_type":     true,
  "sample_rate":   16000,
  "channels":      1,
  "chunk_size":    1024
}
```

### Model options

| Model | Size | Speed | Best for |
|-------|------|-------|----------|
| `base` | ~140 MB | ~0.5s | Most users ✓ |
| `small` | ~244 MB | ~1s | Better accuracy |
| `medium` | ~1.5 GB | ~2s | High accuracy |
| `large-v3` | ~3 GB | ~5s | Maximum accuracy |

Change `whisper_model` in config and restart VoicePrompt.

---

## Auto-Type (Optional)

VoicePrompt can type directly into any app instead of just copying to clipboard.

1. Open **System Settings → Privacy & Security → Accessibility**
2. Click **+** and add the Python binary: `~/Projects/voiceprompt/.venv/bin/python3`
3. Toggle it **ON**

Without this, VoicePrompt uses clipboard + ⌘V (works great, just one extra keypress).

---

## Troubleshooting

**Window doesn't appear**
- Check it's running: `launchctl list | grep voiceprompt`
- Check logs: `~/.voiceprompt/voiceprompt.log`

**Mic not working / no meter movement**
- Grant microphone permission: System Settings → Privacy & Security → Microphone → add Terminal or Python

**Transcription is slow**
- Switch to `base` model in config (fastest, recommended for most use)
- Subsequent recordings are faster after first warm-up

**pyaudio install fails**
```bash
brew install portaudio
.venv/bin/pip install pyaudio
```

**Reload after config change**
```bash
launchctl unload ~/Library/LaunchAgents/com.tars.voiceprompt.plist
launchctl load   ~/Library/LaunchAgents/com.tars.voiceprompt.plist
```

**Manual start (no LaunchAgent)**
```bash
cd ~/Projects/voiceprompt
.venv/bin/python3 voiceprompt.py
```

---

## Privacy

- All audio processing happens on your Mac
- No data is sent anywhere
- No telemetry, no accounts, no cloud
- History stored locally in `~/.voiceprompt/history.json`

---

## Credits

- [OpenAI Whisper](https://github.com/openai/whisper) — local speech recognition
- [PyAudio](https://people.csail.mit.edu/hubert/pyaudio/) — microphone capture
- [PyObjC / AppKit](https://pyobjc.readthedocs.io/) — native macOS UI

---

## License

MIT — free to use, fork, and share.

---

*Built by theCAMML. Inspired by Monologue.*
