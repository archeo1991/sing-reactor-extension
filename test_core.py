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
from server import extract_song_metadata, fetch_lrclib_candidates


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

    def test_metadata_similarity_precedes_duration_and_filters_wrong_track(self):
        items = [
            {"id": 1, "trackName": "错误歌曲", "artistName": "青鸟飞鱼", "duration": 267, "plainLyrics": "wrong"},
            {"id": 2, "trackName": "此生不换", "artistName": "青鸟飞鱼", "duration": 280, "plainLyrics": "right artist"},
            {"id": 3, "trackName": "此生不换", "artistName": "其他歌手", "duration": 267, "plainLyrics": "wrong artist"},
        ]
        with mock.patch("server._fetch_public_text", return_value=self.response(items)):
            candidates = fetch_lrclib_candidates({"song_title": "此生不换", "artist": "青鸟飞鱼", "duration": 267})
        self.assertEqual([item["url"].rsplit("/", 1)[-1] for item in candidates], ["2", "3"])

    def test_missing_artist_allows_strong_track_match(self):
        items = [
            {"id": 4, "trackName": "此生不换", "artistName": "", "duration": 270, "plainLyrics": "lyrics"},
            {"id": 5, "trackName": "完全不同", "artistName": "", "duration": 267, "plainLyrics": "wrong"},
        ]
        with mock.patch("server._fetch_public_text", return_value=self.response(items)):
            candidates = fetch_lrclib_candidates({"song_title": "此生不换", "artist": "", "duration": 267})
        self.assertEqual([item["url"].rsplit("/", 1)[-1] for item in candidates], ["4"])


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
