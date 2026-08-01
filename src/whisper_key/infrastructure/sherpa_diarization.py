from __future__ import annotations

import wave
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import soxr

from whisper_key.domain.diarization import DiarizationSegment


class DiarizationUnavailable(RuntimeError):
    pass


@dataclass(frozen=True)
class DiarizationAudioChunk:
    path: Path
    started_at_ms: int
    ended_at_ms: int


class SherpaDiarizationAdapter:
    SEGMENTATION_NAME = "sherpa-onnx-pyannote-segmentation-3-0.onnx"
    EMBEDDING_NAME = "3dspeaker-eres2net-base-16k.onnx"

    def __init__(self, model_root: Path):
        self.model_root = Path(model_root)
        self.segmentation_model = self.model_root / self.SEGMENTATION_NAME
        self.embedding_model = self.model_root / self.EMBEDDING_NAME

    @property
    def available(self) -> bool:
        return self.segmentation_model.is_file() and self.embedding_model.is_file()

    @property
    def missing_models(self) -> list[str]:
        return [
            label
            for label, path in [
                ("speaker segmentation", self.segmentation_model),
                ("speaker embedding", self.embedding_model),
            ]
            if not path.is_file()
        ]

    def validate_runtime(self) -> int:
        """Load both ONNX models and return the runtime sample rate."""
        if not self.available:
            raise DiarizationUnavailable(f"Missing local models: {', '.join(self.missing_models)}")
        try:
            diarizer = self._create_diarizer(-1, 0.5)
        except DiarizationUnavailable:
            raise
        except Exception as exc:
            raise DiarizationUnavailable(f"Cannot load the local diarization runtime: {exc}") from exc
        return int(diarizer.sample_rate)

    def process_file(
        self,
        audio_path: Path,
        num_speakers: int = -1,
        cluster_threshold: float = 0.5,
        progress=None,
    ) -> list[DiarizationSegment]:
        return self.process_files(
            [DiarizationAudioChunk(Path(audio_path), 0, self._wave_duration_ms(audio_path))],
            num_speakers=num_speakers,
            cluster_threshold=cluster_threshold,
            progress=progress,
        )

    def process_files(
        self,
        chunks: list[DiarizationAudioChunk],
        num_speakers: int = -1,
        cluster_threshold: float = 0.5,
        progress=None,
    ) -> list[DiarizationSegment]:
        if not self.available:
            raise DiarizationUnavailable(f"Missing local models: {', '.join(self.missing_models)}")
        if not chunks:
            return []
        diarizer = self._create_diarizer(num_speakers, cluster_threshold)
        arrays = []
        spans = []
        compressed_cursor_ms = 0
        for chunk in chunks:
            audio, sample_rate = self._read_wave(chunk.path)
            if sample_rate != diarizer.sample_rate:
                audio = soxr.resample(audio, sample_rate, diarizer.sample_rate).astype(np.float32)
            duration_ms = max(1, round(len(audio) / diarizer.sample_rate * 1000))
            spans.append(
                (
                    compressed_cursor_ms,
                    compressed_cursor_ms + duration_ms,
                    chunk.started_at_ms,
                    chunk.ended_at_ms,
                )
            )
            compressed_cursor_ms += duration_ms
            arrays.append(audio)
        combined = np.concatenate(arrays).astype(np.float32, copy=False)
        compressed_segments = self._process(diarizer, combined, progress)
        return self.map_to_session_timeline(compressed_segments, spans)

    def _create_diarizer(self, num_speakers: int, cluster_threshold: float):
        import sherpa_onnx

        config = sherpa_onnx.OfflineSpeakerDiarizationConfig(
            segmentation=sherpa_onnx.OfflineSpeakerSegmentationModelConfig(
                pyannote=sherpa_onnx.OfflineSpeakerSegmentationPyannoteModelConfig(model=str(self.segmentation_model))
            ),
            embedding=sherpa_onnx.SpeakerEmbeddingExtractorConfig(model=str(self.embedding_model)),
            clustering=sherpa_onnx.FastClusteringConfig(
                num_clusters=num_speakers,
                threshold=cluster_threshold,
            ),
            min_duration_on=0.3,
            min_duration_off=0.5,
        )
        if not config.validate():
            raise DiarizationUnavailable("The installed diarization models are invalid or incompatible")
        return sherpa_onnx.OfflineSpeakerDiarization(config)

    @staticmethod
    def _process(diarizer, audio: np.ndarray, progress=None) -> list[DiarizationSegment]:
        callback = None
        if progress:

            def callback(done, total):
                progress(done / max(1, total))
                return 0

        result = diarizer.process(audio, callback=callback).sort_by_start_time()
        return [
            DiarizationSegment(
                speaker_id=f"speaker_{item.speaker + 1}",
                started_at_ms=round(item.start * 1000),
                ended_at_ms=round(item.end * 1000),
                confidence=None,
            )
            for item in result
        ]

    @staticmethod
    def map_to_session_timeline(
        segments: list[DiarizationSegment],
        spans: list[tuple[int, int, int, int]],
    ) -> list[DiarizationSegment]:
        mapped = []
        for segment in segments:
            for compressed_start, compressed_end, session_start, session_end in spans:
                overlap_start = max(segment.started_at_ms, compressed_start)
                overlap_end = min(segment.ended_at_ms, compressed_end)
                if overlap_end <= overlap_start:
                    continue
                compressed_duration = max(1, compressed_end - compressed_start)
                session_duration = max(1, session_end - session_start)
                mapped_start = session_start + round(
                    (overlap_start - compressed_start) / compressed_duration * session_duration
                )
                mapped_end = session_start + round(
                    (overlap_end - compressed_start) / compressed_duration * session_duration
                )
                mapped.append(
                    DiarizationSegment(
                        speaker_id=segment.speaker_id,
                        started_at_ms=mapped_start,
                        ended_at_ms=max(mapped_start + 1, mapped_end),
                        confidence=segment.confidence,
                    )
                )
        return mapped

    @classmethod
    def _wave_duration_ms(cls, path: Path) -> int:
        try:
            with wave.open(str(path), "rb") as reader:
                return max(1, round(reader.getnframes() / reader.getframerate() * 1000))
        except (OSError, EOFError, wave.Error, ZeroDivisionError) as exc:
            raise DiarizationUnavailable(f"Cannot read diarization audio: {exc}") from exc

    @staticmethod
    def _read_wave(path: Path) -> tuple[np.ndarray, int]:
        try:
            with wave.open(str(path), "rb") as reader:
                if reader.getsampwidth() != 2:
                    raise DiarizationUnavailable("Diarization expects PCM16 WAV audio")
                channels = reader.getnchannels()
                rate = reader.getframerate()
                audio = np.frombuffer(reader.readframes(reader.getnframes()), dtype="<i2").astype(np.float32) / 32767
        except (OSError, EOFError, wave.Error) as exc:
            raise DiarizationUnavailable(f"Cannot read diarization audio: {exc}") from exc
        if channels > 1:
            audio = audio.reshape(-1, channels).mean(axis=1)
        return audio, rate
