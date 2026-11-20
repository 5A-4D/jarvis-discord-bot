import io
import unittest
import wave
from pathlib import Path
from types import SimpleNamespace

from jarvis_bot.transcription import LocalVoiceTranscriber, VoiceTranscriptionError

try:
    from faster_whisper.audio import decode_audio
except ImportError:
    decode_audio = None


@unittest.skipIf(decode_audio is None, "faster-whisper is not installed")
class AudioDependencyCompatibilityTests(unittest.TestCase):
    def test_real_decoder_accepts_pcm_wav(self) -> None:
        # Exercise the installed dependency, not a mock: PyAV 19 rejects the
        # metadata_errors argument used by faster-whisper 1.2.1.
        audio = io.BytesIO()
        with wave.open(audio, "wb") as wav:
            wav.setnchannels(1)
            wav.setsampwidth(2)
            wav.setframerate(16000)
            wav.writeframes(b"\x00\x00" * 16000)
        audio.seek(0)
        decoded = decode_audio(audio)
        self.assertEqual(decoded.shape, (16000,))
        self.assertEqual(str(decoded.dtype), "float32")
        self.assertTrue((decoded == 0).all())


class FakeWhisperModel:
    def __init__(self, model_name: str, *, device: str, compute_type: str) -> None:
        self.settings = (model_name, device, compute_type)

    def transcribe(self, path: str, **kwargs: object):
        self.audio = Path(path).read_bytes()
        self.options = kwargs
        return (
            [SimpleNamespace(text=" Jarvis,"), SimpleNamespace(text=" status report. ")],
            SimpleNamespace(language="en"),
        )


class LocalVoiceTranscriberTests(unittest.IsolatedAsyncioTestCase):
    async def test_transcribes_audio_and_normalizes_segments(self) -> None:
        created: list[FakeWhisperModel] = []

        def factory(*args: object, **kwargs: object) -> FakeWhisperModel:
            model = FakeWhisperModel(*args, **kwargs)
            created.append(model)
            return model

        transcriber = LocalVoiceTranscriber(
            model_name="base.en",
            device="cpu",
            compute_type="int8",
            model_factory=factory,
        )

        transcript = await transcriber.transcribe(b"not-real-audio")

        self.assertEqual(transcript, "Jarvis, status report.")
        self.assertEqual(created[0].settings, ("base.en", "cpu", "int8"))
        self.assertEqual(created[0].audio, b"not-real-audio")
        self.assertTrue(created[0].options["vad_filter"])

    async def test_rejects_empty_audio(self) -> None:
        transcriber = LocalVoiceTranscriber(
            model_name="base.en",
            device="cpu",
            compute_type="int8",
            model_factory=FakeWhisperModel,
        )
        with self.assertRaises(VoiceTranscriptionError):
            await transcriber.transcribe(b"")


if __name__ == "__main__":
    unittest.main()
