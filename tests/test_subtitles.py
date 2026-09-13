"""Unit-тесты парсера субтитров VTT/SRT (локальные фикстуры, без сети)."""

from app.services.subtitles import parse_subtitles

VTT = """WEBVTT
Kind: captions
Language: ru

1
00:00:01.000 --> 00:00:04.000
< c >Привет, это первое предложение.

2
00:00:04.000 --> 00:00:07.500
Второе предложение с <i>тегами</i>.

NOTE это комментарий

00:00:07.500 --> 00:00:09.000
Первое предложение.
"""

SRT = """1
00:00:01,000 --> 00:00:04,000
Привет, это первое предложение.

2
00:00:04,000 --> 00:00:07,500
Второе предложение.
"""


def test_vtt_strips_tags_and_dedups_repeats():
    text, cues = parse_subtitles(VTT)
    assert "Привет, это первое предложение." in text
    assert "Второе предложение с тегами." in text
    assert "WEBVTT" not in text
    assert "NOTE" not in text
    assert len(cues) == 3
    assert cues[0] == (1.0, 4.0)


def test_srt_supported():
    text, cues = parse_subtitles(SRT)
    assert "Привет, это первое предложение." in text
    assert cues[0] == (1.0, 4.0)


def test_empty_subtitles():
    text, cues = parse_subtitles("WEBVTT\n")
    assert text == ""
    assert cues == []
