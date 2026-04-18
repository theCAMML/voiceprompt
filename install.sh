#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# VoicePrompt — Install Script
# Sets up virtualenv, installs dependencies, downloads Whisper, and
# installs the LaunchAgent for auto-start on login.
# ─────────────────────────────────────────────────────────────────────────────
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VP_DIR="$HOME/.voiceprompt"
LAUNCH_AGENTS="$HOME/Library/LaunchAgents"
PLIST_NAME="com.tars.voiceprompt.plist"
PLIST_DST="$LAUNCH_AGENTS/$PLIST_NAME"

RED='\033[0;31m'; GRN='\033[0;32m'; YLW='\033[1;33m'
BLU='\033[0;34m'; NC='\033[0m'

banner() { echo -e "\n${BLU}▶ $*${NC}"; }
ok()     { echo -e "  ${GRN}✓${NC} $*"; }
warn()   { echo -e "  ${YLW}⚠${NC}  $*"; }
err()    { echo -e "  ${RED}✗${NC} $*"; exit 1; }

echo ""
echo -e "${BLU}╔══════════════════════════════════════════╗${NC}"
echo -e "${BLU}║   VoicePrompt Installer v2.0             ║${NC}"
echo -e "${BLU}║   Free macOS Voice Dictation             ║${NC}"
echo -e "${BLU}╚══════════════════════════════════════════╝${NC}"
echo ""

# ── 1. Python check ────────────────────────────────────────────────────────────
banner "Checking Python…"
PYTHON=""
for candidate in python3.12 python3.11 python3.10 python3; do
    if command -v "$candidate" &>/dev/null; then
        VER=$("$candidate" --version 2>&1 | grep -oE '[0-9]+\.[0-9]+')
        MAJOR=$(echo "$VER" | cut -d. -f1)
        MINOR=$(echo "$VER" | cut -d. -f2)
        if [[ "$MAJOR" -ge 3 && "$MINOR" -ge 10 ]]; then
            PYTHON="$candidate"
            ok "Found $candidate ($VER)"
            break
        fi
    fi
done
[[ -z "$PYTHON" ]] && err "Python 3.10+ not found. Install from https://www.python.org/downloads/"

# ── 2. Virtual environment ─────────────────────────────────────────────────────
banner "Setting up virtual environment…"
VENV_DIR="$REPO_DIR/.venv"
if [[ ! -d "$VENV_DIR" ]]; then
    "$PYTHON" -m venv "$VENV_DIR"
    ok "Created .venv"
else
    ok ".venv already exists"
fi
VENV_PY="$VENV_DIR/bin/python3"
VENV_PIP="$VENV_DIR/bin/pip"

# ── 3. Dependencies ────────────────────────────────────────────────────────────
banner "Installing dependencies…"
"$VENV_PIP" install --quiet --upgrade pip

# portaudio needed for pyaudio — install via brew if missing
if ! "$VENV_PY" -c "import pyaudio" &>/dev/null; then
    if command -v brew &>/dev/null; then
        brew install portaudio --quiet 2>/dev/null || true
    fi
fi

"$VENV_PIP" install --quiet \
    openai-whisper \
    pyaudio \
    pyobjc-framework-Cocoa \
    pyobjc-framework-AppKit

ok "Dependencies installed"

# ── 4. Config & history dir ────────────────────────────────────────────────────
banner "Setting up ~/.voiceprompt/…"
mkdir -p "$VP_DIR"
CONFIG="$VP_DIR/config.json"
if [[ ! -f "$CONFIG" ]]; then
    cat > "$CONFIG" << 'EOCFG'
{
  "whisper_model": "base",
  "auto_type":     true,
  "sample_rate":   16000,
  "channels":      1,
  "chunk_size":    1024
}
EOCFG
    ok "Created config.json (whisper model: base)"
else
    ok "config.json already exists"
fi

# ── 5. Whisper model pre-download ──────────────────────────────────────────────
banner "Pre-downloading Whisper base model (~140 MB)…"
"$VENV_PY" -c "import whisper; whisper.load_model('base')" && ok "Whisper base model ready"

# ── 6. LaunchAgent ─────────────────────────────────────────────────────────────
banner "Installing LaunchAgent…"
mkdir -p "$LAUNCH_AGENTS"

# Stop existing instance if running
launchctl unload "$PLIST_DST" 2>/dev/null || true
pkill -f voiceprompt.py 2>/dev/null || true
sleep 1

cat > "$PLIST_DST" << EOPLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.tars.voiceprompt</string>
    <key>ProgramArguments</key>
    <array>
        <string>/bin/bash</string>
        <string>${REPO_DIR}/voiceprompt_launcher.sh</string>
    </array>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <dict>
        <key>SuccessfulExit</key>
        <false/>
    </dict>
    <key>ThrottleInterval</key>
    <integer>30</integer>
    <key>StandardOutPath</key>
    <string>${VP_DIR}/launchagent.log</string>
    <key>StandardErrorPath</key>
    <string>${VP_DIR}/launchagent-err.log</string>
    <key>EnvironmentVariables</key>
    <dict>
        <key>PATH</key>
        <string>${VENV_DIR}/bin:/usr/local/bin:/usr/bin:/bin:/opt/homebrew/bin</string>
        <key>HOME</key>
        <string>${HOME}</string>
    </dict>
</dict>
</plist>
EOPLIST

ok "LaunchAgent installed → $PLIST_DST"

# ── 7. Launcher script ─────────────────────────────────────────────────────────
cat > "$REPO_DIR/voiceprompt_launcher.sh" << EOLAUNCHER
#!/usr/bin/env bash
SCRIPT_DIR="\$(cd "\$(dirname "\${BASH_SOURCE[0]}")" && pwd)"
exec "\$SCRIPT_DIR/.venv/bin/python3" "\$SCRIPT_DIR/voiceprompt.py"
EOLAUNCHER
chmod +x "$REPO_DIR/voiceprompt_launcher.sh"
ok "Launcher script updated"

# ── 8. Load and launch ─────────────────────────────────────────────────────────
banner "Starting VoicePrompt…"
launchctl load "$PLIST_DST"
sleep 2

if launchctl list | grep -q "com.tars.voiceprompt"; then
    ok "VoicePrompt is running"
else
    warn "LaunchAgent may not have started — check $VP_DIR/launchagent-err.log"
fi

echo ""
echo -e "${GRN}╔══════════════════════════════════════════╗${NC}"
echo -e "${GRN}║   ✅  VoicePrompt installed!             ║${NC}"
echo -e "${GRN}╚══════════════════════════════════════════╝${NC}"
echo ""
echo "  The VoicePrompt window should now be open."
echo "  Click ⏺ Start Recording, speak, click ⏹ Stop."
echo "  Transcript copies to clipboard — press ⌘V to paste."
echo ""
echo "  Logs:    $VP_DIR/voiceprompt.log"
echo "  Config:  $VP_DIR/config.json"
echo "  History: $VP_DIR/history.json"
echo ""
echo -e "  ${YLW}Optional:${NC} Grant Accessibility permission for auto-type:"
echo "  System Settings → Privacy & Security → Accessibility → add Python"
echo ""
