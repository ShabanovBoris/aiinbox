"""Resolve the response language contract shared by prompt and deterministic repair."""

from app.domain.enums import SourceType
from app.domain.models import NormalizedContent, UserProfile

_VIDEO_SOURCE_TYPES = (SourceType.VIDEO, SourceType.YOUTUBE)
_VIDEO_SOURCE_VALUES = {source_type.value for source_type in _VIDEO_SOURCE_TYPES}


def includes_video_source(content: NormalizedContent) -> bool:
    """Video sources answer in the profile language; text/web follow the content.

    An Instagram Item may hold a Reel, a photo carousel, or a text post, so the
    source type alone cannot decide the language. Extraction records the media
    kind in ``instagram_has_video``; a missing flag means no video was extracted.
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
