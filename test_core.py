import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from cache import ResultCache
from job_manager import JobCancelled, JobManager, JobQueueFull
from models import TranscribeRequest
from transcribe_service import infer_metadata_language, parse_bilibili_video_url, resolve_bilibili_audio, transcribe_request
from server import (
    _empty_correction, _source_matches_subject, correct_segments_from_web, extract_song_metadata,
    fetch_bilibili_subtitle_candidates, fetch_lrclib_candidates, fetch_netease_candidates,
    sync_lrc_to_audio,
)


class JobManagerTests(unittest.TestCase):
    def wait_terminal(self, manager, job_id):
        deadline = time.time() + 2
        while time.time() < deadline:
            job = manager.get(job_id)
            if job and job["status"] in {"succeeded", "failed", "cancelled"}:
                return job
            time.sleep(0.01)
        self.fail("任务未在测试时间内结束")

    def test_success_and_failure(self):
        manager = JobManager(lambda request, _cancel, _stage: {"value": request}, workers=1, queue_size=2)
        success = manager.create("ok")
        self.assertEqual(self.wait_terminal(manager, success["jobId"])["result"], {"value": "ok"})

        failing = JobManager(lambda _request, _cancel, _stage: (_ for _ in ()).throw(ValueError("boom")), workers=1, queue_size=2)
        failed = self.wait_terminal(failing, failing.create("bad")["jobId"])
        self.assertEqual(failed["status"], "failed")
        self.assertEqual(failed["error"], "boom")

    def test_running_cancel_is_cancelling_until_runner_stops(self):
        started = threading.Event()
        release = threading.Event()

        def runner(_request, cancel, _stage):
            started.set()
            self.assertTrue(cancel.wait(1))
            release.wait(1)
            raise JobCancelled()

        manager = JobManager(runner, workers=1, queue_size=2)
        created = manager.create("slow")
        self.assertTrue(started.wait(1))
        cancelling = manager.cancel(created["jobId"])
        self.assertEqual(cancelling["status"], "cancelling")
        self.assertTrue(cancelling["cancelRequested"])
        self.assertEqual(manager.capabilities()["counts"]["cancelling"], 1)
        release.set()
        self.assertEqual(self.wait_terminal(manager, created["jobId"])["status"], "cancelled")

    def test_queued_cancel_releases_logical_capacity(self):
        blocker_started = threading.Event()
        blocker_release = threading.Event()

        def runner(request, _cancel, _stage):
            if request == "block":
                blocker_started.set()
                blocker_release.wait(1)
            return {"value": request}

        manager = JobManager(runner, workers=1, queue_size=1)
        first = manager.create("block")
        self.assertTrue(blocker_started.wait(1))
        queued = manager.create("queued")
        with self.assertRaises(JobQueueFull):
            manager.create("full")
        self.assertEqual(manager.cancel(queued["jobId"])["status"], "cancelled")
        replacement = manager.create("replacement")
        blocker_release.set()
        self.wait_terminal(manager, first["jobId"])
        self.assertEqual(self.wait_terminal(manager, replacement["jobId"])["status"], "succeeded")

    def test_terminal_jobs_are_pruned_without_get(self):
        manager = JobManager(lambda request, _cancel, _stage: {"value": request}, workers=1, queue_size=1, retention_seconds=0.1)
        created = manager.create("ok")
        self.wait_terminal(manager, created["jobId"])
        time.sleep(0.25)
        self.assertIsNone(manager.get(created["jobId"]))
        self.assertEqual(sum(manager.capabilities()["counts"].values()), 0)


