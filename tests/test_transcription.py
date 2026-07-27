from types import SimpleNamespace

from yt_pro_max.transcription import WhisperTranscriber


class FakeModel:
    calls = []

    def __init__(self, name, *, device, compute_type):
        self.device = device
        FakeModel.calls.append((name, device, compute_type))

    def transcribe(self, path, **kwargs):
        if self.device == "cuda":
            raise RuntimeError("CUDA library unavailable")
        word = SimpleNamespace(word=" hello", start=0.1, end=0.4, probability=0.9)
        segment = SimpleNamespace(text=" hello", start=0.0, end=0.5, words=[word])
        info = SimpleNamespace(language="en", language_probability=0.95)
        return iter([segment]), info


def test_transcriber_falls_back_to_cpu_after_cuda_transcription_error(
    settings, monkeypatch, tmp_path
):
    FakeModel.calls = []
    monkeypatch.setattr("faster_whisper.WhisperModel", FakeModel)
    transcriber = WhisperTranscriber(settings)

    result = transcriber.transcribe(tmp_path / "audio.webm")

    assert result.language == "en"
    assert result.segments[0].words[0].text == "hello"
    assert "GPU_TRANSCRIPTION_FAILED_CPU_FALLBACK" in result.warnings
    assert [call[1] for call in FakeModel.calls] == ["cuda", "cpu"]


def test_transcriber_raises_when_no_speech(settings, monkeypatch, tmp_path):
    class SilentModel(FakeModel):
        def transcribe(self, path, **kwargs):
            return iter([]), SimpleNamespace(language="en", language_probability=0.99)

    monkeypatch.setattr("faster_whisper.WhisperModel", SilentModel)
    transcriber = WhisperTranscriber(settings)

    from yt_pro_max.errors import PipelineError

    try:
        transcriber.transcribe(tmp_path / "audio.webm")
    except PipelineError as error:
        assert error.info.code == "NO_SPEECH_DETECTED"
    else:
        raise AssertionError("expected NO_SPEECH_DETECTED")
