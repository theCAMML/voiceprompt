# VoicePrompt 🎙

**Free, open-source macOS voice dictation — powered by OpenAI Whisper, runs 100% locally.**

Click the button → speak → click stop → transcript appears and copies to clipboard automatically.

No subscription. No cloud. No hotkey required (optional). Private by default.

---

## Features

- **Dark HUD UI** — futuristic neon cyan theme with monospace fonts
- **Live spectrum meter** — 48 center-mirrored bars animate with your voice (blue → cyan → white)
- **One-click recording** — prominent Start/Stop button, no setup required
- **Instant transcription** — Whisper `base` transcribes in ~0.5s on Apple Silicon
- **Microphone selector** — pick any input device (AirPods, external mic, built-in)
- **Model switcher** — swap between `base / small / medium / large-v3` from the UI
- **History panel** — every transcript saved with timestamp, persists across restarts
- **Pin transcripts** — pin important entries so they survive Clear; one-click Clear Pins
- **Copy All** — concatenate entire history to clipboard in one click
- **Export** — save full history + pinned to a timestamped `.txt` on your Desktop
- **Optional hotkey** — enable `⌘⇧Space` toggle from the UI (Accessibility not required by default)
- **Auto-type** — optionally types directly into the focused app (requires Accessibility permission)
- **Clipboard fallback** — text always goes to clipboard; press ⌘V to paste
- **Auto-start on login** — LaunchAgent keeps it running silently in the background
- **Privacy first** — audio never leaves your Mac

---

## Requirements

- macOS 12 (Monterey) or later
- Python 3.10+
- ~200 MB disk (Whisper base model)
- Microphone access

---

## Install

```bash
git clone https://github.com/theCAMML/voiceprompt.git
cd voiceprompt
bash install.sh
```

`install.sh` will:
1. Create a Python venv and install dependencies
2. Download the Whisper `base` model
3. Install the LaunchAgent so VoicePrompt starts on login

---

## Usage

VoicePrompt runs as a floating window (always on top).

| Action | How |
|---|---|
| Record | Click **⏺ START RECORDING** |
| Stop | Click **⏹ STOP RECORDING** |
| Paste result | Press **⌘V** anywhere |
| Copy latest again | Click **COPY** next to Latest Output |
| Pin a transcript | Click **PIN** next to Latest Output |
| Clear all pins | Click **CLEAR PINS** in the Pinned section |
| Copy everything | Click **COPY ALL** |
| Export history | Click **EXPORT** → opens `.txt` on Desktop |
| Clear history | Click **CLEAR** |
| Change mic | Use the **MIC** dropdown |
| Change model | Use the **MODEL** dropdown |
| Enable hotkey | Check **⌘⇧Space hotkey** (needs Accessibility) |

---

## Config

Settings are saved automatically to `~/.voiceprompt/config.json`.  
Logs: `~/.voiceprompt/voiceprompt.log`  
History: `~/.voiceprompt/history.json`  
Pinned: `~/.voiceprompt/pinned.json`

---

## Whisper Models

| Model | Speed | Accuracy | RAM |
|---|---|---|---|
| base | ~0.5s ⚡ | Good | ~150MB |
| small | ~1s | Better | ~500MB |
| medium | ~2-3s | Great | ~1.5GB |
| large-v3 | ~5-8s | Best | ~3GB |

`base` is the default and recommended for real-time dictation on Apple Silicon.

---

## Uninstall

```bash
launchctl unload ~/Library/LaunchAgents/com.voiceprompt.app.plist
rm ~/Library/LaunchAgents/com.voiceprompt.app.plist
rm -rf ~/Projects/voiceprompt/.venv
rm -rf ~/.voiceprompt
```

---

## License

MIT — free to use, modify, and share.

---

*Built with [OpenAI Whisper](https://github.com/openai/whisper) + [PyObjC](https://pyobjc.readthedocs.io/)*
