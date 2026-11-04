"""Offline post fixtures exercise yt-dlp's real parser and the Item's durable lifecycle."""

import asyncio
import io
import json
import shutil
import subprocess

import httpx
import pytest
from sqlalchemy import select
from yt_dlp.extractor.instagram import InstagramIE
from yt_dlp.networking import Request, Response

from app.bot.formatting import format_item_failure, format_ready_item
from app.bot.keyboards import item_keyboard
from app.domain.enums import ContentKind, ProcessingStatus, SourceType
from app.errors import AppError
from app.extractors import instagram_post_media as media
from app.extractors import instagram_post_metadata as metadata
from app.extractors.instagram_worker import _compact_element, _with_post_query, post_info
from app.extractors.web import PinningTransport
from app.llm.base import TranscriptionSegmentCheckpoint
from app.services.actions import apply_item_action
from app.services.delivery import enqueue_item_video_delivery
from app.services.url_parsing import instagram_post_shortcode
from app.storage.models import Content, Item, ItemSource
from app.workers.processing import requeue_stale
from tests.fakes import FakeLlmProvider, FakeTranscriber
from tests.test_instagram import ingest_reel, make_worker

POST = "https://www.instagram.com/p/FakePost/?xtok=share%3D%3D"
CDN = "https://scontent.cdninstagram.com"
CAPTION = "Подпись рассказывает, почему этот материал стоит прочитать внимательно."


def photo(number):
    return {
        "pk": number,
        "image_versions2": {"candidates": [{"url": f"{CDN}/{number}.jpg", "width": 1000}]},
    }


def video(number):
    return {
        **photo(number),
        "has_audio": True,
        "video_versions": [{"id": "v", "url": f"{CDN}/{number}.mp4", "height": 720}],
        "video_duration": 21,
    }


@pytest.mark.parametrize(
    ("data", "kinds"),
    [
        (photo(123), ["photo"]),
        (video(123), ["video"]),
        ({"carousel_media": [photo(124), video(125), photo(126)]}, ["photo", "video", "photo"]),
        ({"carousel_media": [photo(i) for i in range(21)]}, ["photo"] * 20),
    ],
)
def test_real_ytdlp_raw_parser_preserves_album_order_and_share_query(monkeypatch, data, kinds):
    product = {"pk": 123, "caption": {"text": CAPTION}, **data}
    page_data = {"xig_polaris_media": {"if_not_gated_logged_out": product}}
    cache = ["RelayPrefetchedStreamCache", 0, [{"__bbox": {"result": {"data": page_data}}}]]
    script = {"require": [[0, 0, 0, [{"__bbox": {"require": [cache]}}]]]}
    body = f'<script type="application/json" data-sjs>{json.dumps(script)}</script>'.encode()
    requests = []

    def open_post(request, *args):
        requests.append(request.url)
        return Response(io.BytesIO(body), request.url, {"Content-Type": "text/html; charset=utf-8"})

    monkeypatch.setattr(InstagramIE, "_real_initialize", lambda self: None)
    monkeypatch.setattr(InstagramIE, "_can_impersonate", False)
    monkeypatch.setattr(InstagramIE, "_download_json", lambda *args, **kwargs: {})
    monkeypatch.setattr(metadata, "open_post_metadata", open_post)
    result = post_info(POST, {"quiet": True, "no_warnings": True})
    assert requests == ["https://www.instagram.com/p/FakePost?xtok=share%3D%3D"]
    assert [part["kind"] for part in result["elements"]] == kinds
    assert result["metadata"]["description"] == CAPTION
    album = data.get("carousel_media")
    if album:
        assert result["total"] == len(album)
        assert [part["url"] for part in result["elements"]] == [
            f"{CDN}/{entry['pk']}.{'mp4' if 'video_versions' in entry else 'jpg'}"
            for entry in album[:20]
        ]


@pytest.mark.parametrize(
    "url",
    [
        "http://instagram.com/p/FakePost",
        "https://www.instagram.com.evil.example/p/FakePost",
        "https://m.instagram.com/p/FakePost",
        "https://name@instagram.com/p/FakePost",
        "https://instagram.com:444/p/FakePost",
        "https://www.instagram.com/p/FakePost/other",
        "https://www.instagram.com/p/%2f",
        "https://www.instagram.com/stories/example/123/",
    ],
)
def test_post_route_rejects_noncanonical_urls(url):
    assert instagram_post_shortcode(url) is None


