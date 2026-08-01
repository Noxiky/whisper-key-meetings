import json
import zipfile
from pathlib import Path

from whisper_key.infrastructure import DiagnosticsBundleService


def test_bundle_excludes_private_content_and_raw_log_messages(tmp_path):
    app_data = tmp_path / "WhisperKey"
    app_data.mkdir()
    private = "PRIVATE_TRANSCRIPT_SENTINEL"
    device = "Jane's Secret Headset"
    (app_data / "whisperkey-gui.log").write_text(
        "2026-07-17 07:00:00,000 INFO whisper_key.ui Started safely\n"
        f"2026-07-17 07:00:01,000 ERROR whisper_key.engine transcription='{private}'\n",
        encoding="utf-8",
    )
    (app_data / "user_settings.yaml").write_text(f"secret: {private}\n", encoding="utf-8")
    session = app_data / "library" / "sessions" / "private"
    session.mkdir(parents=True)
    (session / "transcript.raw.md").write_text(private, encoding="utf-8")
    output = tmp_path / "out"
    service = DiagnosticsBundleService(app_data, output)

    result = service.create(
        version="0.9.0",
        application={"ready": True, "model": "large-v3-turbo", "secret": private},
        safe_settings={
            "language": "auto",
            "audio_routes": {
                "input_device": 4,
                "input_device_name": device,
                "system_audio_device": "default",
            },
        },
        audio_diagnostics={
            "summary": {"status": "pass", "title": "Audio listo", "detail": private},
            "mic": {"status": "active", "device": device, "peak_dbfs": -12.0},
        },
    )

    bundle = Path(result["path"])
    assert bundle.is_file()
    assert result["bytes"] == bundle.stat().st_size
    assert len(result["sha256"]) == 64
    with zipfile.ZipFile(bundle) as archive:
        assert set(archive.namelist()) == {"diagnostics.json", "README.txt"}
        payload = b"\n".join(archive.read(name) for name in archive.namelist())
        report = json.loads(archive.read("diagnostics.json"))

    assert private.encode() not in payload
    assert device.encode() not in payload
    assert report["logs"]["levels"] == {"ERROR": 1, "INFO": 1}
    assert report["logs"]["messages_included"] is False
    assert report["logs"]["recent_incidents"][0]["message_fingerprint"]
    assert report["settings"]["audio_routes"] == {
        "input_route": "explicit",
        "input_device_ref": report["audio_diagnostics"]["mic"]["device_ref"],
        "system_route": "default",
        "system_device_ref": "default",
    }
    assert all(value is False for key, value in report["privacy"].items() if key != "uploaded")
    assert report["privacy"]["uploaded"] is False


def test_bundle_tolerates_missing_logs_and_keeps_only_allowlisted_fields(tmp_path):
    service = DiagnosticsBundleService(tmp_path / "missing", tmp_path / "out")

    result = service.create(
        version="0.9.0",
        application={"ready": False, "unexpected": "private"},
        safe_settings={"theme": "dark", "unexpected": "private"},
    )

    with zipfile.ZipFile(result["path"]) as archive:
        report = json.loads(archive.read("diagnostics.json"))
    assert report["application"] == {"ready": False}
    assert report["settings"] == {"theme": "dark"}
    assert report["logs"]["files"] == []
