"""Persist Instagram post evidence one element at a time and rebuild the source on resume."""

from collections.abc import Awaitable, Callable
from pathlib import Path
from urllib.parse import urlsplit
from uuid import uuid4

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.enums import ContentKind, SourceType
from app.domain.models import NormalizedContent
from app.errors import AppError
from app.extractors.instagram import InstagramExtractor
from app.extractors.instagram_post_media import POST_DOWNLOAD_BYTES
from app.extractors.subprocess_runner import cleanup_temporary_directory
from app.llm.base import LlmProvider, TranscriptionSegmentCheckpoint
from app.services.profile import get_profile
from app.storage.models import Content, ItemSource

STTCheckpoints = tuple[
    dict[int, TranscriptionSegmentCheckpoint],
    Callable[[int, TranscriptionSegmentCheckpoint], Awaitable[None]],
]

_EVIDENCE_KINDS = (ContentKind.TRANSCRIPT, ContentKind.VISUAL_NOTES)


def _canonical_post_url(source: ItemSource) -> str:
    # Keep share parameters on the original link, outside the analysis payload.
    return urlsplit(source.source_url)._replace(query="", fragment="").geturl()


async def _post_rows(session: AsyncSession, source: ItemSource) -> list[Content]:
    return list(
        await session.scalars(
            select(Content)
            .where(
                Content.source_id == source.id,
                Content.kind.in_(
                    (*_EVIDENCE_KINDS, ContentKind.DESCRIPTION, ContentKind.TRANSCRIPT_CHUNK)
                ),
            )
            .order_by(Content.id)
        )
    )


def _evidence(rows: list[Content]) -> dict[tuple[str, ContentKind], str]:
    return {
        ((row.metadata_json or {}).get("instagram_element"), row.kind): row.text
        for row in rows
        if row.kind in _EVIDENCE_KINDS and (row.metadata_json or {}).get("instagram_element")
    }


def _project_post(
    source: ItemSource,
    progress: dict,
    evidence: dict[tuple[str, ContentKind], str],
    caption: str,
) -> NormalizedContent | None:
    sections = []
    has_transcript = has_visual = missing_video_visual = False
    for part in progress["parts"]:
        transcript = evidence.get((part["key"], ContentKind.TRANSCRIPT))
        visual = evidence.get((part["key"], ContentKind.VISUAL_NOTES))
        if not (transcript or visual):
            continue
        has_transcript |= bool(transcript)
        has_visual |= bool(visual)
        missing_video_visual |= part["kind"] == "video" and not visual
        lines = [f"Элемент {part['position']} — {'фото' if part['kind'] == 'photo' else 'видео'}"]
        if transcript:
            lines.append(f"Расшифровка речи: {transcript}")
        if visual:
            lines.append(f"Визуальное описание: {visual}")
        sections.append("\n".join(lines))

    caption_only = not sections
    if caption_only and len(caption) < 40:
        return None
    return NormalizedContent(
        source_type=SourceType.INSTAGRAM,
        url=_canonical_post_url(source),
        title=progress.get("title"),
        author=progress.get("author"),
        text=caption if caption_only else "\n\n".join(sections),
        source_context=caption if caption and not caption_only else None,
        duration_seconds=source.content_duration_seconds,
        metadata={
            "caption_only": caption_only,
            "visual_only": has_visual and not has_transcript,
            "instagram_post_partial": progress["processed"] < progress["total"],
            "instagram_post_visual": has_visual and not missing_video_visual,
        },
    )


async def restore_post(session: AsyncSession, source: ItemSource) -> NormalizedContent | None:
    progress = source.instagram_post_progress
    if not progress or not progress.get("complete"):
        return None
    rows = await _post_rows(session, source)
    caption = next((row.text for row in rows if row.kind is ContentKind.DESCRIPTION), "")
    return _project_post(source, progress, _evidence(rows), caption)


def _failure(code: str, permanent: bool = False) -> dict:
    return {"code": code, "permanent": permanent}


def _file(directory: Path, name: str | None) -> Path:
    if not name:
        raise AppError("DOWNLOAD_FAILED", "Instagram worker did not return a media file")
    candidate = directory / name
    if not candidate.is_file() or not candidate.resolve().is_relative_to(directory.resolve()):
        raise AppError("DOWNLOAD_FAILED", "Instagram worker returned an invalid media file")
    return candidate


def _save_progress(source: ItemSource, progress: dict) -> None:
    source.metadata_json = {**(source.metadata_json or {}), "instagram_post": progress}