def test_share_query_is_not_forwarded_to_api_or_another_post():
    for request in (
        Request("https://www.instagram.com/p/Other"),
        Request("https://www.instagram.com/p/FakePost", data=b"other request"),
        Request("https://www.instagram.com/api/graphql"),
        Request("https://external.example/p/FakePost"),
    ):
        assert _with_post_query(request, POST) is request
        assert "xtok" not in request.url


def test_post_photo_uses_highest_resolution_allowed_cdn_variant():
    element = _compact_element(
        {
            "id": "photo",
            "thumbnails": [
                {"width": 1080, "url": f"{CDN}/full.jpg"},
                {"width": 4000, "url": "https://example.org/not-instagram.jpg"},
                {"width": 150, "url": f"{CDN}/tiny.jpg"},
                {"width": 640, "url": f"{CDN}/medium.jpg"},
            ],
        }
    )
    assert element["url"] == f"{CDN}/full.jpg"


@pytest.mark.parametrize("target", ["https://127.0.0.1/media", "https://alias.fbcdn.net/media"])
async def test_media_redirects_check_hosts_and_pinned_ips(tmp_path, target):
    hosts = []

    async def resolver(host):
        return ["8.8.8.8"] if host == "scontent.cdninstagram.com" else ["127.0.0.1"]

    def server(request):
        hosts.append((request.url.host, request.headers["host"]))
        return httpx.Response(302, headers={"location": target})

    transport = PinningTransport(httpx.MockTransport(server), resolver=resolver)
    async with httpx.AsyncClient(transport=transport) as client:
        with pytest.raises(AppError) as failed:
            await media._download(f"{CDN}/photo.jpg", tmp_path / "photo", 8, {"image/jpeg"}, client)
    assert failed.value.code == "SECURITY_REJECTED"
    assert hosts == [("8.8.8.8", "scontent.cdninstagram.com")]
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize(
    "target",
    [
        "https://127.0.0.1/private",
        "https://i.instagram.com/private",
        "https://www.instagram.com/private",
        "http://www.instagram.com/private",
    ],
)
async def test_metadata_redirects_check_origin_and_pinned_ips(target):
    calls = []
    resolutions = []

    async def resolver(host):
        resolutions.append(host)
        return ["8.8.8.8"] if len(resolutions) == 1 else ["127.0.0.1"]

    def server(request):
        calls.append((request.url.host, request.headers["host"]))
        return httpx.Response(302, headers={"location": target})

    transport = PinningTransport(httpx.MockTransport(server), resolver=resolver)
    async with httpx.AsyncClient(transport=transport) as client:
        with pytest.raises(AppError) as failure:
            await metadata._fetch_metadata(client, Request(POST))
    assert failure.value.code == "SECURITY_REJECTED"
    assert calls == [("8.8.8.8", "www.instagram.com")]
    if target == "https://www.instagram.com/private":
        assert resolutions == ["www.instagram.com", "www.instagram.com"]


async def test_metadata_cookies_use_original_host_and_body_is_bounded():
    async def resolver(host):
        return ["8.8.8.8"]

    def server(request):
        if request.url.path.startswith("/p/"):
            return httpx.Response(
                302,
                headers={
                    "location": "/next",
                    "set-cookie": "csrftoken=fixture; Domain=.instagram.com; Path=/; Secure",
                },
            )
        assert request.headers["cookie"] == "csrftoken=fixture"
        return httpx.Response(200, stream=httpx.ByteStream(b"0123456789"))

    transport = PinningTransport(httpx.MockTransport(server), resolver=resolver)
    async with httpx.AsyncClient(transport=transport) as client:
        with pytest.raises(AppError) as failure:
            await metadata._fetch_metadata(client, Request(POST), byte_limit=8)
    assert failure.value.code == "TOO_LARGE"


async def test_media_stream_byte_cap_without_content_length(tmp_path):
    def server(request):
        return httpx.Response(
            200, headers={"content-type": "image/jpeg"}, stream=httpx.ByteStream(b"abcdefghi")
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(server)) as client:
        with pytest.raises(AppError) as failed:
            await media._download(f"{CDN}/photo.jpg", tmp_path / "photo", 8, {"image/jpeg"}, client)
    assert failed.value.code == "TOO_LARGE"
    assert (tmp_path / "photo").stat().st_size <= 8


