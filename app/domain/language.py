"""Resolve the response language contract shared by prompt and deterministic repair."""

from app.domain.enums import SourceType
from app.domain.models import NormalizedContent, UserProfile

_VIDEO_SOURCE_TYPES = (SourceType.VIDEO, SourceType.YOUTUBE, SourceType.INSTAGRAM)
_VIDEO_SOURCE_VALUES = {source_type.value for source_type in _VIDEO_SOURCE_TYPES}


def includes_video_source(content: NormalizedContent) -> bool:
    """Video sources answer in the profile language; text/web follow the content."""
    if content.source_type in _VIDEO_SOURCE_TYPES:
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
