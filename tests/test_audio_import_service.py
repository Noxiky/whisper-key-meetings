import hashlib
import wave
from pathlib import Path

import numpy as np
import pytest

from whisper_key.application import AudioImportError, AudioImportService, SessionService
from whisper_key.application.audio_import_service import PyAvStreamingDecoder
from whisper_key.meeting_live_transcriber import TranscriptResult


class FakeDecoder:
    def __init__(self, chunks):
        self.chunks = chunks

    def probe_duration_ms(self, _source: Path) -> int:
        samples = sum(len(chunk) for chunk in self.chunks)
        return round(samples / 16_000 * 1000)

    def decode(self, _source: Path):
        yield from self.chunks


class FakeTranscriber:
    instances = []

    def __init__(self, _engine, *, enable_provisional, on_transcript, on_backpressure):
        self.enable_provisional = enable_provisional
        self.on_transcript = on_transcript
        self.on_backpressure = on_backpressure
        self.backlog = 0
        self.offset = 0
        self.source = None
        self.started = False
        self.__class__.instances.append(self)

    def register_source(self, source_id, label):
        self.source = (source_id, label)

    def start(self, active_sources):
        assert active_sources == ["imported"]
        self.started = True

    def push_audio(self, source_id, audio, sample_rate):
        assert self.started
        assert source_id == "imported"
        assert sample_rate == 16_000
        started_at = self.offset
        self.offset += round(len(audio) / sample_rate * 1000)
        self.on_transcript(
            TranscriptResult(
                source_id="imported",
                source="IMPORTED",
                text=f"Fragmento {self.offset}",
                started_at_ms=started_at,
                ended_at_ms=self.offset,
                language="es",
            )
        )
        return True

    def stop(self):
        self.started = False


def test_audio_import_streams_to_durable_session(tmp_path, schema_validator):
    source = tmp_path / "clase.mp3"
    source.write_bytes(b"decoded by fake")
    chunks = [
        np.full(8_000, 1_000, dtype="<i2"),
        np.full(16_000, -1_000, dtype="<i2"),
    ]
    updates = []
    service = SessionService(tmp_path / "library")
    importer = AudioImportService(
        service,
        object(),
        decoder=FakeDecoder(chunks),
        transcriber_factory=FakeTranscriber,
        on_progress=updates.append,
    )

    result = importer.import_file(source)

    assert result.duration_ms == 1_500
    assert result.transcript_segments == 2
    assert "sessions" in result.folder.parts
    assert service.session.status.value == "completed"
    assert service.session.title == "clase"
    assert updates[-1].state == "complete"
    assert updates[-1].percent == 100
    assert FakeTranscriber.instances[-1].enable_provisional is False

    audio_path = next((result.folder / "audio" / "imported").glob("*.wav"))
    with wave.open(str(audio_path), "rb") as reader:
        assert reader.getnchannels() == 1
        assert reader.getframerate() == 16_000
        assert reader.getnframes() == 24_000
    events = service.repository.read_events(result.folder)
    audio_event = next(event for event in events if event["type"] == "audio_chunk_finalized")
    assert audio_event["payload"]["source"] == "IMPORTED"
    assert audio_event["payload"]["sha256"] == hashlib.sha256(audio_path.read_bytes()).hexdigest()
    assert {event["payload"]["source"] for event in events if event["type"] == "transcript_final"} == {
        "IMPORTED"
    }
    validator = schema_validator("timeline-event.schema.json")
    for event in events:
        validator.validate(event)


def test_audio_import_rejects_unsupported_file_before_creating_session(tmp_path):
    source = tmp_path / "notes.txt"
    source.write_text("not audio", encoding="utf-8")
    service = SessionService(tmp_path / "library")
    importer = AudioImportService(
        service,
        object(),
        decoder=FakeDecoder([]),
        transcriber_factory=FakeTranscriber,
    )

    with pytest.raises(AudioImportError, match="Formato no compatible"):
        importer.import_file(source)

    assert service.session is None


def test_pyav_decoder_resamples_real_stereo_wav_in_bounded_frames(tmp_path):
    source = tmp_path / "stereo-48k.wav"
    stereo = np.column_stack(
        (
            np.full(48_000, 1_000, dtype="<i2"),
            np.full(48_000, -500, dtype="<i2"),
        )
    )
    with wave.open(str(source), "wb") as output:
        output.setnchannels(2)
        output.setsampwidth(2)
        output.setframerate(48_000)
        output.writeframes(stereo.tobytes())

    decoder = PyAvStreamingDecoder()
    chunks = list(decoder.decode(source))

    assert decoder.probe_duration_ms(source) == 1_000
    assert chunks
    assert all(chunk.dtype == np.dtype("<i2") for chunk in chunks)
    assert sum(len(chunk) for chunk in chunks) == pytest.approx(16_000, abs=32)


def test_interrupted_import_keeps_decoded_audio_recoverable(tmp_path):
    source = tmp_path / "interrupted.mp3"
    source.write_bytes(b"decoded by fake")

    class FailingDecoder(FakeDecoder):
        def decode(self, _source: Path):
            yield np.full(8_000, 1_000, dtype="<i2")
            raise RuntimeError("decoder stopped")

    service = SessionService(tmp_path / "library")
    importer = AudioImportService(
        service,
        object(),
        decoder=FailingDecoder([np.zeros(8_000, dtype="<i2")]),
        transcriber_factory=FakeTranscriber,
    )

    with pytest.raises(RuntimeError, match="decoder stopped"):
        importer.import_file(source)

    assert service.session.status.value == "recoverable"
    audio_path = next((service.folder / "audio" / "imported").glob("*.wav"))
    with wave.open(str(audio_path), "rb") as reader:
        assert reader.getnframes() == 8_000
    events = service.repository.read_events(service.folder)
    assert any(
        event["type"] == "audio_chunk_finalized" and event["payload"]["source"] == "IMPORTED"
        for event in events
    )
