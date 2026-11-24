"""Resolve the response language contract shared by prompt and deterministic repair."""

import re
from functools import cache

from app.domain.enums import SourceType
from app.domain.models import NormalizedContent, UserProfile

_VIDEO_SOURCE_TYPES = (SourceType.VIDEO, SourceType.YOUTUBE)
_VIDEO_SOURCE_VALUES = {source_type.value for source_type in _VIDEO_SOURCE_TYPES}

# Links, hashtags and mentions are not prose and often arrive in another language
# than the caption itself, so they must not vote on the detected language.
_MARKUP = re.compile(r"https?://\S+|#\w+|@\w+", re.UNICODE)
# A short caption is where py3langid confuses close languages: correct Russian text
# reads as Bulgarian or Macedonian, correct English as Uzbek or Dutch. Normalized
# probabilities hide that ("Наконец-то пятница" scores 0.53 for Bulgarian), because
# the model rescales its scores by the square root of the text length. The raw
# log-posterior gap grows with the actual evidence, so a wide absolute margin
# abstains on short captions and still publishes ordinary long ones.
_MIN_LANGUAGE_EVIDENCE = 12.0


@cache
def _language_identifier():
    from py3langid.langid import MODEL_FILE, LanguageIdentifier

    return LanguageIdentifier.from_model_file(MODEL_FILE)


def detect_source_language(text: str) -> str | None:
    """Return the language of human-authored source text, or None when unsure."""
    cleaned = " ".join(_MARKUP.sub(" ", text).split())
    if not cleaned:
        return None
    ranked = _language_identifier().rank(cleaned)
    language, score = ranked[0]
    if language == "zxx" or score - ranked[1][1] < _MIN_LANGUAGE_EVIDENCE:
        return None
    return language


def includes_video_source(content: NormalizedContent) -> bool:
    """Video sources answer in the profile language; text/web follow the content.

    An Instagram Item may hold a Reel, a photo carousel, or a text post, so the
    source type alone cannot decide the language. Extraction records whether the
    Item has video in ``instagram_has_video``; a missing flag means it has none.
    """
    if content.source_type in _VIDEO_SOURCE_TYPES:
        return True
    if content.metadata.get("instagram_has_video") is True:
        return True
    successful_source_types = content.metadata.get("successful_source_types")
    return isinstance(successful_source_types, list) and any(
        source_type in _VIDEO_SOURCE_VALUES for source_type in successful_source_types
    )


def response_language_for(content: NormalizedContent, profile: UserProfile) -> str | None:
    """Return the language the model is instructed to answer in, if it is known."""
    if includes_video_source(content):
        return profile.preferred_language
    return content.language or None