class CacheTests(unittest.TestCase):
    def make_value(self, marker="x"):
        return {"raw": {"segments": [], "words": []}, "final": {"segments": [], "language": "zh", "marker": marker}}

    def test_cache_hit_and_corruption_is_ignored(self):
        with tempfile.TemporaryDirectory() as directory:
            cache = ResultCache(directory, max_entries=2, ttl_seconds=60)
            request = TranscribeRequest(url="https://www.bilibili.com/video/BV1abc?p=2&utm_source=x", start=1, end=3)
            key = cache.make_key(request, {"name": "tiny"}, {"schema_version": 1})
            value = self.make_value()
            cache.set(key, value)
            self.assertEqual(cache.get(key), value)
            cache._memory.clear()
            Path(directory, f"{key}.json").write_text("not json", encoding="utf-8")
            self.assertIsNone(cache.get(key))

    def test_ttl_uses_created_at_not_access_time(self):
        with tempfile.TemporaryDirectory() as directory:
            cache = ResultCache(directory, max_entries=2, ttl_seconds=1)
            cache.set("key", self.make_value())
            path = Path(directory, "key.json")
            stored = json.loads(path.read_text(encoding="utf-8"))
            stored["created_at"] = time.time() - 2
            stored["accessed_at"] = time.time()
            path.write_text(json.dumps(stored), encoding="utf-8")
            cache._memory.clear()
            self.assertIsNone(cache.get("key"))
            self.assertFalse(path.exists())

    def test_disk_lru_uses_accessed_at(self):
        with tempfile.TemporaryDirectory() as directory:
            cache = ResultCache(directory, max_entries=2, ttl_seconds=60)
            cache.set("old", self.make_value("old"))
            time.sleep(0.01)
            cache.set("recent", self.make_value("recent"))
            cache._memory.clear()
            self.assertIsNotNone(cache.get("old"))
            cache.set("new", self.make_value("new"))
            self.assertTrue(Path(directory, "old.json").exists())
            self.assertFalse(Path(directory, "recent.json").exists())
            self.assertTrue(Path(directory, "new.json").exists())


class BilibiliResolverTests(unittest.TestCase):
    def test_parse_video_and_page(self):
        self.assertEqual(parse_bilibili_video_url("https://www.bilibili.com/video/BV1sc411V7ZE?p=2"), ("BV1sc411V7ZE", 2))
        self.assertIsNone(parse_bilibili_video_url("https://example.com/video/BV1sc411V7ZE"))

    def test_resolve_uses_selected_page_and_best_audio(self):
        video = {
            "code": 0,
            "data": {
                "title": "【4K修复】青鸟飞鱼《此生不换》",
                "desc": "歌曲：此生不换\n歌手：青鸟飞鱼",
                "duration": 500,
                "tname": "音乐综合",
                "owner": {"name": "Uploader"},
                "pages": [
                    {"page": 1, "cid": 11, "part": "其他", "duration": 233},
                    {"page": 2, "cid": 22, "part": "此生不换 4k 上传", "duration": 267},
                ],
            },
        }
        play = {"code": 0, "data": {"dash": {"audio": [{"bandwidth": 1, "baseUrl": "https://cdn/low"}, {"bandwidth": 2, "base_url": "https://cdn/high"}]}}}
        def response(payload):
            value = mock.MagicMock()
            value.__enter__.return_value.read.return_value = json.dumps(payload).encode()
            return value
        with mock.patch("transcribe_service.urlopen", side_effect=[response(video), response(play)]) as opened:
            result = resolve_bilibili_audio("https://www.bilibili.com/video/BV1sc411V7ZE?p=2", 10)
        info = result["video_info"]
        self.assertEqual(result["audio_url"], "https://cdn/high")
        self.assertEqual(info["title"], "此生不换 4k 上传")
        self.assertEqual(info["total_title"], "【4K修复】青鸟飞鱼《此生不换》")
        self.assertEqual(info["duration"], 267)
        self.assertEqual(info["total_duration"], 500)
        self.assertIn("歌曲：此生不换", info["description"])
        self.assertEqual(info["categories"], ["音乐综合"])
        self.assertIn("cid=22", opened.call_args_list[1].args[0].full_url)

    def test_bv1my411w7ai_metadata_uses_page_and_total_title(self):
        info = {
            "title": "此生不换 4k 上传",
            "page_title": "此生不换 4k 上传",
            "total_title": "【4K修复】青鸟飞鱼《此生不换》经典歌曲",
            "original_title": "【4K修复】青鸟飞鱼《此生不换》经典歌曲",
            "duration": 267,
            "description": "歌曲：此生不换\n演唱：青鸟飞鱼",
            "uploader": "影视音乐收藏",
        }
        metadata = extract_song_metadata(info["title"], info["uploader"], info)
        self.assertEqual(metadata["song_title"], "此生不换")
        self.assertEqual(metadata["artist"], "青鸟飞鱼")
        self.assertEqual(metadata["duration"], 267)
        self.assertEqual(infer_metadata_language(info), "zh")

    def test_metadata_version_flags_are_preserved(self):
        metadata = extract_song_metadata("歌手《歌曲》现场翻唱 Remix 伴奏", "歌手", {"duration": 30, "metadata_language": "zh"})
        self.assertTrue(metadata["is_live"])
        self.assertTrue(metadata["is_cover"])
        self.assertTrue(metadata["is_instrumental"])
        self.assertIn("live", metadata["version_terms"])
        self.assertEqual(metadata["language"], "zh")

    def test_metadata_language_falls_back_to_auto_for_noise_only(self):
        info = {"title": "4K MV", "description": "", "tags": ["HD"]}
        self.assertEqual(infer_metadata_language(info), "auto")

    def test_metadata_language_detects_obvious_japanese_korean_and_english(self):
        self.assertEqual(infer_metadata_language({"title": "君の知らない物語"}), "ja")
        self.assertEqual(infer_metadata_language({"title": "사랑 노래"}), "ko")
        self.assertEqual(infer_metadata_language({"title": "A Beautiful Song"}), "en")


