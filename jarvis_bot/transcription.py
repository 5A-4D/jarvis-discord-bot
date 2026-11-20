from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path
from typing import Any, Callable


class VoiceTranscriptionError(RuntimeError):
    """Raised when a Discord voice note cannot be transcribed locally."""


class LocalVoiceTranscriber:
    """Lazily transcribe audio with a local faster-whisper model."""

    def __init__(
        self,
        *,
        model_name: str,
        device: str,
        compute_type: str,
        model_factory: Callable[..., Any] | None = None,
    ) -> None:
        self.model_name = model_name
        self.device = device
        self.compute_type = compute_type
        self._model_factory = model_factory
        self._model: Any | None = None
        self._lock = asyncio.Lock()

    async def transcribe(self, audio: bytes, *, suffix: str = ".ogg") -> str:
        if not audio:
            raise VoiceTranscriptionError("The voice note contained no audio data.")

        async with self._lock:
            try:
                return await asyncio.to_thread(self._transcribe_sync, audio, suffix)
            except VoiceTranscriptionError:
                raise
            except Exception as error:
                raise VoiceTranscriptionError(str(error)) from error

    def _load_model(self) -> Any:
        if self._model is None:
            model_factory = self._model_factory
            if model_factory is None:
                try:
                    from faster_whisper import WhisperModel
                except ImportError as error:
                    raise VoiceTranscriptionError(
                        "faster-whisper is not installed."
                    ) from error
                model_factory = WhisperModel
            self._model = model_factory(
                self.model_name,
                device=self.device,
                compute_type=self.compute_type,
            )
        return self._model

    def _transcribe_sync(self, audio: bytes, suffix: str) -> str:
        safe_suffix = Path(suffix).suffix or ".ogg"
        with tempfile.NamedTemporaryFile(suffix=safe_suffix) as temporary_audio:
            temporary_audio.write(audio)
            temporary_audio.flush()
            segments, _ = self._load_model().transcribe(
                temporary_audio.name,
                beam_size=5,
                vad_filter=True,
            )
            transcript = " ".join(
                str(segment.text).strip() for segment in segments if segment.text.strip()
            ).strip()

        if not transcript:
            raise VoiceTranscriptionError("No intelligible speech was detected.")
        return transcript
