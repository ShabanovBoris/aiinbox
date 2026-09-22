import pytest
from pydantic import ValidationError

from app.config import Settings


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("processing_concurrency", 0),
        ("processing_poll_seconds", 0),
        ("processing_timeout_seconds", 0),
        ("shutdown_timeout_seconds", 0),
        ("backup_keep", 0),
        ("web_timeout_seconds", 0),
        ("max_download_bytes", 0),
        ("max_redirects", 0),
        ("web_max_attempts", 0),
        ("web_backoff_seconds", -0.1),
        ("max_audio_bytes", 0),
        ("transcription_timeout_seconds", 0),
        ("youtube_max_duration_seconds", 0),
        ("youtube_max_audio_bytes", 0),
        ("youtube_max_video_bytes", 0),
        ("youtube_max_subtitle_bytes", 0),
        ("video_frame_interval_seconds", 0),
        ("video_max_frames", 0),
        ("llm_timeout_seconds", 0),
    ],
)
def test_invalid_numeric_settings_fail_fast(field, value):
    with pytest.raises(ValidationError):
        Settings(_env_file=None, **{field: value})


def test_chunk_overlap_must_be_smaller_than_chunk_size():
    with pytest.raises(ValidationError, match="CONTENT_CHUNK_OVERLAP_CHARS"):
        Settings(
            _env_file=None,
            CONTENT_CHUNK_MAX_CHARS=1000,
            CONTENT_CHUNK_OVERLAP_CHARS=1000,
        )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("default_timezone", "Not/AZone"),
        ("allowed_telegram_user_ids", "42,nope"),
        ("allowed_telegram_user_ids", "-42"),
        ("llm_provider", "unknown"),
    ],
)
def test_invalid_enum_and_identity_settings_fail_fast(field, value):
    with pytest.raises(ValidationError):
        Settings(_env_file=None, **{field: value})