class TranscribeRequestTests(unittest.TestCase):
    def test_metadata_language_applies_only_to_auto_request(self):
        settings = mock.Mock(
            request_timeout=60, model_name="tiny", device="cpu", compute_type="int8",
            ytdlp_socket_timeout=10, max_download_bytes=1024, ffmpeg_timeout=10,
        )
        cache = mock.Mock()
        cache.get.return_value = None
        run_model = mock.Mock(return_value=([], mock.Mock(language="zh")))
        dependencies = {
            "settings": settings,
            "vad_filter": False,
            "cache": cache,
            "lyrics_config": {},
            "get_model": mock.Mock(),
            "run_command": mock.Mock(),
            "run_model_transcription": run_model,
            "bounded_timeout": lambda _deadline, timeout: timeout,
            "remaining": lambda _deadline: 60,
            "extract_song_metadata": mock.Mock(return_value={"search_queries": []}),
            "empty_correction": mock.Mock(return_value={}),
            "correct_segments_from_web": mock.Mock(),
            "interpolate_words_from_segments": mock.Mock(return_value=[]),
            "lyrics_web_correction": False,
        }
        bilibili_source = {
            "audio_url": "https://cdn.example/audio.m4s",
            "headers": {},
            "video_info": {"title": "此生不换", "uploader": "青鸟飞鱼"},
        }

        with mock.patch("transcribe_service.resolve_bilibili_audio", return_value=bilibili_source):
            transcribe_request(
                TranscribeRequest(url="https://www.bilibili.com/video/BV1abc", start=0, end=10, language="auto"),
                **dependencies,
            )
            transcribe_request(
                TranscribeRequest(url="https://www.bilibili.com/video/BV1abc", start=0, end=10, language="en"),
                **dependencies,
            )

        self.assertEqual(run_model.call_args_list[0].args[1], "zh")
        self.assertEqual(run_model.call_args_list[1].args[1], "en")


