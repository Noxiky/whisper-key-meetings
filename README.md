# Whisper Key Meetings

> A meeting-mode fork of [**PinW/whisper-key-local**](https://github.com/PinW/whisper-key-local) — all upstream functionality preserved, plus a live dual-track meeting transcription mode. Original work © 2025 Pin Wang, MIT-licensed. This fork remains MIT under the same terms (see [LICENSE](LICENSE)).

Global hotkeys to record speech and transcribe directly to your cursor, **plus a meeting listener that transcribes microphone + computer audio in real time to your console** (press `F9`).

> Questions or ideas? [Discord](https://discord.gg/uZnXV8snhz) (upstream community)

## 🎧 Meeting Listener Mode (this fork)

Press **`F9`** to toggle a live meeting transcription. Captures your microphone and your PC's system audio (Windows WASAPI loopback) on independent tracks, transcribes each with `faster-whisper`, and prints labeled lines to the terminal in real time:

```
[MIC] What's the agenda for today?
[SYS] Welcome to the all-hands meeting.
[MIC] Got it, ready when you are.
```

- **Independent tracks** — `[MIC]` for your microphone, `[SYS]` for whatever's playing through your default Windows playback device (YouTube, Zoom, Spotify…).
- **No file output** — pure live console transcription. Nothing saved to disk.
- **Silence-segmented + queued** — segments flush on ≥0.8s pauses (or a 15s safety cap), pushed to a background transcription worker so capture never blocks.
- **Hallucination filter** — strips common whisper-on-silence artifacts (`"Transcription by CastingWords"`, lone `.`, `"Thanks for watching"`, etc.).
- **Per-device override** — set `capture.meeting.system_audio_device: "<substring>"` in `user_settings.yaml` if your default playback device isn't the one with the audio you want.
- **Auto-stop** — meeting ends automatically after `auto_stop_silence_seconds` (default 120) of no audio on either source.

All other dictation / voice-command functionality from the upstream README below is unchanged.

## 🚀 One-paste install (Windows)

Open PowerShell and paste:

```powershell
irm https://raw.githubusercontent.com/Noxiky/whisper-key-meetings/main/install.ps1 | iex
```

That runs a preflight check (git, Python 3.11–3.13, PyPI reachability, disk space), clones the repo to `%USERPROFILE%\whisper-key-meetings`, builds a `.venv`, installs all dependencies, auto-installs CUDA 12 runtime libs if an NVIDIA GPU is detected, creates a Desktop shortcut, and verifies the install at the end. Re-running pulls the latest changes.

### Run only the smoke test (no install)

To check what would or wouldn't work on your machine *before* installing:

```powershell
irm https://raw.githubusercontent.com/Noxiky/whisper-key-meetings/main/doctor.ps1 | iex
```

It prints colored `[ok]` / `[warn]` / `[XX]` lines for: PowerShell version, git, Python, internet to PyPI / GitHub / pythonhosted, disk space, NVIDIA GPU detection, and — if you already installed — whether the venv is valid and CUDA DLLs are present. Exit 0 if all green, 1 if anything failed.

### Uninstall

```powershell
irm https://raw.githubusercontent.com/Noxiky/whisper-key-meetings/main/uninstall.ps1 | iex
```

Removes the install dir and the Desktop shortcut. By default it **keeps** your user settings (`%APPDATA%\whisperkey`) and the downloaded whisper models (`~\.cache\huggingface\hub`, can be several GB). To purge those too, set the relevant env vars first:

```powershell
$env:WKM_PURGE_CONFIG = "1"
$env:WKM_PURGE_MODELS = "1"
$env:WKM_YES          = "1"   # skip the confirmation prompt
irm https://raw.githubusercontent.com/Noxiky/whisper-key-meetings/main/uninstall.ps1 | iex
```

Optional overrides (set **before** the `irm | iex` line):

```powershell
$env:WKM_INSTALL_DIR = "D:\apps\wkm"   # install somewhere else
$env:WKM_NO_LAUNCH   = "1"             # don't ask to launch at the end
```

After install, launch any time with:

```powershell
cd $env:USERPROFILE\whisper-key-meetings
.\run-whisper-key.cmd
```

## ✨ Features

- **Meeting Listener (new)**: F9 toggles live dual-track [MIC]/[SYS] console transcription
- **Global Hotkey**: Start recording speech from any app
- **Auto-Paste**: Transcribe directly to cursor
- **Auto-Send**: Optionally auto-send with ENTER keypress
- **Local/Offline**: Voice data never leaves your computer
- **CPU Ready**: Small, efficient models available
- **GPU Ready**: Support for both NVIDIA & AMD cards
- **Cross-platform**: Works on Windows and macOS
- **Voice Commands**: Trigger shortcuts, text snippets, and shell commands by voice — [docs](docs/voice-commands.md)
- **Configurable**: Customize hotkeys, models, and [much more](#️-configuration)

## 🚀 Quick Start

### From PyPI (Recommended)

Requires Python 3.11-3.13

```bash
# With pipx (isolated environment)
pipx install whisper-key-local

# Or with pip
pip install whisper-key-local
```

Then run: `whisper-key` (or `wk` for short)

### Windows App

1. Download `whisper-key.exe` from the [latest release](https://github.com/PinW/whisper-key-local/releases/latest)
2. Run `whisper-key.exe`

### From Source

```bash
git clone https://github.com/PinW/whisper-key-local.git
cd whisper-key-local
pip install -e .
python whisper-key.py
```

## 🎤 Basic Usage

| Hotkey | Windows | macOS |
|--------|---------|-------|
| Start recording | `Ctrl+Win` | `Fn+Ctrl` |
| Stop & transcribe | `Ctrl` | `Fn` |
| Stop & auto-send | `Alt` | `Option` |
| Cancel recording | `Esc` | `Shift` |
| Voice command mode | `Alt+Win` | `Fn+Command` |
| **Meeting listener toggle** | **`F9`** | **`F9`** |

Open the system tray / menu bar icon to:
- Toggle auto-paste vs clipboard-only
- Change transcription model
- Select audio device

## 🗣️ Voice Commands

Speak trigger phrases to run shell commands and more. Define in:
- **Windows:** `%APPDATA%\whisperkey\commands.yaml`
- **macOS:** `~/.whisperkey/commands.yaml`

```yaml
commands:
  # Send a keyboard shortcut
  - trigger: "undo"
    hotkey: "ctrl+z"
  # Deliver pre-written text
  - trigger: "my email"
    type: "user@example.com"
  # Run a shell command
  - trigger: "open notepad"
    run: 'notepad.exe'
```

See the **[Voice Commands Guide](docs/voice-commands.md)** for full details.

## ⚡ GPU Acceleration

Whisper Key detects your GPU on first launch and offers one-press install of the required runtime libraries. Supports **NVIDIA** (CUDA) and **AMD** (ROCm).

For manual setup or troubleshooting, see the **[GPU Setup Guide](docs/gpu-setup.md)**.

## ⚙️ Configuration

Local settings at:
- **Windows:** `%APPDATA%\whisperkey\user_settings.yaml`
- **macOS:** `~/.whisperkey/user_settings.yaml`

Delete this file and restart app to reset to defaults.

| Option | Default | Notes |
|--------|---------|-------|
| **Whisper** |||
| `whisper.model` | `tiny` | Any model defined in `whisper.models` |
| `whisper.device` | `cpu` | cpu or cuda (NVIDIA/AMD GPU) — [setup guide](docs/gpu-setup.md) |
| `whisper.compute_type` | `int8` | int8/float16/float32 |
| `whisper.language` | `auto` | auto or language code (en, es, fr, etc.) |
| `whisper.beam_size` | `5` | Higher = more accurate but slower (1-10) |
| `whisper.initial_prompt` | `""` | Guide transcription style, language variant, or script |
| `whisper.hotwords` | `[]` | Words the model should favor (names, technical terms) |
| `whisper.strip_trailing_period` | `false` | Strip trailing period from output |
| `whisper.models` | (see config) | Add custom HuggingFace or local models |
| **Hotkeys** |||
| `hotkey.recording_hotkey` | `ctrl+win` / `fn+ctrl` | Windows / macOS |
| `hotkey.stop_key` | `ctrl` / `fn` | Stop recording |
| `hotkey.auto_send_key` | `alt` / `option` | Stop + paste + Enter |
| `hotkey.cancel_combination` | `esc` / `shift` | Cancel recording |
| `hotkey.recording_mode` | `toggle` | toggle or push_to_talk |
| `hotkey.command_hotkey` | `alt+win` / `fn+command` | Voice command mode |
| **Voice Activity Detection** |||
| `vad.vad_precheck_enabled` | `true` | Prevent hallucinations on silence |
| `vad.vad_onset_threshold` | `0.7` | Speech detection start (0.0-1.0) |
| `vad.vad_offset_threshold` | `0.55` | Speech detection end (0.0-1.0) |
| `vad.vad_min_speech_duration` | `0.1` | Min speech segment (seconds) |
| `vad.vad_realtime_enabled` | `true` | Auto-stop on silence |
| `vad.vad_silence_timeout_seconds` | `30.0` | Seconds before auto-stop |
| **Audio** |||
| `audio.host` | `null` | Audio API (WASAPI, Core Audio, etc.) |
| `audio.channels` | `1` | 1 = mono, 2 = stereo |
| `audio.dtype` | `float32` | float32/int16/int24/int32 |
| `audio.max_duration` | `900` | Max recording seconds (0 = unlimited) |
| `audio.input_device` | `default` | Device ID or "default" |
| **Clipboard** |||
| `clipboard.auto_paste` | `true` | false = clipboard only |
| `clipboard.delivery_method` | `paste` | paste (Ctrl+V) or type (direct injection) |
| `clipboard.paste_hotkey` | `ctrl+v` / `cmd+v` | Paste key simulation |
| `clipboard.paste_pre_paste_delay` | `0.05` | Delay after copy, before paste hotkey (seconds) |
| `clipboard.paste_preserve_clipboard` | `true` | Restore clipboard after paste |
| `clipboard.paste_clipboard_restore_delay` | `0.5` | Delay before clipboard restore (seconds) |
| `clipboard.type_also_copy_to_clipboard` | `false` | Also copy to clipboard in type mode |
| `clipboard.type_auto_enter_delay` | `0.15` | Delay before ENTER after typing (seconds) |
| `clipboard.type_auto_enter_delay_per_100_chars` | `0.1` | Extra ENTER delay per 100 typed chars (seconds) |
| **Logging** |||
| `logging.level` | `INFO` | DEBUG/INFO/WARNING/ERROR/CRITICAL |
| `logging.file.enabled` | `true` | Write to app.log |
| `logging.log_transcriptions` | `false` | Include transcribed text in log (privacy) |
| `logging.console.enabled` | `true` | Print to console |
| `logging.console.level` | `WARNING` | Console verbosity |
| **Audio Feedback** |||
| `audio_feedback.enabled` | `true` | Play sounds on record/stop |
| `audio_feedback.transcription_complete_enabled` | `false` | Play sound on transcription complete |
| `audio_feedback.start_sound` | `assets/sounds/...` | Custom sound file path |
| `audio_feedback.stop_sound` | `assets/sounds/...` | Custom sound file path |
| `audio_feedback.cancel_sound` | `assets/sounds/...` | Custom sound file path |
| `audio_feedback.transcription_complete_sound` | `assets/sounds/...` | Custom sound file path |
| **System Tray** |||
| `system_tray.enabled` | `true` | Show tray icon |
| `system_tray.tooltip` | `Whisper Key` | Hover text |
| **Console** |||
| `console.start_hidden` | `false` | Hide console after startup (whisper-key-hideable.exe only) |
| **Update** |||
| `update.mode` | `prompt` | prompt or auto |
| **Voice Commands** |||
| `voice_commands.enabled` | `true` | Enable voice command mode |

## 📁 Model Cache

Default path for transcription models (via HuggingFace):
- **Windows:** `%USERPROFILE%\.cache\huggingface\hub\`
- **macOS:** `~/.cache/huggingface/hub/`

## Contributing

Check the [roadmap](docs/roadmap/roadmap.md) for planned features and see [CONTRIBUTING.md](CONTRIBUTING.md) for guidelines. Please open an issue before starting work on new features.

## 📦 Dependencies

**Cross-platform:**
`faster-whisper` · `numpy` · `sounddevice` · `soxr` · `pyperclip` · `ruamel.yaml` · `pystray` · `Pillow` · `playsound3` · `ten-vad` · `hf-xet`

**Windows:** `global-hotkeys` · `pywin32`

**macOS:** `pyobjc-framework-Quartz` · `pyobjc-framework-ApplicationServices`
