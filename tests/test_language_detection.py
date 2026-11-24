"""The caption detector must name a language only when the caption actually shows one."""

import pytest

from app.domain.language import detect_source_language

RUSSIAN = "Подпись рассказывает, почему этот материал стоит прочитать внимательно."
ENGLISH = "The best coffee in town is brewed slowly every morning."
UKRAINIAN = "Сьогодні я приготував найкращий борщ за рецептом моєї бабусі, і це справді смачно."
GERMAN = "Heute habe ich den besten Kaffee in der Stadt getrunken, und es war wirklich lecker."


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        (ENGLISH, "en"),
        (RUSSIAN, "ru"),
        (UKRAINIAN, "uk"),
        (GERMAN, "de"),
        # Ordinary captions keep publishing a language once there is enough prose.
        ("Сегодня гуляли по набережной и пили кофе", "ru"),
        ("Rainy day in the city", "en"),
        # Markup often carries a language of its own; only the prose may vote.
        (f"{ENGLISH} #кофе #утро", "en"),
        (f"{RUSSIAN} #coffee #morning", "ru"),
        # No usable prose signal: abstain instead of inventing a language.
        ("🔥🔥🔥", None),
        ("ok", None),
        ("#coffee #morning #vibes", None),
        ("https://example.com/page", None),
        ("", None),
    ],
)
def test_caption_language_detection_is_explicit_about_uncertainty(text, expected):
    assert detect_source_language(text) == expected


@pytest.mark.parametrize(
    "text",
    [
        # Short Russian captions: the classifier prefers Bulgarian, Macedonian or
        # Kyrgyz, which is exactly the wrong instruction for the answer language.
        "Наконец-то пятница",
        "Наша новая квартира",
        "Рецепт на заметку",
        "Листайте вправо",
        "Пишите в директ",
        "Читайте в карусели",
        "Сохраняйте в закладки",
        "Ссылка: example.com",
        "Кот снова сломал ёлку",
        "Пишите нам в директ прямо сейчас",
        # Short English captions: the classifier prefers Uzbek, Dutch or a pidgin.
        "Sunday morning vibes",
        "Weekend mood",
        "Homemade pasta for dinner",
        "Amazing coffee recipe",
        # A shout-out to a country name is not a language sample.
        "ESPAÑA",
    ],
)
def test_short_captions_abstain_instead_of_naming_a_related_language(text):
    assert detect_source_language(text) is None


def test_caption_language_detection_is_deterministic():
    assert detect_source_language(ENGLISH) == detect_source_language(ENGLISH) == "en"