async def test_media_byte_budget_covers_more_than_one_download(tmp_path):
    def server(request):
        return httpx.Response(
            200, headers={"content-type": "image/jpeg"}, stream=httpx.ByteStream(b"abcdef")
        )

    budget = media._PostBudget(10)
    async with httpx.AsyncClient(transport=httpx.MockTransport(server)) as client:
        await media._download(f"{CDN}/one", tmp_path / "one", 8, {"image/jpeg"}, client, budget)
        with pytest.raises(AppError) as failure:
            await media._download(f"{CDN}/two", tmp_path / "two", 8, {"image/jpeg"}, client, budget)
    assert failure.value.code == "POST_BUDGET_EXHAUSTED"
    assert budget.used == 10


@pytest.mark.skipif(
    not shutil.which("ffmpeg") or not shutil.which("ffprobe"), reason="ffmpeg tools unavailable"
)
@pytest.mark.parametrize("kind", ["photo", "silent", "video", "oversized"])
def test_real_media_preparation(tmp_path, monkeypatch, kind):
    sample = tmp_path / ("sample.png" if kind == "photo" else "sample.mp4")
    seconds = "0.2" if kind == "silent" else "2"
    cmd = ["ffmpeg", "-v", "error", "-f", "lavfi", "-i", f"color=c=red:s=128x128:r=10:d={seconds}"]
    if kind == "photo":
        cmd += ["-frames:v", "1", "-threads", "1"]
    else:
        if kind in {"video", "oversized"}:
            cmd += ["-f", "lavfi", "-i", "sine=frequency=800:sample_rate=16000:d=2", "-c:a", "aac"]
        cmd += ["-c:v", "mpeg4", "-pix_fmt", "yuv420p"]
    subprocess.run([*cmd, str(sample)], check=True, timeout=15, capture_output=True)

    async def fetch(url, path, limit, types, budget):
        if kind == "oversized" and url.endswith("/sample"):
            raise AppError("TOO_LARGE", "video stream too large", True)
        shutil.copyfile(sample, path)

    monkeypatch.setattr(media, "_fetch", fetch)
    directory = tmp_path / "work"
    result = media.prepare_post_media(
        {
            "element": {
                "kind": "photo" if kind == "photo" else "video",
                "url": f"{CDN}/sample",
                "audio_url": f"{CDN}/track" if kind == "oversized" else None,
            },
            "download_dir": str(directory),
            "audio": kind != "photo",
            "visual": True,
            "max_frames": 6,
            "frame_interval": 20,
            "scene_threshold": 0.35,
            "max_duration": 30,
            "video_bytes": 50_000_000,
            "audio_bytes": 50_000_000,
            "download_bytes_remaining": media.POST_DOWNLOAD_BYTES,
        }
    )
    if kind == "oversized":
        assert result["visual_error"] == {"code": "TOO_LARGE", "permanent": True}
        assert not result["images"]
    else:
        assert 1 <= len(result["images"]) <= 6
        assert (directory / result["images"][0]).read_bytes().startswith(b"\xff\xd8\xff")
    if kind in {"video", "oversized"}:
        assert (directory / result["audio"]).stat().st_size > 0
        assert result["duration"] == 2
    if kind == "silent":
        assert result["audio_error"]["code"] == "NO_AUDIO_TRACK"


class FixturePosts:
    def __init__(self, tmp_path, kinds, *, caption=CAPTION):
        self.temp_dir = tmp_path / "post-media"
        self.kinds = kinds
        self.caption = caption
        self.transcriber = FakeTranscriber()
        self.manifest_reads = 0
        self.prepared = []
        self.errors = {}
        self.manifest_error = None
        self.block_on = None
        self.started = asyncio.Event()
        self.bytes_per_element = 0

    async def get_post(self, url):
        assert url == POST
        self.manifest_reads += 1
        if self.manifest_error:
            raise AppError(self.manifest_error, "fixture access failure")
        return {
            "metadata": {"id": "FakePost", "description": self.caption, "uploader": "Author"},
            "total": len(self.kinds),
            "elements": [
                {"id": str(index), "kind": kind} for index, kind in enumerate(self.kinds, 1)
            ],
        }

    async def prepare_post_element(self, element, directory, *, audio, visual, **kwargs):
        identifier = element["id"]
        self.prepared.append((identifier, audio, visual))
        directory.mkdir(parents=True)
        if identifier == self.block_on:
            self.started.set()
            await asyncio.Future()
        if error := self.errors.get(identifier):
            raise error
        if self.bytes_per_element > kwargs["download_bytes_remaining"]:
            return {"media_error": {"code": "POST_BUDGET_EXHAUSTED", "permanent": False}}
        result = {
            "duration": 10 if element["kind"] == "video" else None,
            "images": [],
            "downloaded_bytes": self.bytes_per_element,
        }
        if visual:
            (directory / "photo.jpg").write_text(identifier)
            result["images"] = ["photo.jpg"]
        if audio:
            (directory / "audio.mp3").write_text(identifier)
            result["audio"] = "audio.mp3"
        return result


