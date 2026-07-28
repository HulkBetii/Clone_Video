import builtins
import os
from types import SimpleNamespace

from yt_pro_max.transcription import WhisperTranscriber


class FakeModel:
    calls = []
    transcribe_calls = []

    def __init__(self, name, *, device, compute_type):
        self.device = device
        FakeModel.calls.append((name, device, compute_type))

    def transcribe(self, path, **kwargs):
        FakeModel.transcribe_calls.append((self.device, kwargs))
        if self.device == "cuda":
            raise RuntimeError("CUDA library unavailable")
        word = SimpleNamespace(word=" hello", start=0.1, end=0.4, probability=0.9)
        segment = SimpleNamespace(text=" hello", start=0.0, end=0.5, words=[word])
        info = SimpleNamespace(language="en", language_probability=0.95)
        return iter([segment]), info

    def detect_language(self, **kwargs):
        return "ja", 0.96, [("ja", 0.96)]


def test_transcriber_falls_back_to_cpu_after_cuda_transcription_error(
    settings, monkeypatch, tmp_path
):
    FakeModel.calls = []
    FakeModel.transcribe_calls = []
    monkeypatch.setattr("faster_whisper.WhisperModel", FakeModel)
    transcriber = WhisperTranscriber(settings)

    result = transcriber.transcribe(tmp_path / "audio.webm")

    assert result.language == "en"
    assert result.segments[0].words[0].text == "hello"
    assert "GPU_TRANSCRIPTION_FAILED_CPU_FALLBACK" in result.warnings
    assert [call[1] for call in FakeModel.calls] == ["cuda", "cpu"]


def test_transcriber_forwards_requested_language_and_preserves_it_on_cpu_fallback(
    settings, monkeypatch, tmp_path
):
    FakeModel.calls = []
    FakeModel.transcribe_calls = []
    monkeypatch.setattr("faster_whisper.WhisperModel", FakeModel)
    monkeypatch.setattr(
        "yt_pro_max.transcription._load_language_sample", lambda _path: object()
    )
    transcriber = WhisperTranscriber(settings)

    transcriber.transcribe(tmp_path / "audio.webm", language="ja")

    assert [call[1].get("language") for call in FakeModel.transcribe_calls] == ["ja", "ja"]


def test_transcriber_rejects_detected_language_mismatch(settings, monkeypatch, tmp_path):
    class MismatchedLanguageModel(FakeModel):
        def detect_language(self, **kwargs):
            return "en", 0.91, [("en", 0.91)]

    monkeypatch.setattr("faster_whisper.WhisperModel", MismatchedLanguageModel)
    monkeypatch.setattr(
        "yt_pro_max.transcription._load_language_sample", lambda _path: object()
    )
    transcriber = WhisperTranscriber(settings)

    from yt_pro_max.errors import PipelineError

    try:
        transcriber.transcribe(tmp_path / "audio.webm", language="ja")
    except PipelineError as error:
        assert error.info.code == "TRANSCRIPTION_LANGUAGE_MISMATCH"
        assert error.info.details["detected_language"] == "en"
    else:
        raise AssertionError("expected TRANSCRIPTION_LANGUAGE_MISMATCH")


def test_transcriber_maps_missing_faster_whisper_to_model_load_failure(
    settings, monkeypatch
):
    original_import = builtins.__import__

    def fail_faster_whisper(name, *args, **kwargs):
        if name == "faster_whisper":
            raise ModuleNotFoundError(name)
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fail_faster_whisper)
    transcriber = WhisperTranscriber(settings)

    from yt_pro_max.errors import PipelineError

    try:
        transcriber._load_model()
    except PipelineError as error:
        assert error.info.code == "MODEL_LOAD_FAILED"
    else:
        raise AssertionError("expected MODEL_LOAD_FAILED")


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


def test_configured_cuda_dll_directory_is_registered(settings, monkeypatch, tmp_path):
    (tmp_path / "cublas64_12.dll").touch()
    (tmp_path / "cudnn64_9.dll").touch()
    settings.cuda_dll_dir = tmp_path
    original_path = os.environ.get("PATH", "")
    fake_handle = object()
    monkeypatch.setattr("yt_pro_max.transcription.os.add_dll_directory", lambda path: fake_handle)
    transcriber = WhisperTranscriber(settings)

    assert transcriber._cuda_dll_handle is fake_handle
    assert os.environ["PATH"].split(os.pathsep)[0] == str(tmp_path)
    monkeypatch.setenv("PATH", original_path)