class LrclibCandidateTests(unittest.TestCase):
    @staticmethod
    def response(items):
        return json.dumps(items), "https://lrclib.net/api/search"

    def test_title_extracts_artist_with_unicode_dash_suffix(self):
        metadata = extract_song_metadata("【4K/歌词字幕】黄霄雲—《此生不换》-20260829宇宙无敌号2.0杭州演唱会")
        self.assertEqual(metadata["artist"], "黄霄雲")
        self.assertEqual(metadata["song_title"], "此生不换")

    def test_metadata_similarity_precedes_duration_and_filters_wrong_track(self):
        items = [
            {"id": 1, "trackName": "错误歌曲", "artistName": "青鸟飞鱼", "duration": 267, "plainLyrics": "wrong"},
            {"id": 2, "trackName": "此生不换", "artistName": "青鸟飞鱼", "duration": 280, "plainLyrics": "right artist"},
            {"id": 3, "trackName": "此生不换", "artistName": "其他歌手", "duration": 267, "plainLyrics": "wrong artist"},
        ]
        with mock.patch("server._fetch_public_text", return_value=self.response(items)):
            candidates = fetch_lrclib_candidates({"song_title": "此生不换", "artist": "青鸟飞鱼", "duration": 267})
        self.assertEqual([item["url"].rsplit("/", 1)[-1] for item in candidates], ["2", "3"])

    def test_artist_query_also_runs_song_only_fallback(self):
        responses = [self.response([]), self.response([
            {"id": 6, "trackName": "此生不换", "artistName": "青鸟飞鱼", "duration": 267, "plainLyrics": "lyrics"},
        ])]
        with mock.patch("server._fetch_public_text", side_effect=responses) as fetch:
            candidates = fetch_lrclib_candidates({"song_title": "此生不换", "artist": "黄霄雲", "duration": 267})
        self.assertEqual(fetch.call_count, 2)
        self.assertEqual([item["url"].rsplit("/", 1)[-1] for item in candidates], ["6"])

    def test_missing_artist_allows_strong_track_match(self):
        items = [
            {"id": 4, "trackName": "此生不换", "artistName": "", "duration": 270, "plainLyrics": "lyrics"},
            {"id": 5, "trackName": "完全不同", "artistName": "", "duration": 267, "plainLyrics": "wrong"},
        ]
        with mock.patch("server._fetch_public_text", return_value=self.response(items)):
            candidates = fetch_lrclib_candidates({"song_title": "此生不换", "artist": "", "duration": 267})
        self.assertEqual([item["url"].rsplit("/", 1)[-1] for item in candidates], ["4"])

    def test_cover_candidate_requires_strong_lrc_anchors(self):
        metadata = {"song_title": "此生不换", "artist": "黄霄雲"}
        strong_rank = (4, 1.0, 0.9, 2, 1.0)
        weak_rank = (1, 1.0, 0.9, 1, 1.0)
        self.assertTrue(_source_matches_subject("此生不换 - 青鸟飞鱼", metadata, strong_rank))
        self.assertFalse(_source_matches_subject("此生不换 - 青鸟飞鱼", metadata, weak_rank))
        self.assertFalse(_source_matches_subject("完全不同 - 青鸟飞鱼", metadata, strong_rank))