async def extract_post(
    session: AsyncSession,
    source: ItemSource,
    extractor: InstagramExtractor,
    provider: LlmProvider,
    *,
    user_id: int,
    checkpoints: Callable[[str], Awaitable[STTCheckpoints]],
    clear_checkpoints: Callable[[str], Awaitable[None]],
    frame_interval: int,
    max_frames: int,
    scene_threshold: float,
) -> NormalizedContent:
    """Use the media adapter for bytes and keep the post's durable progress in SQLite."""
    previous = source.instagram_post_progress or {}
    try:
        post = await extractor.get_post(source.source_url)
    except AppError as exc:
        if previous:
            rows = await _post_rows(session, source)
            caption = next((row.text for row in rows if row.kind is ContentKind.DESCRIPTION), "")
            content = _project_post(source, previous, _evidence(rows), caption)
            if content is not None:
                _save_progress(
                    source,
                    {
                        **previous,
                        "complete": True,
                        "refresh_error": _failure(exc.code, exc.permanent),
                    },
                )
                await session.commit()
                return content
        raise
    metadata = post["metadata"]
    caption = (metadata.get("description") or metadata.get("caption") or "").strip()
    old_parts = {part["key"]: part for part in previous.get("parts", [])}
    parts = [
        {
            **old_parts.get(f"{position}:{element['id']}:{element['kind']}", {}),
            "key": f"{position}:{element['id']}:{element['kind']}",
            "position": position,
            "kind": element["kind"],
        }
        for position, element in enumerate(post["elements"], 1)
    ]
    progress = {
        "total": post["total"],
        "processed": 0,
        "title": (metadata.get("title") or "").strip() or None,
        "author": (metadata.get("uploader") or metadata.get("creator") or "").strip() or None,
        "parts": parts,
        "complete": False,
        "downloaded_bytes": previous.get("downloaded_bytes", 0),
    }
    rows = await _post_rows(session, source)
    valid_keys = {part["key"] for part in parts}
    obsolete = [
        row.id
        for row in rows
        if row.kind is ContentKind.DESCRIPTION
        or (row.metadata_json or {}).get("instagram_element") not in valid_keys
    ]
    if obsolete:
        await session.execute(delete(Content).where(Content.id.in_(obsolete)))
    evidence = {key: value for key, value in _evidence(rows).items() if key[0] in valid_keys}
    if caption:
        session.add(
            Content(
                item_id=source.item_id,
                source_id=source.id,
                kind=ContentKind.DESCRIPTION,
                text=caption,
                metadata_json={
                    "title": progress["title"],
                    "creator": progress["author"],
                    "instagram_id": metadata["id"],
                    "canonical_url": _canonical_post_url(source),
                },
            )
        )
    _save_progress(source, progress)
    vision = provider.capabilities.vision
    language = (await get_profile(session, user_id)).preferred_language if vision else ""
    await session.commit()

    for index, (part, element) in enumerate(zip(parts, post["elements"], strict=True)):
        # Each replacement gets its own JSON value for SQLAlchemy change tracking.
        if part.get("done"):
            continue
        remaining = POST_DOWNLOAD_BYTES - progress["downloaded_bytes"]
        if remaining <= 0:
            progress = {**progress, "budget_exhausted": True}
            break
        outcome, downloaded = await _extract_element(
            session,
            source,
            extractor,
            provider,
            part,
            element,
            evidence,
            checkpoints=checkpoints,
            clear_checkpoints=clear_checkpoints,
            language=language,
            caption=caption,
            vision=vision,
            frame_interval=frame_interval,
            max_frames=max_frames,
            scene_threshold=scene_threshold,
            single=post["total"] == 1,
            download_bytes_remaining=remaining,
        )
        parts = [*parts[:index], outcome, *parts[index + 1 :]]
        budget_exhausted = any(
            error["code"] == "POST_BUDGET_EXHAUSTED" for error in outcome["errors"].values()
        )
        progress = {
            **progress,
            "parts": parts,
            "processed": sum(p.get("done", False) for p in parts),
            "downloaded_bytes": progress["downloaded_bytes"] + downloaded,
            "budget_exhausted": budget_exhausted,
        }
        _save_progress(source, progress)
        await session.commit()
        if budget_exhausted:
            break

    progress = {**progress, "processed": sum(p.get("done", False) for p in parts), "complete": True}
    _save_progress(source, progress)
    content = _project_post(source, progress, evidence, caption)
    if content is not None:
        await session.commit()
        return content
    failures = [error for part in parts for error in part.get("errors", {}).values()]
    first = next((error for error in failures if not error["permanent"]), None)
    first = first or (failures[0] if failures else _failure("EXTRACTION_FAILED", True))
    raise AppError(first["code"], "Instagram post has no analyzable content", first["permanent"])