async def _state(factory, item_id):
    async with factory() as session:
        item = await session.get(Item, item_id)
        source = await session.scalar(select(ItemSource).where(ItemSource.item_id == item_id))
        rows = (await session.scalars(select(Content).where(Content.source_id == source.id))).all()
    return item, source, rows


def _buttons(item, source):
    return [button for row in item_keyboard(item, [source]).inline_keyboard for button in row]


async def test_carousel_budget_returns_partial_and_manual_retry_resumes(tmp_path, session_factory):
    item = await ingest_reel(session_factory, text=POST)
    extractor = FixturePosts(tmp_path, ["photo", "photo"])
    extractor.bytes_per_element = media.POST_DOWNLOAD_BYTES // 2 + 1
    worker = make_worker(session_factory, extractor, FakeLlmProvider(vision=True))
    assert await worker.process_one()
    partial, source, _ = await _state(session_factory, item.id)
    assert partial.analysis_completeness == "PARTIAL"
    assert source.instagram_post_progress["budget_exhausted"]
    assert "Лимит загрузки поста исчерпан" in format_ready_item(partial, [source])
    assert await apply_item_action(session_factory, 42, item.id, "retry")
    assert await worker.process_one()
    ready, source, rows = await _state(session_factory, item.id)
    assert ready.analysis_completeness == "VISUAL_ONLY"
    assert not source.instagram_post_progress["budget_exhausted"]
    assert [call[0] for call in extractor.prepared] == ["1", "2", "2"]
    assert sum(row.kind is ContentKind.VISUAL_NOTES for row in rows) == 2


async def test_carousel_retry_recovers_only_missing_element_and_keeps_open_link(
    tmp_path, session_factory
):
    item = await ingest_reel(session_factory, text=POST)
    provider = FakeLlmProvider(vision=True)
    extractor = FixturePosts(tmp_path, ["photo", "video", "photo"])
    extractor.errors["2"] = AppError("TIMEOUT", "fixture timeout")
    extractor.errors["3"] = AppError("TOO_LARGE", "fixture oversized", True)
    worker = make_worker(session_factory, extractor, provider)
    assert await worker.process_one()

    partial, source, _ = await _state(session_factory, item.id)
    assert source.source_type is SourceType.INSTAGRAM
    assert partial.analysis_completeness == "PARTIAL"
    assert "1 из 3" in format_ready_item(partial, [source])
    assert any(
        button.text == "↗ Открыть в Instagram" and button.url == POST
        for button in _buttons(partial, source)
    )
    assert not any("Прислать" in button.text for button in _buttons(partial, source))
    assert await enqueue_item_video_delivery(session_factory, 42, item.id, source.id) is None

    extractor.manifest_error = "RATE_LIMITED"
    await apply_item_action(session_factory, 42, item.id, "retry")
    assert await worker.process_one()
    still_partial, _, _ = await _state(session_factory, item.id)
    assert still_partial.analysis_completeness == "PARTIAL"

    extractor.manifest_error = None
    del extractor.errors["2"]
    await apply_item_action(session_factory, 42, item.id, "retry")
    assert await worker.process_one()
    final, source, rows = await _state(session_factory, item.id)
    assert final.processing_status is ProcessingStatus.READY
    assert final.analysis_completeness == "PARTIAL"
    assert not any("Повторить" in button.text for button in _buttons(final, source))
    assert [call[0] for call in extractor.prepared] == ["1", "2", "3", "2"]
    assert not list(extractor.temp_dir.iterdir())
    assert sum(row.kind is ContentKind.DESCRIPTION for row in rows) == 1
    analyzed = provider.calls[-1][0]
    assert analyzed.text.index("Элемент 1") < analyzed.text.index("Элемент 2")
    assert "Элемент 3" not in analyzed.text
    assert analyzed.source_context == CAPTION
    assert analyzed.url == "https://www.instagram.com/p/FakePost/"


