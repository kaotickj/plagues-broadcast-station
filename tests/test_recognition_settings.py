import pytest

from gnb_player.extraction import ExtractionConfig


def test_recognition_settings_preserve_english_and_require_transcription():
    config = ExtractionConfig.from_dict({})
    assert (config.recognition_language, config.recognition_task, config.recognition_prompt) == ("en", "transcribe", "")
    assert ExtractionConfig.from_dict({"recognition_language": " AUTO "}).recognition_language == "auto"
    assert ExtractionConfig.from_dict({"recognition_language": "FRA"}).recognition_language == "fra"
    with pytest.raises(ValueError, match="translation"):
        ExtractionConfig.from_dict({"recognition_task": "translate"})


@pytest.mark.parametrize("values", [
    {"recognition_language": "English"}, {"recognition_language": "en-US"},
    {"recognition_language": 1}, {"recognition_prompt": "x" * 501},
    {"recognition_prompt": "hidden\x00text"}, {"recognition_prompt": "hidden\x7ftext"}, {"recognition_prompt": False},
])
def test_recognition_settings_reject_invalid_input(values):
    with pytest.raises(ValueError):
        ExtractionConfig.from_dict(values)


def test_explicit_recognition_context_round_trips_without_title_inference():
    config = ExtractionConfig.from_dict({"recognition_prompt": "User supplied context\nSecond line"})
    assert ExtractionConfig.from_dict(config.to_dict()) == config
