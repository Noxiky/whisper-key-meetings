import os
import queue
import re
import threading
import wave
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import numpy as np


@dataclass(frozen=True)
class AudioPacket:
    source_id: str
    audio: np.ndarray
    sample_rate: int


@dataclass
class OpenWave:
    source_id: str
    index: int
    part_path: Path
    raw_file: object
    wave_file: wave.Wave_write
    sample_rate: int
    channels: int
    frames: int = 0
    writes_since_sync: int = 0


class DurableAudioStore:
    SENTINEL = object()

    def __init__(
        self,
        max_queue_packets: int = 256,
        rotate_seconds: int = 1800,
        sync_every_packets: int = 10,
        on_file_finalized: Callable[[str, Path], None] | None = None,
    ):
        self._queue = queue.Queue(maxsize=max_queue_packets)
        self.rotate_seconds = rotate_seconds
        self.sync_every_packets = sync_every_packets
        self.on_file_finalized = on_file_finalized
        self._thread: threading.Thread | None = None
        self._folder: Path | None = None
        self._stage_id: str | None = None
        self._writers: dict[str, OpenWave] = {}
        self._indices: dict[str, int] = {}
        self._error: Exception | None = None

    @property
    def backlog(self) -> int:
        return self._queue.qsize()

    @property
    def error(self) -> Exception | None:
        return self._error

    def start(self, folder: Path, stage_id: str) -> None:
        if self._thread and self._thread.is_alive():
            raise RuntimeError("Audio store is already running")
        self._folder = Path(folder)
        self._stage_id = stage_id
        self._writers = {}
        self._indices = {}
        self._error = None
        with self._queue.mutex:
            self._queue.queue.clear()
        self._thread = threading.Thread(target=self._run, name="WhisperKeyAudioStore", daemon=True)
        self._thread.start()

    def submit(self, source_id: str, audio: np.ndarray, sample_rate: int) -> bool:
        if not self._thread or not self._thread.is_alive() or self._error:
            return False
        if not re.fullmatch(r"[a-z0-9_-]+", source_id):
            return False
        array = np.asarray(audio, dtype=np.float32).copy()
        if not array.size or sample_rate <= 0:
            return True
        try:
            self._queue.put_nowait(AudioPacket(source_id, array, sample_rate))
            return True
        except queue.Full:
            return False

    def stop(self, timeout: float = 30.0) -> None:
        thread = self._thread
        if not thread:
            return
        if thread.is_alive():
            self._queue.put(self.SENTINEL)
        thread.join(timeout=timeout)
        if thread.is_alive():
            raise TimeoutError("Audio store did not drain before timeout")
        self._thread = None
        if self._error:
            raise RuntimeError("Audio store failed") from self._error

    def recover_partials(self, folder: Path) -> list[Path]:
        recovered = []
        for part_path in Path(folder).rglob("*.wav.part"):
            try:
                with wave.open(str(part_path), "rb") as reader:
                    if reader.getnframes() <= 0:
                        continue
                destination = part_path.with_suffix("")
                os.replace(part_path, destination)
                recovered.append(destination)
            except (OSError, EOFError, wave.Error):
                continue
        return recovered

    def _run(self) -> None:
        try:
            while True:
                packet = self._queue.get()
                if packet is self.SENTINEL:
                    break
                self._write(packet)
        except Exception as exc:
            self._error = exc
        finally:
            for source_id in list(self._writers):
                self._close_writer(source_id)

    def _write(self, packet: AudioPacket) -> None:
        channels = 1 if packet.audio.ndim == 1 else packet.audio.shape[1]
        frames = packet.audio.shape[0]
        writer = self._writers.get(packet.source_id)
        if writer and (
            writer.sample_rate != packet.sample_rate
            or writer.channels != channels
            or writer.frames + frames > self.rotate_seconds * writer.sample_rate
        ):
            self._close_writer(packet.source_id)
            writer = None
        if writer is None:
            writer = self._open_writer(packet.source_id, packet.sample_rate, channels)

        pcm = (np.clip(packet.audio, -1.0, 1.0) * 32767).astype("<i2", copy=False)
        writer.wave_file.writeframes(pcm.tobytes(order="C"))
        writer.frames += frames
        writer.writes_since_sync += 1
        if writer.writes_since_sync >= self.sync_every_packets:
            writer.raw_file.flush()
            os.fsync(writer.raw_file.fileno())
            writer.writes_since_sync = 0

    def _open_writer(self, source_id: str, sample_rate: int, channels: int) -> OpenWave:
        folder_name = "sys" if source_id == "system" else source_id
        folder = self._require_folder() / "audio" / folder_name
        folder.mkdir(parents=True, exist_ok=True)
        if source_id not in self._indices:
            existing = []
            for path in folder.glob(f"{self._stage_id}-*.wav"):
                try:
                    existing.append(int(path.stem.rsplit("-", 1)[1]))
                except (IndexError, ValueError):
                    continue
            self._indices[source_id] = max(existing, default=0)
        index = self._indices[source_id] + 1
        self._indices[source_id] = index
        part_path = folder / f"{self._stage_id}-{index:04d}.wav.part"
        raw_file = part_path.open("w+b")
        wave_file = wave.open(raw_file, "wb")
        wave_file.setnchannels(channels)
        wave_file.setsampwidth(2)
        wave_file.setframerate(sample_rate)
        writer = OpenWave(source_id, index, part_path, raw_file, wave_file, sample_rate, channels)
        self._writers[source_id] = writer
        return writer

    def _close_writer(self, source_id: str) -> None:
        writer = self._writers.pop(source_id, None)
        if not writer:
            return
        writer.wave_file.close()
        writer.raw_file.flush()
        os.fsync(writer.raw_file.fileno())
        writer.raw_file.close()
        destination = writer.part_path.with_suffix("")
        os.replace(writer.part_path, destination)
        if self.on_file_finalized:
            self.on_file_finalized(source_id, destination)

    def _require_folder(self) -> Path:
        if self._folder is None:
            raise RuntimeError("Audio store has no session folder")
        return self._folder