@pytest.mark.parametrize("caption", [CAPTION, "Коротко"])
async def test_photo_with_no_vision_is_honest_and_retryable(tmp_path, session_factory, caption):
    item = await ingest_reel(session_factory, text=POST)
    extractor = FixturePosts(tmp_path, ["photo"], caption=caption)
    worker = make_worker(session_factory, extractor, FakeLlmProvider())
    assert await worker.process_one()
    saved, source, _ = await _state(session_factory, item.id)
    assert not extractor.prepared
    if caption == CAPTION:
        assert saved.analysis_completeness == "CAPTION_ONLY"
        copy = format_ready_item(saved, [source])
    else:
        assert saved.processing_status is ProcessingStatus.FAILED
        assert saved.error_code == "VISION_UNAVAILABLE"
        copy = format_item_failure(saved, [source])
    assert "модель с поддержкой изображений" in copy
    assert any("Повторить" in button.text for button in _buttons(saved, source))
    await apply_item_action(session_factory, 42, item.id, "retry")
    vision_worker = make_worker(session_factory, extractor, FakeLlmProvider(vision=True))
    assert await vision_worker.process_one()
    ready, _, _ = await _state(session_factory, item.id)
    assert ready.analysis_completeness == "VISUAL_ONLY"


@pytest.mark.parametrize("code", ["AUTH_REQUIRED", "RATE_LIMITED"])
async def test_access_failures_keep_original_and_offer_retry(tmp_path, session_factory, code):
    item = await ingest_reel(session_factory, text=POST)
    extractor = FixturePosts(tmp_path, ["photo"])
    extractor.manifest_error = code
    assert await make_worker(session_factory, extractor, FakeLlmProvider()).process_one()
    saved, source, _ = await _state(session_factory, item.id)
    assert saved.error_code == code
    assert "Instagram" in format_item_failure(saved, [source])
    assert any(button.url == POST for button in _buttons(saved, source))
    assert any("Повторить" in button.text for button in _buttons(saved, source))


async def test_cancel_resume_then_analysis_retry_reuses_completed_media(tmp_path, session_factory):
    item = await ingest_reel(session_factory, text=POST)
    extractor = FixturePosts(tmp_path, ["photo", "photo"])
    extractor.block_on = "2"
    provider = FakeLlmProvider(vision=True, analyze_failures=1)
    worker = make_worker(session_factory, extractor, provider)
    task = asyncio.create_task(worker.process_one())
    await asyncio.wait_for(extractor.started.wait(), timeout=3)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not list(extractor.temp_dir.iterdir())
    assert await requeue_stale(session_factory) == 1
    extractor.block_on = None
    assert await worker.process_one()
    failed, _, _ = await _state(session_factory, item.id)
    assert failed.processing_stage == "ANALYZING"
    assert failed.processing_status is ProcessingStatus.FAILED
    await apply_item_action(session_factory, 42, item.id, "retry")
    assert await worker.process_one()
    saved, _, _ = await _state(session_factory, item.id)
    assert saved.analysis_completeness == "VISUAL_ONLY"
    assert [entry[0] for entry in extractor.prepared] == ["1", "2", "2"]
    assert extractor.manifest_reads == 2


async def test_stt_segments_are_isolated_between_videos_in_one_post(tmp_path, session_factory):
    item = await ingest_reel(session_factory, text=POST)
    extractor = FixturePosts(tmp_path, ["video", "video"])

    class SegmentedTranscriber:
        attempts = 0

        async def transcribe(self, path, *, completed_segments, on_segment, **kwargs):
            identifier = path.read_text()
            if identifier == "1":
                assert completed_segments == {}
                return "First transcript"
            self.attempts += 1
            if self.attempts == 1:
                assert completed_segments == {}
                await on_segment(
                    0, TranscriptionSegmentCheckpoint("part", "a" * 64, "provider", "m", 300, "v1")
                )
                raise AppError("TIMEOUT", "fixture STT timeout")
            assert completed_segments[0].text == "part"
            return "Second transcript"

    extractor.transcriber = SegmentedTranscriber()
    worker = make_worker(session_factory, extractor, FakeLlmProvider())
    assert await worker.process_one()
    saved, _, rows = await _state(session_factory, item.id)
    assert saved.analysis_completeness == "PARTIAL"
    assert sum(row.kind is ContentKind.TRANSCRIPT_CHUNK for row in rows) == 1
    await apply_item_action(session_factory, 42, item.id, "retry")
    assert await worker.process_one()
    saved, _, rows = await _state(session_factory, item.id)
    assert saved.analysis_completeness == "TRANSCRIPT_ONLY"
    assert sum(row.kind is ContentKind.TRANSCRIPT for row in rows) == 2
    assert not any(row.kind is ContentKind.TRANSCRIPT_CHUNK for row in rows)
    assert [entry[0] for entry in extractor.prepared] == ["1", "2", "2"]