class UnifiedLyricsTests(unittest.TestCase):
    def test_bilibili_subtitle_priority_and_parse(self):
        listing = {"data": {"subtitle": {"subtitles": [
            {"lan": "en", "lan_doc": "English", "subtitle_url": "//sub/en"},
            {"lan": "zh-CN", "lan_doc": "中文（自动生成）", "subtitle_url": "//sub/auto"},
            {"lan": "zh-CN", "lan_doc": "中文", "subtitle_url": "//sub/zh"},
        ]}}}
        body = json.dumps({"body": [{"from": 1.2, "to": 2.5, "content": "第一句"}, {"from": 3, "to": 4, "content": "第二句"}]})
        responses = [(json.dumps(listing), "https://api.bilibili.com/x/player/v2"), (body, "https://sub/en"), (body, "https://sub/auto"), (body, "https://sub/zh")]
        info = {"bvid": "BV1", "cid": 2, "title": "歌曲", "uploader": "歌手", "duration": 10}
        with mock.patch("server._fetch_public_text", side_effect=responses):
            candidates = fetch_bilibili_subtitle_candidates(info)
        self.assertEqual(candidates[0]["subtitle_label"], "中文")
        self.assertFalse(candidates[0]["is_automatic"])
        self.assertIn("[00:01.20]第一句", candidates[0]["lrc_text"])

    def test_bilibili_subtitle_candidate_matches_subject_and_preserves_artist(self):
        listing = {"data": {"subtitle": {"subtitles": [
            {"lan": "zh-CN", "lan_doc": "中文", "subtitle_url": "//sub/zh"},
        ]}}}
        body = json.dumps({"body": [
            {"from": 1.2, "to": 2.5, "content": "第一句"},
            {"from": 3, "to": 4, "content": "第二句"},
        ]})
        info = {
            "bvid": "BV1", "cid": 2, "title": "青鸟飞鱼《此生不换》", "uploader": "影视音乐收藏",
            "duration": 10,
        }
        with mock.patch("server._fetch_public_text", side_effect=[
            (json.dumps(listing), "https://api.bilibili.com/x/player/v2"),
            (body, "https://sub/zh"),
        ]):
            candidates = fetch_bilibili_subtitle_candidates(info)
        candidate = candidates[0]
        metadata = extract_song_metadata(info["title"], info["uploader"], info)
        self.assertEqual(candidate["track_name"], "此生不换")
        self.assertEqual(candidate["artist_name"], "青鸟飞鱼")
        self.assertEqual(candidate["subtitle_label"], "中文")
        self.assertIn("此生不换", candidate["title"])
        self.assertIn("青鸟飞鱼", candidate["title"])
        self.assertTrue(_source_matches_subject(
            candidate["title"], metadata, (2, 1.0, 0.9, 2, 1.0)
        ))

    def test_netease_candidate_is_unified(self):
        search = {"result": {"songs": [{"id": 7, "name": "歌曲", "duration": 10000, "artists": [{"name": "歌手"}]}]}}
        lyric = {"lrc": {"lyric": "[00:01.00]第一句"}}
        with mock.patch("server._fetch_public_text", side_effect=[(json.dumps(search), "search"), (json.dumps(lyric), "lyric")]):
            candidates = fetch_netease_candidates({"song_title": "歌曲", "artist": "歌手", "duration": 10})
        self.assertEqual(candidates[0]["provider"], "netease")
        self.assertEqual(candidates[0]["track_name"], "歌曲")
        self.assertGreater(candidates[0]["metadata_score"], 0.8)

    def test_linear_timeline_wins_and_short_span_stays_offset(self):
        lrc = [{"start": float(i * 10), "end": float(i * 10 + 4), "text": f"第{i}句歌词"} for i in range(5)]
        whisper = [{"start": 2 + 1.02 * i * 10, "end": 5 + 1.02 * i * 10, "text": f"第{i}句歌词"} for i in range(5)]
        _, details = sync_lrc_to_audio(lrc, whisper, 0, 60)
        self.assertEqual(details["timeline_model"], "linear")
        self.assertAlmostEqual(details["scale"], 1.02, places=2)
        _, short = sync_lrc_to_audio(lrc[:3], whisper[:3], 0, 30)
        self.assertEqual(short["timeline_model"], "offset")

    def test_long_span_linear_timeline_ignores_raw_offset_spread(self):
        lrc = [{"start": float(i * 20), "end": float(i * 20 + 4), "text": f"长歌第{i}句"} for i in range(11)]
        whisper = [
            {"start": 2 + 1.02 * i * 20, "end": 6 + 1.02 * i * 20, "text": f"长歌第{i}句"}
            for i in range(11)
        ]
        synced, details = sync_lrc_to_audio(lrc, whisper, 0, 215)
        self.assertEqual(details["timeline_model"], "linear")
        self.assertTrue(details["accepted"])
        self.assertGreater(details["offset_spread"], 1.5)
        self.assertAlmostEqual(details["scale"], 1.02, places=3)
        self.assertLess(details["rmse"], 0.01)
        self.assertEqual(len(synced), len(lrc))

    def test_linear_timeline_rejects_high_rmse_anchors(self):
        lrc = [{"start": float(i * 50), "end": float(i * 50 + 4), "text": f"异常第{i}句"} for i in range(5)]
        actual_starts = [2, 53, 130, 155, 206]
        whisper = [
            {"start": actual_starts[i], "end": actual_starts[i] + 4, "text": f"异常第{i}句"}
            for i in range(5)
        ]
        _, details = sync_lrc_to_audio(lrc, whisper, 0, 220)
        self.assertNotEqual(details["timeline_model"], "linear")

    def test_bilibili_selection_fields_use_extracted_source_artist(self):
        segments = [
            {"start": 1.0, "end": 2.5, "text": "第一句"},
            {"start": 3.0, "end": 4.5, "text": "第二句"},
        ]
        candidate = {
            "provider": "bilibili_subtitle", "title": "青鸟飞鱼 此生不换 B站字幕：中文",
            "track_name": "此生不换", "artist_name": "青鸟飞鱼", "duration": 10,
            "source_url": "https://sub/zh", "url": "https://sub/zh",
            "lrc_text": "[00:01.00]第一句\n[00:03.00]第二句", "plain_text": "第一句\n第二句",
            "metadata_score": 1.0, "subtitle_label": "中文",
        }
        info = {"title": "青鸟飞鱼《此生不换》", "uploader": "影视音乐收藏", "duration": 10}
        with mock.patch("server.fetch_bilibili_subtitle_candidates", return_value=[candidate]), \
                mock.patch("server.fetch_netease_candidates", return_value=[]), \
                mock.patch("server.fetch_lrclib_candidates", return_value=[]):
            _, correction = correct_segments_from_web(
                segments, info["title"], info["uploader"], video_info=info, clip_start=0, clip_end=10,
            )
        self.assertTrue(correction["applied"])
        self.assertEqual(correction["provider"], "bilibili_subtitle")
        self.assertEqual(correction["metadata"]["song_title"], "此生不换")
        self.assertEqual(correction["metadata"]["detected_artist"], "青鸟飞鱼")
        self.assertEqual(correction["metadata"]["source_artist"], "青鸟飞鱼")

    def test_empty_correction_has_compatible_extended_fields(self):
        correction = _empty_correction("query", "reason")
        self.assertEqual(correction["mode"], "none")
        self.assertIn("provider", correction)
        self.assertIn("metadata", correction)
        self.assertIn("timeline", correction)
        self.assertEqual(correction["attempted_providers"], [])


class ServerAuthTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import server
        from fastapi.testclient import TestClient
        cls.server = server
        cls.client = TestClient(server.app)

    def test_extension_origin_gets_no_store_session_token(self):
        response = self.client.get("/auth/session", headers={"Origin": "chrome-extension://test-extension"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers.get("cache-control"), "no-store")
        self.assertEqual(response.json()["token"], self.server._API_TOKEN)
        self.assertEqual(response.headers.get("access-control-allow-origin"), "chrome-extension://test-extension")

    def test_service_worker_without_origin_uses_extension_id(self):
        response = self.client.get(
            "/auth/session",
            headers={"X-Sing-Reactor-Extension": "abcdefghijklmnopabcdefghijklmnop"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers.get("cache-control"), "no-store")

    def test_missing_origin_requires_valid_extension_id(self):
        missing = self.client.get("/auth/session")
        self.assertEqual(missing.status_code, 403)
        invalid = self.client.get(
            "/auth/session", headers={"X-Sing-Reactor-Extension": "not-an-extension"}
        )
        self.assertEqual(invalid.status_code, 403)

    def test_regular_web_origin_cannot_get_session_token(self):
        response = self.client.get("/auth/session", headers={"Origin": "https://example.com"})
        self.assertEqual(response.status_code, 403)
        self.assertNotIn("access-control-allow-origin", response.headers)

    def test_heavy_endpoints_always_require_token(self):
        missing = self.client.get("/transcribe/jobs/not-found")
        self.assertEqual(missing.status_code, 401)
        accepted = self.client.get(
            "/transcribe/jobs/not-found",
            headers={"X-Sing-Reactor-Token": self.server._API_TOKEN},
        )
        self.assertEqual(accepted.status_code, 404)


class ServerConcurrencyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import server
        cls.server = server

    def test_processing_slot_released_when_cancel_races_after_acquire(self):
        server = self.server
        original_slots = server._processing_slots
        server._processing_slots = threading.BoundedSemaphore(1)
        cancel = threading.Event()
        cancel.set()
        try:
            with self.assertRaises(JobCancelled):
                server._run_transcription(object(), cancel)
            self.assertTrue(server._processing_slots.acquire(blocking=False))
            server._processing_slots.release()
        finally:
            server._processing_slots = original_slots

    def test_run_command_terminates_on_cancel(self):
        server = self.server
        cancel = threading.Event()
        cancel.set()
        process = mock.Mock()
        running = {"value": True}
        process.poll.side_effect = lambda: None if running["value"] else 0
        process.terminate.side_effect = lambda: running.update(value=False)
        process.wait.return_value = 0
        with mock.patch.object(server.subprocess, "Popen", return_value=process):
            with self.assertRaises(JobCancelled):
                server.run_command(["ffmpeg"], "音频截取失败", 10, cancel_event=cancel)
        process.terminate.assert_called_once()

    def test_whisper_deadline_releases_request_but_serializes_inference(self):
        server = self.server

        class SlowModel:
            def transcribe(self, *_args, **_kwargs):
                time.sleep(0.15)
                return [], mock.Mock(language="zh")

        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory, "source.wav")
            source.write_bytes(b"test")
            with mock.patch.object(server, "get_model", return_value=SlowModel()):
                with self.assertRaises(TimeoutError):
                    server.run_model_transcription(
                        source, None, False, threading.Event(), time.monotonic() + 0.05,
                    )
            time.sleep(0.2)
            self.assertTrue(server._model_inference_slot.acquire(blocking=False))
            server._model_inference_slot.release()


if __name__ == "__main__":
    unittest.main()
