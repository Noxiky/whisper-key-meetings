# WhisperKey Meeting Listener + Dictation Modes Implementation Plan

> For Hermes: Use subagent-driven-development skill to implement this plan task-by-task.

Goal: Add two WhisperKey capture modes: mic-only dictation with auto-paste, and meeting-listener mode that records microphone plus PC/system audio into a timestamped .txt transcript with pause-separated lines and future speaker diarization support.

Architecture: Keep the current dictation path stable. Add a separate meeting recorder path that captures microphone and WASAPI loopback/system audio as separate tracks where possible, writes session artifacts under the user data directory, and post-processes with faster-whisper first. Add diarization later behind an optional dependency (WhisperX/pyannote or sherpa-onnx), because real speaker naming is a harder problem than transcription.

Tech Stack: Python, sounddevice currently in WhisperKey, Windows WASAPI loopback candidate libraries SoundCard or PyAudioWPatch/pyaudio_portaudio, faster-whisper for transcription, Silero/sherpa-onnx VAD or current TEN VAD for pause splitting, optional WhisperX/pyannote for diarization.

---

## Research Summary

Best open-source options found:

- Meetily: https://github.com/Zackriya-Solutions/meetily — local meeting assistant, mic + system audio, good architecture reference, speaker ID listed as coming soon.
- WhisperX: https://github.com/m-bain/whisperX — Whisper transcription + word timestamps + pyannote diarization.
- pyannote.audio: https://github.com/pyannote/pyannote-audio — core diarization toolkit.
- sherpa-onnx: https://github.com/k2-fsa/sherpa-onnx — offline ASR/VAD/speaker diarization/speaker ID with ONNX; promising packaged route.
- noScribe: https://github.com/kaixxx/noScribe — local transcription GUI with diarization, GPL reference.
- TranscriptionSuite: https://github.com/homelab-00/TranscriptionSuite — local STT app, diarization, live transcription, GPL reference.
- Vexa: https://github.com/Vexa-ai/vexa — meeting bot route for Meet/Teams/Zoom; better real speaker metadata, less like local WhisperKey.
- PyAudioWPatch: https://github.com/s0d3s/PyAudioWPatch — Windows WASAPI loopback capture.
- pyaudio_portaudio: https://github.com/intxcc/pyaudio_portaudio — MIT PyAudio/PortAudio fork with WASAPI loopback.
- SoundCard: https://github.com/bastibe/SoundCard — BSD Python audio library with Windows loopback support.
- ProcTap: https://github.com/m96-chan/ProcTap — process-specific Windows audio capture experiment.

Important limitation: local mixed audio cannot reliably know real human names. It can label Speaker 1 / Speaker 2, or use manual naming/enrolled voice profiles later. Meeting bots can sometimes get actual names from platform metadata.

---

## Task 1: Add capture mode config

Objective: Represent mic-only dictation vs meeting listener in config without changing default behavior.

Files:
- Modify: `src/whisper_key/config.defaults.yaml`
- Modify: `src/whisper_key/config_manager.py`
- Test: add or update config validation tests if test suite has config tests.

Config shape:

```yaml
capture:
  mode: dictation  # dictation | meeting
  meeting:
    output_dir: ""
    split_on_pause_seconds: 1.2
    capture_microphone: true
    capture_system_audio: true
    system_audio_backend: auto  # auto | soundcard | pyaudiowpatch
    diarization: false
    speaker_labels: anonymous  # anonymous | manual | enrolled
```

Verification:
- Run config validation.
- Launch existing dictation mode and confirm no behavior change.

## Task 2: Add hotkey or command for meeting mode

Objective: Add a separate start/stop path for meeting listener without breaking Ctrl+Win dictation.

Files:
- Modify: `src/whisper_key/config.defaults.yaml`
- Modify: `src/whisper_key/config_manager.py`
- Modify: `src/whisper_key/hotkey_listener.py`
- Modify: `src/whisper_key/state_manager.py`

Suggested hotkeys:
- `ctrl+win`: mic-only dictation (existing)
- `shift+ctrl+win`: meeting listener toggle

