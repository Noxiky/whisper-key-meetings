import logging
import threading
import time
from collections.abc import Callable

import numpy as np
from faster_whisper import WhisperModel


class WhisperEngine:
    def __init__(
        self,
        model_key: str = "tiny",
        device: str = "cpu",
        compute_type: str = "int8",
        language: str = None,
        beam_size: int = 5,
        initial_prompt: str = "",
        hotwords: list = None,
        strip_trailing_period: bool = False,
        vad_manager=None,
        model_registry=None,
        log_transcriptions: bool = False,
    ):

        self.model_key = model_key
        self.device = device
        self.compute_type = compute_type
        self.language = None if language == "auto" else language
        self.beam_size = beam_size
        self.initial_prompt = initial_prompt or None
        self.hotwords = ", ".join(hotwords) if hotwords else None
        self.strip_trailing_period = strip_trailing_period
        self.model = None
        self.logger = logging.getLogger(__name__)
        self.registry = model_registry
        self.log_transcriptions = log_transcriptions

        self._loading_thread = None
        self._progress_callback = None
        self.last_load_error = None
        self.model_load_ms: int | None = None
        self.transcription_count = 0
        self.last_transcription_metrics = {}
        self._last_script_retry = False

        self.vad_manager = vad_manager

        self._load_model()

    def _get_model_source(self, model_key: str) -> str:
        if self.registry:
            if hasattr(self.registry, "get_runtime_source"):
                return self.registry.get_runtime_source(model_key)
            return self.registry.get_source(model_key)
        return model_key

    def _is_model_cached(self, model_key: str = None) -> bool:
        if model_key is None:
            model_key = self.model_key
        if self.registry:
            return self.registry.is_model_cached(model_key)
        return False

    def _load_model(self):
        started = time.perf_counter()
        try:
            print(f"🧠 Loading Whisper AI model [{self.model_key}]...")

            was_cached = self._is_model_cached()
            if not was_cached:
                print("Downloading model, this may take a few minutes....")

            model_source = self._get_model_source(self.model_key)
            self.model = WhisperModel(model_source, device=self.device, compute_type=self.compute_type)

            if not was_cached:
                print("\n")  # Workaround for download status bar misplacement

            print(f"   ✓ Whisper model [{self.model_key}] ready!")
            device_label = "GPU" if self.device == "cuda" else "CPU"
            print(f"   ✓ Running on {device_label} with {self.compute_type} precision")
            self.model_load_ms = round((time.perf_counter() - started) * 1000)

        except Exception as e:
            self.logger.error(f"Failed to load Whisper model: {e}")
            raise

    def _load_model_async(self, new_model_key: str, progress_callback: Callable[[str], None] | None = None):
        def _background_loader():
            old_model_key = self.model_key
            old_model = self.model
            old_model_load_ms = getattr(self, "model_load_ms", None)
            self.last_load_error = None
            started = time.perf_counter()
            try:
                self._safe_progress(progress_callback, "Checking model cache...")
                was_cached = self._is_model_cached(new_model_key)

                self._safe_progress(
                    progress_callback,
                    "Loading cached model..." if was_cached else "Downloading model...",
                )

                self.logger.info(f"Loading Whisper model: {new_model_key} (async)")

                model_source = self._get_model_source(new_model_key)
                new_model = WhisperModel(model_source, device=self.device, compute_type=self.compute_type)
                self.model = new_model

                self.model_key = new_model_key
                self.model_load_ms = round((time.perf_counter() - started) * 1000)
                self.transcription_count = 0
                self.logger.info(f"Whisper model [{new_model_key}] loaded successfully (async)")

                self._safe_progress(progress_callback, "Model ready!")

            except Exception as e:
                self.model_key = old_model_key
                self.model = old_model
                self.model_load_ms = old_model_load_ms
                self.last_load_error = str(e)
                self.logger.error(f"Failed to load Whisper model async: {e}")
                self._safe_progress(progress_callback, f"Failed to load model: {e}")
            finally:
                self._loading_thread = None
                self._progress_callback = None

        if self._loading_thread and self._loading_thread.is_alive():
            self.logger.warning("Model loading already in progress, ignoring new request")
            return None

        self._progress_callback = progress_callback
        thread = threading.Thread(target=_background_loader, daemon=True)
        self._loading_thread = thread
        thread.start()
        return thread

    def _safe_progress(self, callback: Callable[[str], None] | None, message: str) -> None:
        if not callback:
            return
        try:
            callback(message)
        except Exception:
            self.logger.exception("Model progress callback failed")

    def is_loading(self) -> bool:
        return self._loading_thread is not None and self._loading_thread.is_alive()

    def transcribe_audio(self, audio_data: np.ndarray) -> str | None:
        self.last_transcription_metrics = {"status": "not_started"}
        self._last_script_retry = False
        if self.model is None:
            self.last_transcription_metrics = {"status": "model_unavailable"}
            return None

        if audio_data is None or len(audio_data) == 0:
            self.logger.warning("No audio data to transcribe")
            self.last_transcription_metrics = {"status": "empty_audio"}
            return None
        audio_duration_ms = round(len(audio_data) / 16_000 * 1000)
        started = None
        try:
            speech_detected = True
            if self.vad_manager and self.vad_manager.is_available():
                speech_detected = self.vad_manager.check_audio_for_speech(audio_data)

            if not speech_detected:
                print("   ✗ No speech detected, skipping transcription")
                self.last_transcription_metrics = {
                    "status": "no_speech",
                    "audio_duration_ms": audio_duration_ms,
                }
                return None

            started = time.perf_counter()
            inference_index = getattr(self, "transcription_count", 0) + 1
            self.transcription_count = inference_index

            # Prep audio for faster-whisper
            if len(audio_data.shape) > 1:
                audio_data = audio_data.flatten()

            audio_data = audio_data.astype(np.float32)

            transcribe_kwargs = dict(
                beam_size=self.beam_size,
                language=self.language,
                task="transcribe",
                condition_on_previous_text=False,
                multilingual=self.language is None,
                language_detection_segments=3,
            )
            if self.initial_prompt:
                transcribe_kwargs["initial_prompt"] = self.initial_prompt
            if self.hotwords:
                transcribe_kwargs["hotwords"] = self.hotwords

            segments, info = self.model.transcribe(audio_data, **transcribe_kwargs)

            transcribed_text = ""
            for segment in segments:
                transcribed_text += segment.text

            transcribed_text = transcribed_text.strip()
            transcribed_text, info = self.ensure_detected_language_script(
                audio_data,
                transcribed_text,
                info,
                transcribe_kwargs,
            )

            if self.strip_trailing_period and transcribed_text.endswith("."):
                transcribed_text = transcribed_text[:-1]

            transcription_time = time.perf_counter() - started
            print(f"   ✓ Transcription completed in {transcription_time:.1f} seconds")

            # Log some info about what we transcribed
            detected_language = info.language
            confidence = info.language_probability
            processing_ms = round(transcription_time * 1000)
            real_time_factor = transcription_time / max(0.001, audio_duration_ms / 1000)
            self.last_transcription_metrics = {
                "status": "complete" if transcribed_text else "empty_transcript",
                "detected_language": detected_language,
                "language_probability": round(float(confidence), 4),
                "processing_ms": processing_ms,
                "audio_duration_ms": audio_duration_ms,
                "real_time_factor": round(real_time_factor, 4),
                "model": self.model_key,
                "device": self.device,
                "compute_type": self.compute_type,
                "model_load_ms": getattr(self, "model_load_ms", None),
                "inference_index": inference_index,
                "cold_inference": inference_index == 1,
                "script_retry": self._last_script_retry,
            }
            self.logger.info(
                "Transcription complete. Language: %s (confidence: %.2f) - Time: %.2fs",
                detected_language,
                confidence,
                transcription_time,
            )
            if self.log_transcriptions:
                self.logger.info(f"Transcribed text: '{transcribed_text}'")
            else:
                self.logger.info(f"Transcribed {len(transcribed_text)} chars")

            if transcribed_text:
                print(f"   ✓ Transcribed: '{transcribed_text}'")
                return transcribed_text
            else:
                self.logger.info("Transcription was empty")
                return None

        except Exception as e:
            self.logger.error(f"Transcription failed: {e}")
            processing_ms = round((time.perf_counter() - started) * 1000) if started is not None else 0
            self.last_transcription_metrics = {
                "status": "failed",
                "audio_duration_ms": audio_duration_ms,
                "processing_ms": processing_ms,
                "model": self.model_key,
                "device": self.device,
                "compute_type": self.compute_type,
                "model_load_ms": getattr(self, "model_load_ms", None),
                "inference_index": locals().get("inference_index"),
                "cold_inference": locals().get("inference_index") == 1,
                "error": str(e),
            }
            return None

    def ensure_detected_language_script(self, audio_data, text, info, transcribe_kwargs=None):
        """Retry confidently detected Russian speech when output lost its Cyrillic script.

        Whisper's language detector and decoder are separate.  On a short multilingual
        utterance the detector can correctly report Russian while an English prompt still
        steers the decoder toward translated Latin text.  A forced-language retry keeps
        the original speech language without changing normal ES/EN auto-detection.
        """
        detected = getattr(info, "language", None)
        probability = float(getattr(info, "language_probability", 0.0) or 0.0)
        if detected != "ru" or probability < 0.70 or self._cyrillic_ratio(text) >= 0.20:
            return text, info

        retry_kwargs = dict(transcribe_kwargs or {})
        retry_kwargs.update(
            {
                "beam_size": self.beam_size,
                "language": "ru",
                "task": "transcribe",
                "condition_on_previous_text": False,
                "multilingual": False,
                "initial_prompt": ("Русская речь. Записывай дословно по-русски кириллицей; не переводи на английский."),
            }
        )
        retry_kwargs.pop("hotwords", None)
        self.logger.warning(
            "Russian was detected with %.2f confidence but the decoded text was not Cyrillic; retrying",
            probability,
        )
        retry_segments, retry_info = self.model.transcribe(audio_data, **retry_kwargs)
        retry_text = "".join(segment.text for segment in retry_segments).strip()
        if self._cyrillic_ratio(retry_text) > self._cyrillic_ratio(text):
            self._last_script_retry = True
            return retry_text, retry_info
        return text, info

    @staticmethod
    def _cyrillic_ratio(text: str) -> float:
        letters = [character for character in text if character.isalpha()]
        if not letters:
            return 0.0
        cyrillic = sum("\u0400" <= character <= "\u052f" for character in letters)
        return cyrillic / len(letters)

    def change_model(self, new_model_key: str, progress_callback: Callable[[str], None] | None = None):

        if new_model_key == self.model_key:
            if progress_callback:
                progress_callback("Model already loaded")
            return

        return self._load_model_async(new_model_key, progress_callback)
