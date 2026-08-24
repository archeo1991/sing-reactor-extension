import logging
import tempfile
import time
import xml.etree.ElementTree as ET
from pathlib import Path
from urllib.error import HTTPError, URLError

from fastapi import HTTPException
from yt_dlp import YoutubeDL

from job_manager import JobCancelled


logger = logging.getLogger("sing_reactor.transcribe_service")


MODEL_TRANSCRIBE_PARAMS = {
    "word_timestamps": True,
    "beam_size": 5,
    "best_of": 5,
    "condition_on_previous_text": True,
}


def transcribe_request(
    request,
    *,
    settings,
    vad_filter,
    cache,
    lyrics_config,
    get_model,
    run_command,
    run_model_transcription,
    bounded_timeout,
    remaining,
    extract_song_metadata,
    empty_correction,
    correct_segments_from_web,
    interpolate_words_from_segments,
    lyrics_web_correction,
    cancel_event=None,
    stage_callback=None,
):
    deadline = time.monotonic() + settings.request_timeout
    duration = request.end - request.start
    stage_callback = stage_callback or (lambda _stage: None)

    def check_cancel():
        if cancel_event is not None and cancel_event.is_set():
            raise JobCancelled("识别任务已取消")

    model_params = {
        "name": settings.model_name,
        "device": settings.device,
        "compute_type": settings.compute_type,
        "vad_filter": vad_filter,
        **MODEL_TRANSCRIBE_PARAMS,
    }
    cache_key = cache.make_key(request, model_params, lyrics_config)
    stage_callback("cache_lookup")
    cached = cache.get(cache_key)
    if cached:
        result = dict(cached["final"])
        result["cacheHit"] = True
        return result

    check_cancel()
    try:
        with tempfile.TemporaryDirectory(prefix="bls-whisper-") as temp_dir:
            temp_path = Path(temp_dir)
            output_template = str(temp_path / "source.%(ext)s")

            def check_download(progress):
                check_cancel()
                total = progress.get("downloaded_bytes") or progress.get("total_bytes") or progress.get("total_bytes_estimate") or 0
                if total > settings.max_download_bytes:
                    raise ValueError("下载文件超过大小上限")

            stage_callback("downloading")
            options = {
                "format": "bestaudio/best",
                "outtmpl": output_template,
                "noplaylist": True,
                "quiet": True,
                "no_warnings": True,
                "socket_timeout": bounded_timeout(deadline, settings.ytdlp_socket_timeout),
                "retries": settings.ytdlp_retries,
                "fragment_retries": settings.ytdlp_retries,
                "extractor_retries": settings.ytdlp_retries,
                "file_access_retries": settings.ytdlp_retries,
                "max_filesize": settings.max_download_bytes,
                "progress_hooks": [check_download],
                "download_ranges": lambda _info, _ydl: [{"start_time": request.start, "end_time": request.end}],
            }
            with YoutubeDL(options) as downloader:
                video_info = downloader.extract_info(request.url, download=True)
                check_cancel()
                requested_downloads = video_info.get("requested_downloads") or []
                downloaded_info = requested_downloads[0] if requested_downloads else video_info
                downloaded = Path(downloaded_info.get("filepath") or downloader.prepare_filename(downloaded_info))
                video_title = str(video_info.get("title") or "")
                uploader = str(video_info.get("uploader") or video_info.get("channel") or "")
                song_metadata = extract_song_metadata(video_title, uploader, video_info)

            if not downloaded.exists():
                candidates = list(temp_path.glob("source.*"))
                if not candidates:
                    raise HTTPException(status_code=500, detail="yt-dlp 未生成音频文件")
                downloaded = candidates[0]
            if downloaded.stat().st_size > settings.max_download_bytes:
                raise HTTPException(status_code=413, detail="下载文件超过大小上限")

            check_cancel()
            stage_callback("ffmpeg")
            wav_path = temp_path / "clip.wav"
            section_start = downloaded_info.get("section_start")
            local_start = 0.0 if section_start is not None else request.start
            run_command(
                ["ffmpeg", "-y", "-ss", str(local_start), "-i", str(downloaded), "-t", str(duration),
                 "-vn", "-ac", "1", "-ar", "16000", str(wav_path)],
                "音频截取失败",
                bounded_timeout(deadline, settings.ffmpeg_timeout),
                cancel_event=cancel_event,
                deadline=deadline,
            )
            check_cancel()
            if remaining(deadline) <= 0:
                raise TimeoutError("请求处理超时")

            stage_callback("whisper")
            check_cancel()
            language = None if request.language in {"", "auto"} else request.language
            whisper_segments, whisper_info = run_model_transcription(
                wav_path, language, vad_filter, cancel_event, deadline,
            )
            raw_segments = []
            whisper_words = []
            for segment in whisper_segments:
                check_cancel()
                text = segment.text.strip()
                if not text:
                    continue
                item = {"start": float(segment.start) + request.start, "end": float(segment.end) + request.start, "text": text}
                raw_segments.append(item)
                segment_words = []
                for word in (getattr(segment, "words", None) or []):
                    word_text = str(getattr(word, "word", "")).strip()
                    word_start = getattr(word, "start", None)
                    word_end = getattr(word, "end", None)
                    if word_text and word_start is not None and word_end is not None and float(word_end) > float(word_start):
                        segment_words.append({"text": word_text, "start": float(word_start) + request.start,
                                              "end": float(word_end) + request.start})
                whisper_words.extend(segment_words or interpolate_words_from_segments([item]))
            check_cancel()

            result = [dict(item) for item in raw_segments]
            search_queries = song_metadata["search_queries"]
            search_query = search_queries[0] if search_queries else ""
            correction = empty_correction(search_query, "联网歌词校正已关闭", search_queries)
            correction["total_count"] = len(result)
            if lyrics_web_correction and remaining(deadline) > 6:
                stage_callback("lyrics")
                check_cancel()
                try:
                    result, correction = correct_segments_from_web(
                        result, video_title, uploader, whisper_words, request.start, request.end, video_info, deadline,
                        cancel_check=check_cancel,
                    )
                except TimeoutError:
                    correction = empty_correction(search_query, "联网歌词校正超时，已保留原识别结果", search_queries)
                    correction["total_count"] = len(result)
                except (HTTPError, URLError, OSError, ValueError, UnicodeError, ET.ParseError) as exc:
                    logger.warning("联网歌词校正失败 error=%s", exc)
                    correction = empty_correction(search_query, "联网搜索失败，已保留原识别结果", search_queries)
                    correction["total_count"] = len(result)
            elif lyrics_web_correction:
                correction = empty_correction(search_query, "请求预算不足，已跳过联网歌词校正", search_queries)
                correction["total_count"] = len(result)
            check_cancel()
            for segment in result:
                segment.setdefault("corrected", False)
                segment.setdefault("confidence", 0.0)
            final = {"segments": result, "language": whisper_info.language, "correction": correction, "cacheHit": False}
            stage_callback("cache_write")
            cache.set(cache_key, {"raw": {"segments": raw_segments, "words": whisper_words}, "final": final})
            return final
    except JobCancelled:
        raise