Verification:
- Existing dictation starts/stops as before.
- Meeting hotkey logs a placeholder action before capture implementation.

## Task 3: Build Windows system audio capture spike

Objective: Choose a reliable system-audio capture library on this PC.

Files:
- Create: `.temp/spike_system_audio_capture.py`

Test candidates in order:
1. SoundCard
2. PyAudioWPatch or pyaudio_portaudio

Spike requirements:
- List input/mic devices.
- List loopback/system-output devices.
- Record 5 seconds of mic and system audio separately.
- Save WAV files.
- Confirm non-zero samples.

Verification:
- Play YouTube/system audio and speak during spike.
- Confirm both WAV files contain audio.

## Task 4: Create MeetingRecorder component

Objective: Encapsulate meeting-mode recording separate from existing AudioRecorder.

Files:
- Create: `src/whisper_key/meeting_recorder.py`
- Modify: `src/whisper_key/state_manager.py`

API sketch:

```python
class MeetingRecorder:
    def start_recording(self) -> bool: ...
    def stop_recording(self) -> MeetingRecording: ...
    def get_recording_status(self) -> bool: ...
```

`MeetingRecording` should include:
- `session_id`
- `started_at`
- `mic_wav_path`
- `system_wav_path`
- `mixed_wav_path` if generated
- sample rate / duration metadata

Verification:
- Meeting mode creates WAV artifacts under app data.
- Existing dictation path untouched.

## Task 5: Add pause-separated transcript writer

Objective: Produce `.txt` with line breaks at pauses before diarization.

Files:
- Create: `src/whisper_key/meeting_transcript_writer.py`
- Modify: `src/whisper_key/state_manager.py`

Initial implementation:
- Transcribe mixed audio with faster-whisper using word/segment timestamps if available.
- Write each segment on a separate line.
- Insert blank line if gap between segment end and next segment start exceeds config threshold.

Output example:

```text
[00:00:02] Me: We should start with the Hermes gateway setup.

[00:00:08] System: Okay, I can share my screen now.
```

Before diarization is implemented, labels can be:
- `Me` for mic-only track segments if separate track transcription works.
- `System` for system track segments.
- `Speaker` for mixed unknown.

Verification:
- Meeting mode creates a `.txt` file.
- Pauses create new lines/blank lines.

## Task 6: Add optional diarization backend

Objective: Add anonymous speaker labels using WhisperX/pyannote or sherpa-onnx behind optional config.

Files:
- Create: `src/whisper_key/diarization.py`
- Modify: `src/whisper_key/config.defaults.yaml`
- Modify: `src/whisper_key/meeting_transcript_writer.py`

Approach:
- Start with anonymous labels: `Speaker 1`, `Speaker 2`.
- Do not promise real names.
- If pyannote is used, require `HF_TOKEN` and explicit model terms acceptance.
- If sherpa-onnx works locally, prefer it for easier packaging.

Verification:
- On a sample WAV with two speakers, transcript includes alternating anonymous speakers.

## Task 7: Integrate overlay state

Objective: Reuse the floating overlay to show meeting listener state.

Files:
- Modify: `src/whisper_key/system_tray.py`
- Modify: `src/whisper_key/floating_overlay.py`
- Modify: `src/whisper_key/state_manager.py`

Add state:
- `meeting` or `listening`

Verification:
- Overlay label changes to `meeting` when meeting listener is active.
- Close X still exits app.

## Task 8: End-to-end verification

Objective: Prove all modes work on Windows.

Manual tests:
1. Start WhisperKey.
2. Press `Ctrl+Win`, speak, press `Ctrl`: text auto-pastes.
3. Press meeting hotkey, play PC audio + speak, stop meeting hotkey.
4. Confirm output folder contains WAV + TXT.
5. Confirm TXT has multiple pause-separated lines.
6. Confirm overlay state changes.

Regression checks:
- Existing CUDA/faster-whisper smoke test still works.
- Existing hotkeys still register.
- No duplicate stale WhisperKey process remains.
