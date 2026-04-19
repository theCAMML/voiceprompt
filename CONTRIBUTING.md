# Contributing to VoicePrompt

Thanks for contributing! VoicePrompt is a community-built free alternative to Monologue.

## How to contribute

1. **Fork** the repo on GitHub
2. **Clone your fork** locally
3. **Create a branch**: `git checkout -b feat/your-feature`
4. **Make your changes**
5. **Push to your fork**: `git push origin feat/your-feature`
6. **Open a PR** against `theCAMML/voiceprompt` main branch

## Platform ports

VoicePrompt is macOS-native. Platform ports are welcome:

| Platform | Status | Key dependencies |
|---|---|---|
| macOS | ✅ Built | rumps, pyaudio, pynput, osascript |
| Windows | 🔄 In progress (kenzoid) | pystray, pyautogui, pyaudio, pynput |
| Linux | ⬜ Open | pystray, xdotool or ydotool |

When adding a platform port, please include:
- `voiceprompt_<platform>.py` — platform-specific app
- `install_<platform>.sh` (or `.bat`) — setup script
- Update README.md with platform-specific install instructions

## Code style

- Python 3.10+
- Keep core Whisper transcription logic shared where possible
- Graceful degradation: never crash, always fall back (clipboard if auto-type fails, raw transcript if formatting fails)
- No new cloud dependencies without discussion

## Bug fixes

- Include a description of the bug and what caused it
- Reference any external recommendations (e.g. "fix suggested by Dr. Claude — bypass ffmpeg via numpy array")