async def _extract_element(
    session: AsyncSession,
    source: ItemSource,
    extractor: InstagramExtractor,
    provider: LlmProvider,
    part: dict,
    element: dict,
    evidence: dict[tuple[str, ContentKind], str],
    *,
    checkpoints: Callable[[str], Awaitable[STTCheckpoints]],
    clear_checkpoints: Callable[[str], Awaitable[None]],
    language: str,
    caption: str,
    vision: bool,
    frame_interval: int,
    max_frames: int,
    scene_threshold: float,
    single: bool,
    download_bytes_remaining: int,
) -> tuple[dict, int]:
    key = part["key"]
    photo = part["kind"] == "photo"
    transcript = evidence.get((key, ContentKind.TRANSCRIPT))
    visual_notes = evidence.get((key, ContentKind.VISUAL_NOTES))
    no_speech = part.get("no_speech", False)
    errors = dict(part.get("errors", {}))
    downloaded = 0
    if transcript:
        errors.pop("audio", None)
    if visual_notes:
        errors.pop("visual", None)

    async def save(kind: ContentKind, text: str) -> None:
        session.add(
            Content(
                item_id=source.item_id,
                source_id=source.id,
                kind=kind,
                text=text,
                metadata_json={"instagram_element": key, "position": part["position"]},
            )
        )
        evidence[key, kind] = text
        if kind is ContentKind.TRANSCRIPT:
            await clear_checkpoints(key)
        await session.commit()

    if part["kind"] not in {"photo", "video"}:
        errors["media"] = _failure("UNSUPPORTED_SOURCE", True)
    else:
        audio = (
            not photo
            and not transcript
            and not no_speech
            and not errors.get("audio", {}).get("permanent")
        )
        visual = vision and not visual_notes and not errors.get("visual", {}).get("permanent")
        if audio or visual:
            directory = Path(extractor.temp_dir) / f"ig-post-{uuid4().hex}"
            try:
                media = await extractor.prepare_post_element(
                    element,
                    directory,
                    audio=audio,
                    visual=visual,
                    frame_interval=frame_interval,
                    max_frames=max_frames,
                    scene_threshold=scene_threshold,
                    download_bytes_remaining=download_bytes_remaining,
                )
                downloaded = media.get("downloaded_bytes", 0)
                if error := media.get("media_error"):
                    raise AppError(error["code"], "Instagram media unavailable", error["permanent"])
                if single and media.get("duration"):
                    source.content_duration_seconds = media["duration"]
                if audio:
                    try:
                        if error := media.get("audio_error"):
                            raise AppError(
                                error["code"], "Instagram audio unavailable", error["permanent"]
                            )
                        completed, persist = await checkpoints(key)
                        transcript = (
                            await extractor.transcriber.transcribe(
                                _file(directory, media.get("audio")),
                                duration_seconds=media.get("duration"),
                                completed_segments=completed,
                                on_segment=persist,
                            )
                        ).strip()
                        if not transcript:
                            raise AppError("EMPTY_TRANSCRIPT", "Instagram video has no speech")
                        await save(ContentKind.TRANSCRIPT, transcript)
                        errors.pop("audio", None)
                    except AppError as exc:
                        if exc.code in {"NO_AUDIO_TRACK", "EMPTY_TRANSCRIPT"}:
                            no_speech = True
                            await clear_checkpoints(key)
                            await session.commit()
                            errors.pop("audio", None)
                        else:
                            errors["audio"] = _failure(exc.code, exc.permanent)

                if visual:
                    try:
                        if error := media.get("visual_error"):
                            raise AppError(
                                error["code"], "Instagram visuals unavailable", error["permanent"]
                            )
                        images = [_file(directory, image) for image in media["images"]]
                        notes = (
                            await provider.describe_images(
                                images,
                                context=(transcript or caption)[:1500] or None,
                                preferred_language=language,
                                photo=photo,
                            )
                        ).strip()[:800]
                        if not notes:
                            raise AppError("VISUAL_FAILED", "Instagram visual notes were empty")
                        visual_notes = notes
                        await save(ContentKind.VISUAL_NOTES, notes)
                        errors.pop("visual", None)
                    except AppError as exc:
                        errors["visual"] = _failure(exc.code, exc.permanent)
            except AppError as exc:
                for channel, requested in (("audio", audio), ("visual", visual)):
                    if requested:
                        errors[channel] = _failure(exc.code, exc.permanent)
            finally:
                cleanup_temporary_directory(directory)

        if transcript and errors.get("visual", {}).get("code") == "VISION_UNAVAILABLE":
            errors.pop("visual")
        if (
            not vision
            and not (transcript or visual_notes)
            and (photo or no_speech or errors.get("audio", {}).get("permanent"))
        ):
            errors.setdefault("visual", _failure("VISION_UNAVAILABLE"))
    return {
        **part,
        "no_speech": no_speech,
        "errors": errors,
        "done": bool(transcript or visual_notes) and not errors,
    }, downloaded
