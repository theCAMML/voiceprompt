@echo off
setlocal enabledelayedexpansion

:: ─────────────────────────────────────────────────────────────────────────────
:: VoicePrompt — Windows Installer
:: Sets up a virtualenv, installs dependencies, downloads Whisper,
:: and optionally adds VoicePrompt to Windows startup.
:: ─────────────────────────────────────────────────────────────────────────────

set "REPO_DIR=%~dp0"
if "%REPO_DIR:~-1%"=="\" set "REPO_DIR=%REPO_DIR:~0,-1%"
set "VENV_DIR=%REPO_DIR%\.venv"
set "VP_DIR=%USERPROFILE%\.voiceprompt"
set "SCRIPT=%REPO_DIR%\voiceprompt_windows.py"

echo.
echo ╔══════════════════════════════════════════╗
echo ║   VoicePrompt Installer — Windows        ║
echo ║   Free local voice dictation             ║
echo ╚══════════════════════════════════════════╝
echo.

:: ── 1. Find Python 3.10+ ─────────────────────────────────────────────────────
echo [1/6] Checking Python...
set "PYTHON="
for %%C in (python3.12 python3.11 python3.10 python3 python) do (
    if not defined PYTHON (
        where %%C >nul 2>&1 && (
            for /f "tokens=2 delims= " %%V in ('%%C --version 2^>^&1') do (
                for /f "tokens=1,2 delims=." %%A in ("%%V") do (
                    if %%A GEQ 3 if %%B GEQ 10 (
                        set "PYTHON=%%C"
                        echo   OK: Found %%C ^(%%V^)
                    )
                )
            )
        )
    )
)

if not defined PYTHON (
    echo   ERROR: Python 3.10+ not found.
    echo   Install from https://www.python.org/downloads/
    echo   Make sure to check "Add Python to PATH" during install.
    pause
    exit /b 1
)

:: ── 2. Virtual environment ────────────────────────────────────────────────────
echo.
echo [2/6] Setting up virtual environment...
if not exist "%VENV_DIR%" (
    %PYTHON% -m venv "%VENV_DIR%"
    echo   OK: Created .venv
) else (
    echo   OK: .venv already exists
)
set "VENV_PY=%VENV_DIR%\Scripts\python.exe"
set "VENV_PIP=%VENV_DIR%\Scripts\pip.exe"

:: ── 3. Dependencies ───────────────────────────────────────────────────────────
echo.
echo [3/6] Installing dependencies (this may take a few minutes)...
"%VENV_PY%" -m pip install --quiet --upgrade pip

:: Core (same as macOS)
"%VENV_PIP%" install --quiet openai-whisper pyaudio pynput

:: Windows replacements for macOS-only packages
"%VENV_PIP%" install --quiet pyperclip

:: Optional — system tray icon (nice to have)
"%VENV_PIP%" install --quiet pystray Pillow

:: Optional — auto-type (paste via Ctrl+V after recording)
"%VENV_PIP%" install --quiet pyautogui

echo   OK: Dependencies installed

:: ── 4. Config dir ─────────────────────────────────────────────────────────────
echo.
echo [4/6] Setting up %%USERPROFILE%%\.voiceprompt\...
if not exist "%VP_DIR%" mkdir "%VP_DIR%"
set "CONFIG=%VP_DIR%\config.json"
if not exist "%CONFIG%" (
    (
        echo {
        echo   "whisper_model": "base",
        echo   "auto_type":     false,
        echo   "auto_type_delay": 2.0,
        echo   "sample_rate":   16000,
        echo   "channels":      1,
        echo   "chunk_size":    1024
        echo }
    ) > "%CONFIG%"
    echo   OK: Created config.json
) else (
    echo   OK: config.json already exists
)

:: ── 5. Pre-download Whisper base model ────────────────────────────────────────
echo.
echo [5/6] Pre-downloading Whisper base model (~140 MB)...
"%VENV_PY%" -c "import whisper; whisper.load_model('base'); print('  OK: Whisper base model ready')"
if errorlevel 1 (
    echo   WARN: Whisper pre-download failed — it will download on first run.
)

:: ── 6. Optional startup ───────────────────────────────────────────────────────
echo.
echo [6/6] Windows startup (optional)...
set /p ADD_STARTUP="   Add VoicePrompt to Windows startup? [y/N]: "
if /i "%ADD_STARTUP%"=="y" (
    "%VENV_PY%" -c "from voiceprompt_windows import install_startup; from pathlib import Path, sys; install_startup(Path(r'%SCRIPT%'), Path(r'%VENV_PY%'))" 2>nul
    reg add "HKCU\Software\Microsoft\Windows\CurrentVersion\Run" /v "VoicePrompt" /t REG_SZ /d "\"%VENV_PY%\" \"%SCRIPT%\"" /f >nul 2>&1
    if errorlevel 1 (
        echo   WARN: Could not add to startup automatically.
        echo   To add manually: open Task Scheduler and create a task that runs:
        echo   "%VENV_PY%" "%SCRIPT%"
    ) else (
        echo   OK: Added to Windows startup
    )
) else (
    echo   Skipped.
)

:: ── Done ──────────────────────────────────────────────────────────────────────
echo.
echo ╔══════════════════════════════════════════╗
echo ║   VoicePrompt installed!                 ║
echo ╚══════════════════════════════════════════╝
echo.
echo   To run VoicePrompt:
echo     "%VENV_PY%" "%SCRIPT%"
echo.
echo   Or double-click: run_voiceprompt.bat  (created below)
echo.
echo   Logs:    %VP_DIR%\voiceprompt.log
echo   Config:  %VP_DIR%\config.json
echo   History: %VP_DIR%\history.json
echo.
echo   Hotkey:  Ctrl+Shift+Space (enable in app)
echo   Auto-type: set "auto_type": true in config.json
echo              (pastes via Ctrl+V after a 2s delay)
echo.

:: Create a convenience launcher bat
(
    echo @echo off
    echo "%VENV_PY%" "%SCRIPT%"
) > "%REPO_DIR%\run_voiceprompt.bat"
echo   Created run_voiceprompt.bat

echo.
set /p LAUNCH_NOW="   Launch VoicePrompt now? [Y/n]: "
if /i not "%LAUNCH_NOW%"=="n" (
    start "" "%VENV_PY%" "%SCRIPT%"
)

endlocal
