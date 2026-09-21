import bisect
import concurrent.futures
import hmac
import html
import http.client
import ipaddress
import json
import logging
import math
import os
import re
import secrets
import shutil
import socket
import ssl
import subprocess
import tempfile
import threading
import time
import xml.etree.ElementTree as ET
from difflib import SequenceMatcher
from html.parser import HTMLParser
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, quote_plus, unquote, urlencode, urljoin, urlparse



import uvicorn
from fastapi import FastAPI, Header, HTTPException, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from yt_dlp import YoutubeDL
from faster_whisper import WhisperModel

from app_config import LYRICS_CONFIG, LYRICS_CONFIG_PATH, SETTINGS
from cache import ResultCache
from job_manager import JobManager, JobQueueFull
from models import MAX_DURATION, TranscribeRequest
from transcribe_service import MODEL_TRANSCRIBE_PARAMS, transcribe_request


logger = logging.getLogger("sing_reactor.server")


def _cfg(path, expected_type=None):
    value = LYRICS_CONFIG
    traversed = []
    for part in path.split("."):
        traversed.append(part)
        if isinstance(value, dict) and part in value:
            value = value[part]
        elif isinstance(value, list) and part.isdigit() and int(part) < len(value):
            value = value[int(part)]
        else:
            raise KeyError(f"缺少歌词配置项 {'.'.join(traversed)}")
    if expected_type is not None and (not isinstance(value, expected_type) or expected_type in (int, float) and isinstance(value, bool)):
        expected_name = getattr(expected_type, "__name__", str(expected_type))
        raise TypeError(f"歌词配置项 {path} 必须是 {expected_name}")
    return value


def _cfg_number(path):
    return float(_cfg(path, (int, float)))


def _compile_cfg_patterns(path):
    patterns = _cfg(path, list)
    if not patterns or not all(isinstance(pattern, str) and pattern for pattern in patterns):
        raise TypeError(f"歌词配置项 {path} 必须是非空字符串数组")
    try:
        return re.compile("(?:" + ")|(?:".join(patterns) + ")", re.I)
    except re.error as exc:
        raise ValueError(f"歌词配置项 {path} 包含非法正则: {exc}") from exc


def _compile_cfg_pattern(path):
    pattern = _cfg(path, str)
    try:
        return re.compile(pattern, re.I)
    except re.error as exc:
        raise ValueError(f"歌词配置项 {path} 包含非法正则: {exc}") from exc


if _cfg("schema_version", int) != 1:
    raise ValueError("歌词配置项 schema_version 必须为 1")
for _section in ("title_cleanup", "uploader", "metadata", "search", "candidate_filter", "script_tracks", "lrc_sync", "provider_quality", "timeline_quality", "fallback"):
    _cfg(_section, dict)
for _path in (
    "title_cleanup.removable_terms", "title_cleanup.date_patterns", "title_cleanup.venue_suffix_patterns",
    "uploader.trusted_official_markers", "metadata.bracket_patterns",
    "metadata.discovery_title_patterns", "metadata.artist_title_separators",
    "script_tracks.priority", "candidate_filter.metadata_line_patterns", "candidate_filter.noise_patterns",
    "candidate_filter.ui_only_patterns",
):
    _cfg(_path, list)
for _path in (
    "metadata.extract_book_title", "search.lrclib_enabled", "search.netease_enabled", "script_tracks.enabled",
    "lrc_sync.preserve_between_anchors", "fallback.enable_text_only",
):
    _cfg(_path, bool)
for _path in (
    "search.max_queries", "search.results_per_query", "search.max_candidates", "search.lrclib_max_results", "search.netease_max_results",
    "candidate_filter.minimum_candidate_lines", "candidate_filter.page_lines_preferred_minimum",
    "candidate_filter.combined_snippet_minimum", "candidate_filter.text_consistency_min_matches",
    "candidate_filter.text_only_minimum_multiple", "candidate_filter.text_only_minimum_single",
    "script_tracks.minimum_lines", "script_tracks.minimum_substantial_tracks",
    "script_tracks.preferred_source_minimum_lines", "lrc_sync.tail_allowance_lines",
    "timeline_quality.min_lines", "timeline_quality.min_anchored_lines",
    "timeline_quality.single_anchor_max_units", "lrc_sync.linear_min_anchors",
):
    _cfg(_path, int)
for _path in (
    "candidate_filter.alignment_confidence_threshold", "candidate_filter.source_title_similarity_threshold",
    "candidate_filter.artist_match_threshold", "candidate_filter.strong_audio_coverage",
    "candidate_filter.strong_audio_average_confidence", "candidate_filter.strong_audio_high_confidence_ratio",
    "candidate_filter.text_consistency_average_confidence", "candidate_filter.text_only_minimum_ratio",
    "lrc_sync.similarity_threshold", "lrc_sync.single_anchor_threshold", "lrc_sync.short_clip_max_duration",
    "lrc_sync.offset_cluster_tolerance", "lrc_sync.max_offset_spread", "lrc_sync.local_support_similarity",
    "lrc_sync.local_support_time_tolerance", "lrc_sync.repeated_fallback_similarity",
    "lrc_sync.prefix_tolerance", "lrc_sync.tail_allowance_seconds", "lrc_sync.min_clipped_duration",
    "lrc_sync.min_retained_ratio", "lrc_sync.pause_split_threshold", "lrc_sync.minimum_piece_duration",
    "lrc_sync.line_end_padding", "lrc_sync.linear_min_span", "lrc_sync.linear_scale_min", "lrc_sync.linear_scale_max", "lrc_sync.linear_rmse_improvement", "timeline_quality.high_confidence_threshold",
    "timeline_quality.high_confidence_ratio", "timeline_quality.average_confidence", "timeline_quality.coverage",
    "timeline_quality.single_line_coverage", "timeline_quality.single_line_average_confidence",
    "timeline_quality.per_line_minimum_confidence",
):
    _cfg_number(_path)
_cfg("metadata.book_title_pattern", str)
_cfg("provider_quality", dict)
_cfg("search.query_templates", dict)
for _category in ("with_artist", "without_artist", "latin", "non_latin"):
    _templates = _cfg(f"search.query_templates.{_category}", list)
    for _index, _template in enumerate(_templates):
        if not isinstance(_template, str) or "{song}" not in _template:
            raise TypeError(f"歌词配置项 search.query_templates.{_category}.{_index} 必须是包含 {{song}} 的字符串")
        try:
            _template.format(artist="artist", song="song")
        except (KeyError, ValueError) as exc:
            raise ValueError(f"歌词配置项 search.query_templates.{_category}.{_index} 模板非法: {exc}") from exc

HOST = SETTINGS.host
PORT = SETTINGS.port
MODEL_NAME = SETTINGS.model_name
DEVICE = SETTINGS.device
COMPUTE_TYPE = SETTINGS.compute_type
VAD_FILTER = SETTINGS.vad_filter
LYRICS_WEB_CORRECTION = SETTINGS.lyrics_web_correction
WEB_USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124 Safari/537.36"
SEARCH_TIMEOUT = 6
PAGE_TIMEOUT = 6
MAX_SEARCH_BYTES = 800_000
MAX_PAGE_BYTES = 1_500_000
MAX_CANDIDATES = _cfg("search.max_candidates", int)
MAX_RESULTS_PER_QUERY = _cfg("search.results_per_query", int)
MAX_SEARCH_QUERIES = _cfg("search.max_queries", int)
MAX_ALIGNMENT_UNITS = 6_000
MAX_ALIGNMENT_CELLS = 12_000_000
DEFAULT_LYRICS_HOSTS = (
    "lrclib.net", "www.bing.com", "html.duckduckgo.com", "genius.com", "musixmatch.com",
    "azlyrics.com", "lyrics.com", "lyricsfreak.com", "songlyrics.com", "lyricstranslate.com",
    "mojim.com", "kkbox.com", "kugou.com", "kuwo.cn", "music.163.com", "qq.com",
)
ALLOWED_LYRICS_HOSTS = frozenset(DEFAULT_LYRICS_HOSTS + SETTINGS.allowed_lyrics_hosts)

EXTENSION_ORIGIN_REGEX = None if SETTINGS.extension_origins else r"^(?:chrome|moz)-extension://[a-zA-Z0-9_-]+$"

app = FastAPI(title="声迹识别服务")
app.add_middleware(
    CORSMiddleware,
    allow_origins=list(SETTINGS.extension_origins),
    allow_origin_regex=EXTENSION_ORIGIN_REGEX,
    allow_methods=["GET", "POST", "DELETE"],
    allow_headers=["Content-Type", "X-Sing-Reactor-Token", "X-Sing-Reactor-Extension"],
)

_API_TOKEN = SETTINGS.api_token.strip() or secrets.token_urlsafe(32)
_model = None
_model_lock = threading.Lock()
_model_inference_slot = threading.BoundedSemaphore(1)
_model_executor = concurrent.futures.ThreadPoolExecutor(max_workers=1, thread_name_prefix="whisper-inference")
_processing_slots = threading.BoundedSemaphore(SETTINGS.max_concurrent_tasks)
_result_cache = ResultCache(SETTINGS.cache_dir, SETTINGS.cache_max_entries, SETTINGS.cache_ttl_seconds)


class _SearchResultParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.results = []
        self._in_result = False
        self._href = None
        self._title = []
        self._snippet = []
        self._in_snippet = 0

    def handle_starttag(self, tag, attrs):
        attrs_dict = dict(attrs)
        classes = attrs_dict.get("class", "").split()
        if tag == "li" and "b_algo" in classes:
            self._in_result = True
            self._href = None
            self._title = []
            self._snippet = []
            return
        if not self._in_result:
            return
        if tag == "a" and not self._href:
            self._href = attrs_dict.get("href")
        if tag in {"p", "div"} and ({"b_caption", "b_lineclamp2", "b_paractl"} & set(classes)):
            self._in_snippet += 1

    def handle_data(self, data):
        if not self._in_result:
            return
        if self._in_snippet:
            self._snippet.append(data)
        elif self._href:
            self._title.append(data)

    def handle_endtag(self, tag):
        if not self._in_result:
            return
        if self._in_snippet and tag in {"p", "div"}:
            self._in_snippet -= 1
        if tag != "li":
            return
        title = " ".join("".join(self._title).split())
        snippet = " ".join("".join(self._snippet).split())
        href = _unwrap_search_url(self._href or "")
        if title and href:
            self.results.append({"url": href, "title": title, "snippet": snippet})
        self._in_result = False
        self._href = None


class _LyricsPageParser(HTMLParser):
    PREFERRED_HINTS = ("lyric", "lyrics", "lrc", "song-text", "songtext", "geci", "歌词")

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.body_parts = []
        self.preferred_parts = []
        self.json_ld = []
        self._stack = []
        self._skip_depth = 0
        self._preferred_depth = 0
        self._json_depth = 0

    def handle_starttag(self, tag, attrs):
        attrs_dict = dict(attrs)
        is_json = tag == "script" and "ld+json" in attrs_dict.get("type", "").lower()
        is_skip = tag in {"script", "style", "noscript", "svg", "nav", "footer", "header", "aside", "form"} and not is_json
        marker = f"{attrs_dict.get('id', '')} {attrs_dict.get('class', '')}".lower()
        is_preferred = not is_skip and not is_json and any(hint in marker for hint in self.PREFERRED_HINTS)
        self._stack.append((tag, is_skip, is_preferred, is_json))
        self._skip_depth += int(is_skip)
        self._preferred_depth += int(is_preferred)
        self._json_depth += int(is_json)
        if not self._skip_depth and not self._json_depth and tag in {"br", "p", "div", "li", "tr", "section", "article", "h1", "h2", "h3"}:
            self._append("\n")

    def handle_endtag(self, tag):
        match_index = next((index for index in range(len(self._stack) - 1, -1, -1)
                            if self._stack[index][0] == tag), None)
        if match_index is None:
            return
        closing = self._stack[match_index:]
        if not self._skip_depth and not self._json_depth and tag in {"p", "div", "li", "tr", "section", "article", "pre"}:
            self._append("\n")
        del self._stack[match_index:]
        self._skip_depth -= sum(int(item[1]) for item in closing)
        self._preferred_depth -= sum(int(item[2]) for item in closing)
        self._json_depth -= sum(int(item[3]) for item in closing)

    def handle_data(self, data):
        if self._json_depth:
            self.json_ld.append(data)
        elif not self._skip_depth:
            self._append(data)

    def _append(self, text):
        self.body_parts.append(text)
        if self._preferred_depth:
            self.preferred_parts.append(text)


METADATA_RE = _compile_cfg_patterns("candidate_filter.metadata_line_patterns")
NOISE_RE = _compile_cfg_patterns("candidate_filter.noise_patterns")
UI_ONLY_RE = _compile_cfg_patterns("candidate_filter.ui_only_patterns")
TIME_TAG_RE = re.compile(r"\[(?:\d{1,3}:)?\d{1,2}:\d{2}(?:[.:]\d{1,3})?\]|\[\d{1,3}:\d{2}(?:[.:]\d{1,3})?\]")
LRC_TIME_RE = re.compile(r"\[(\d{1,3}:\d{1,2}(?::\d{2})?(?:[.:]\d{1,3})?)\]")
LRC_METADATA_RE = re.compile(r"\[(?:ar|ti|al|by|offset|re|ve|length|id|hash|sign|qq)\s*:[^\]]*\]", re.I)
TRADITIONAL_TO_SIMPLIFIED = str.maketrans(
    "許還記後來樂們著麼這個為說開護該實時淚舊細終點應會過從將無與讓愛聲風夢裡見長發體國門間邊寫聽當對萬東絲兩嚴喪並亂亞產畢畫異眾歲歸氣漢滿爾獨現盡禮離種稱穩競總維綠網羅聖聞聯腦臉臺與舉葉號處備復夠頭學寶尋導層嶺島嶼幫幾廣廳彈強錄徑徹憶懷態戰戲戶攜擁擇據擔擴擺數斷曆書機殺條極標樣權歡歲歷殘殼毀決沉潔潛灣濃燈爐爭牆獲環產療監盤礙確碼禪積稱穀窮簽籠糾紅級純納紛紙統經絕繼續網羅罰職聽膚膽臺與舉艱藝節蘇蘭虛蟲補裝觀觸計訊討訓議證識譜變讓貝負財責賢敗賬貨質贊趕躍軟轉輪輕辦邁還進遠選遞邊醫釋鎖錯長門閃閉問闊隊陽陰陣險隨雜雙難電靜頁頂順頓顏題類飛飯飲餘館馬驚驗髮鬥魚鳥鳴麥黃齊齒龍彎區",
    "许还记后来乐们着么这个为说开护该实时泪旧细终点应会过从将无与让爱声风梦里见长发体国门间边写听当对万东丝两严丧并乱亚产毕画异众岁归气汉满尔独现尽礼离种称稳竞总维绿网罗圣闻联脑脸台与举叶号处备复够头学宝寻导层岭岛屿帮几广厅弹强录径彻忆怀态战戏户携拥择据担扩摆数断历书机杀条极标样权欢岁历残壳毁决沉洁潜湾浓灯炉争墙获环产疗监盘碍确码禅积称谷穷签笼纠红级纯纳纷纸统经绝继续网罗罚职听肤胆台与举艰艺节苏兰虚虫补装观触计讯讨训议证识谱变让贝负财责贤败账货质赞赶跃软转轮轻办迈还进远选递边医释锁错长门闪闭问阔队阳阴阵险随杂双难电静页顶顺顿颜题类飞饭饮余馆马惊验发斗鱼鸟鸣麦黄齐齿龙弯区"
)


def get_model():
    global _model
    if _model is None:
        with _model_lock:
            if _model is None:
                _model = WhisperModel(MODEL_NAME, device=DEVICE, compute_type=COMPUTE_TYPE)
    return _model


def validate_request(request: TranscribeRequest):
    parsed = urlparse(request.url)
    host = (parsed.hostname or "").lower()
    if parsed.scheme not in {"http", "https"} or not (host == "bilibili.com" or host.endswith(".bilibili.com")):
        raise HTTPException(status_code=400, detail="只允许识别 bilibili.com 页面 URL")


def _is_extension_origin(origin):
    if SETTINGS.extension_origins:
        return origin in SETTINGS.extension_origins
    parsed = urlparse(origin)
    return parsed.scheme in {"chrome-extension", "moz-extension"} and bool(parsed.netloc)


def _require_token(token):
    if not token or not hmac.compare_digest(token, _API_TOKEN):
        raise HTTPException(status_code=401, detail="API token 无效")


def _remaining(deadline):
    return deadline - time.monotonic()


def _bounded_timeout(deadline, requested):
    remaining = _remaining(deadline)
    if remaining <= 0:
        raise TimeoutError("请求处理超时")
    return max(0.1, min(float(requested), remaining))


def _stop_process(process):
    if process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=2)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=2)


def run_command(args, error_prefix, timeout, cancel_event=None, deadline=None):
    from job_manager import JobCancelled

    effective_deadline = min(
        time.monotonic() + float(timeout),
        deadline if deadline is not None else float("inf"),
    )
    try:
        process = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=500, detail=f"{error_prefix}：未找到 ffmpeg，请先安装并加入 PATH") from exc
    try:
        while process.poll() is None:
            if cancel_event is not None and cancel_event.is_set():
                _stop_process(process)
                raise JobCancelled("识别任务已取消")
            if time.monotonic() >= effective_deadline:
                _stop_process(process)
                raise HTTPException(status_code=504, detail=f"{error_prefix}：执行超时")
            time.sleep(0.05)
        stdout, stderr = process.communicate()
    except BaseException:
        _stop_process(process)
        raise
    if process.returncode:
        detail = (stderr or stdout or "未知错误").strip()
        raise HTTPException(status_code=500, detail=f"{error_prefix}：{detail[-1000:]}")


def run_model_transcription(wav_path, language, vad_filter, cancel_event, deadline):
    from job_manager import JobCancelled

    acquired = False
    future = None
    inference_dir = None
    try:
        while not acquired:
            if cancel_event is not None and cancel_event.is_set():
                raise JobCancelled("识别任务已取消")
            if _remaining(deadline) <= 0:
                raise TimeoutError("请求处理超时")
            acquired = _model_inference_slot.acquire(timeout=min(0.1, _remaining(deadline)))
        inference_dir = tempfile.mkdtemp(prefix="bls-whisper-inference-")
        inference_path = os.path.join(inference_dir, "clip.wav")
        shutil.copyfile(wav_path, inference_path)

        def infer():
            try:
                segments, info = get_model().transcribe(
                    inference_path, language=language, vad_filter=vad_filter, **MODEL_TRANSCRIBE_PARAMS,
                )
                return list(segments), info
            finally:
                shutil.rmtree(inference_dir, ignore_errors=True)

        future = _model_executor.submit(infer)
        while True:
            if cancel_event is not None and cancel_event.is_set():
                raise JobCancelled("识别任务已取消")
            remaining = _remaining(deadline)
            if remaining <= 0:
                raise TimeoutError("请求处理超时")
            try:
                return future.result(timeout=min(0.1, remaining))
            except concurrent.futures.TimeoutError:
                continue
    finally:
        if inference_dir is not None and future is None:
            shutil.rmtree(inference_dir, ignore_errors=True)
        if acquired:
            if future is not None and not future.done():
                future.add_done_callback(lambda _future: _model_inference_slot.release())
            else:
                _model_inference_slot.release()


TITLE_NOISE_RE = _compile_cfg_patterns("title_cleanup.removable_terms")
DATE_RE = _compile_cfg_patterns("title_cleanup.date_patterns")
VENUE_SUFFIX_RE = _compile_cfg_patterns("title_cleanup.venue_suffix_patterns")
ARTIST_HINT_RE = _compile_cfg_patterns("uploader.trusted_official_markers")
BRACKET_RE = _compile_cfg_patterns("metadata.bracket_patterns")
DISCOVERY_TITLE_RES = [_compile_cfg_pattern(f"metadata.discovery_title_patterns.{index}")
                       for index, _ in enumerate(_cfg("metadata.discovery_title_patterns", list))]
BOOK_TITLE_RE = _compile_cfg_pattern("metadata.book_title_pattern")
ARTIST_TITLE_SEPARATOR_RE = _compile_cfg_patterns("metadata.artist_title_separators")
ALIGNMENT_CONFIDENCE_THRESHOLD = _cfg_number("candidate_filter.alignment_confidence_threshold")
PAUSE_SPLIT_THRESHOLD = _cfg_number("lrc_sync.pause_split_threshold")
MINIMUM_PIECE_DURATION = _cfg_number("lrc_sync.minimum_piece_duration")
LINE_END_PADDING = _cfg_number("lrc_sync.line_end_padding")


def _compact_title_part(value):
    value = TITLE_NOISE_RE.sub(" ", str(value or ""))
    value = DATE_RE.sub(" ", value)
    value = VENUE_SUFFIX_RE.sub(" ", value)
    value = re.sub(r"[【】\[\]（）(){}]+", " ", value)
    value = re.sub(r"(?:\+|/)+", " ", value)
    return re.sub(r"\s+", " ", value).strip(" -_·:：—–")


def _clean_video_title(title):
    value = html.unescape(str(title or ""))

    def clean_bracket(match):
        content = next(part for part in match.groups() if part is not None)
        return f" {content} " if not TITLE_NOISE_RE.search(content) else " "

    value = BRACKET_RE.sub(clean_bracket, value)
    value = DATE_RE.sub(" ", value)
    value = VENUE_SUFFIX_RE.sub(" ", value)
    value = TITLE_NOISE_RE.sub(" ", value)
    value = re.sub(r"[｜|丨]+", " - ", value)
    value = re.sub(r"\s*[-—–_:：]+\s*", " - ", value)
    return re.sub(r"\s+", " ", value).strip(" -_·")[:120]


def _metadata_text_values(video_info):
    info = video_info or {}
    values = []
    for key in ("track", "artist", "album_artist", "creator", "alt_title", "description"):
        value = info.get(key)
        if value:
            values.append(str(value))
    for key in ("tags", "categories"):
        values.extend(str(value) for value in (info.get(key) or []) if value)
    return values


def _explicit_song_from_metadata(video_info):
    info = video_info or {}
    if info.get("track"):
        return _compact_title_part(info["track"])
    for key in ("total_title", "original_title", "page_title"):
        value = str(info.get(key) or "")
        book_match = BOOK_TITLE_RE.search(value)
        if book_match:
            return _compact_title_part(book_match.group(1))
    for value in _metadata_text_values(info):
        book_match = BOOK_TITLE_RE.search(value)
        if book_match:
            return _compact_title_part(book_match.group(1))
        for pattern in DISCOVERY_TITLE_RES:
            match = pattern.search(value)
            if match:
                return _compact_title_part(match.group(1))
    return ""


def _candidate_is_artist(candidate, video_info):
    normalized = normalize_lyric_text(candidate)
    if not normalized:
        return False
    info = video_info or {}
    for key in ("artist", "album_artist", "creator"):
        if normalized == normalize_lyric_text(info.get(key, "")):
            return True
    for tag in info.get("tags") or []:
        if normalized == normalize_lyric_text(tag):
            return True
    return False


def _trusted_uploader_artist(uploader, title, artist_candidates):
    uploader_text = str(uploader or "").strip()
    normalized = normalize_lyric_text(uploader_text)
    if not normalized:
        return ""
    if any(lyric_similarity(uploader_text, candidate) >= _cfg_number("candidate_filter.artist_match_threshold")
           for candidate in artist_candidates if candidate):
        return uploader_text
    if ARTIST_HINT_RE.search(uploader_text) and normalized in normalize_lyric_text(title):
        return re.sub(ARTIST_HINT_RE, " ", uploader_text).strip(" -_·")
    return ""


def _extract_artist_and_song(title, uploader="", video_info=None):
    original = html.unescape(str(title or ""))
    cleaned = _clean_video_title(original)
    artist = ""
    song = ""

    book = BOOK_TITLE_RE.search(original) if _cfg("metadata.extract_book_title", bool) else None
    if book:
        song = _compact_title_part(book.group(1))
        prefix = _compact_title_part(original[:book.start()])
        suffix = _compact_title_part(original[book.end():])
        if prefix:
            artist = ARTIST_TITLE_SEPARATOR_RE.split(prefix)[-1].strip()
            presenter_match = re.search(
                r"(?:听|赏|演唱|歌手)\s*([A-Za-z0-9\u3400-\u9fff·]{2,40})$",
                artist,
                re.I,
            )
            if presenter_match:
                artist = presenter_match.group(1)
        elif suffix:
            artist = ARTIST_TITLE_SEPARATOR_RE.split(suffix)[0].strip()
    else:
        parts = [part.strip() for part in ARTIST_TITLE_SEPARATOR_RE.split(cleaned) if part.strip()]
        if len(parts) == 2:
            left, right = parts
            if _candidate_is_artist(right, video_info):
                song, artist = left, right
            elif _candidate_is_artist(left, video_info):
                artist, song = left, right
            elif re.search(r"\b(?:feat\.?|ft\.?)\b", right, re.I):
                artist, song = left, right
            else:
                artist, song = left, right
        else:
            song = cleaned

    metadata_song = _explicit_song_from_metadata(video_info)
    if metadata_song and (not song or normalize_lyric_text(metadata_song) in normalize_lyric_text(song)):
        song = metadata_song
    artist = _compact_title_part(artist)
    song = _compact_title_part(song)
    if not artist:
        explicit_artists = [str((video_info or {}).get(key) or "") for key in ("artist", "album_artist", "creator")]
        artist = next((value for value in explicit_artists if value), "")
    if not artist:
        info = video_info or {}
        alternate_info = {key: value for key, value in info.items()
                          if key not in {"page_title", "total_title", "original_title"}}
        for key in ("total_title", "original_title", "page_title"):
            alternate_title = str(info.get(key) or "")
            if alternate_title and alternate_title != original:
                alternate_artist, _ = _extract_artist_and_song(alternate_title, uploader, alternate_info)
                if alternate_artist:
                    artist = alternate_artist
                    break
    if not artist:
        artist = _trusted_uploader_artist(uploader, original, [])
    if artist and normalize_lyric_text(artist) == normalize_lyric_text(song):
        artist = ""
    return artist[:80], song[:120]


def _build_search_queries(song_title, artist=""):
    if not song_title:
        return []
    templates = _cfg("search.query_templates", dict)
    category = "latin" if re.search(r"[A-Za-z\u3040-\u30ff]", song_title) else "non_latin"
    selected = list(_cfg(f"search.query_templates.{'with_artist' if artist else 'without_artist'}", list))
    if artist:
        selected.extend(_cfg("search.query_templates.without_artist", list))
    selected.extend(_cfg(f"search.query_templates.{category}", list))
    queries = []
    for template in selected:
        if not isinstance(template, str) or "{song}" not in template:
            raise TypeError(f"歌词配置项 search.query_templates.{category} 必须包含使用 {{song}} 的字符串模板")
        try:
            queries.append(template.format(artist=artist, song=song_title))
        except (KeyError, ValueError) as exc:
            raise ValueError(f"歌词搜索模板非法: {template}: {exc}") from exc
    return list(dict.fromkeys(re.sub(r"\s+", " ", query).strip() for query in queries))[:MAX_SEARCH_QUERIES]


def extract_song_metadata(title, uploader="", video_info=None):
    info = video_info if isinstance(video_info, dict) else {}
    artist, song_title = _extract_artist_and_song(title, uploader, info)
    raw_text = "\n".join([str(title or "")] + [str(info.get(key) or "") for key in (
        "total_title", "original_title", "page_title", "title", "description", "dynamic",
    )])
    version_patterns = (
        ("live", r"\blive\b|现场|現場|演唱会|演唱會|音乐节|音樂節"),
        ("cover", r"\bcover\b|翻唱|试唱|試唱"),
        ("instrumental", r"\binstrumental\b|伴奏|无人声|無人聲|off\s*vocal|karaoke"),
        ("remix", r"\bremix\b|重混|混音版"),
        ("remaster", r"\bremaster(?:ed)?\b|重制|修复版|修復版"),
        ("edit", r"\bedit\b|剪辑版|剪輯版|片段"),
    )
    version_terms = [name for name, pattern in version_patterns if re.search(pattern, raw_text, re.I)]
    language = str(info.get("metadata_language") or info.get("language") or "auto")
    version = "/".join(version_terms)
    return {
        "song_title": song_title,
        "artist": artist,
        "duration": info.get("duration"),
        "search_queries": _build_search_queries(song_title, artist),
        "language": language,
        "version": version,
        "is_live": "live" in version_terms,
        "is_cover": "cover" in version_terms,
        "is_instrumental": "instrumental" in version_terms,
        "version_terms": version_terms,
    }


def normalize_lyric_text(text):
    value = html.unescape(str(text or "")).lower().translate(TRADITIONAL_TO_SIMPLIFIED)
    value = re.sub(r"\([^)]*(?:和声|重复|repeat|music)[^)]*\)", "", value, flags=re.I)
    return "".join(char for char in value if char.isalnum())


def clean_lyrics_text(text):
    """把临时抓取的页面文本清洗为歌词行；不会持久化网页或完整歌词。"""
    value = html.unescape(re.sub(r"<[^>]{0,500}>", "\n", str(text or "")))
    value = TIME_TAG_RE.sub("", value)
    lines = []
    occurrences = {}
    for raw in value.replace("\r", "\n").split("\n"):
        line = re.sub(r"\s+", " ", raw).strip(" \t|·•-—_")
        line = re.sub(r"^(?:歌词|lyrics?)\s*[:：]?\s*", "", line, flags=re.I)
        if not line or len(line) < 2 or len(line) > 100:
            continue
        if UI_ONLY_RE.fullmatch(line) or METADATA_RE.search(line) or NOISE_RE.search(line):
            continue
        if re.fullmatch(r"/?\s*首页(?:\s*/\s*[^/\n]+){1,4}\s*/?", line, re.I):
            continue
        if re.search(r"https?://|www\.|@\w+", line, re.I):
            continue
        normalized = normalize_lyric_text(line)
        if len(normalized) < 2:
            continue
        letters = sum(char.isalnum() for char in line)
        if letters / max(1, len(line)) < 0.45:
            continue
        if len(line.split()) > 18 and not re.search(r"[，。！？,.!?]", line):
            continue
        occurrences[normalized] = occurrences.get(normalized, 0) + 1
        if occurrences[normalized] <= 4:
            lines.append(line)
    return lines[:300]


def parse_lrc_timed_lines(text):
    """解析网页或纯文本中的 LRC 时间标签，不保留原始网页内容。"""
    value = html.unescape(str(text or ""))
    value = re.sub(r"<[^>]{0,1000}>", "\n", value)
    value = LRC_METADATA_RE.sub("", value)
    parsed = []
    for raw_line in value.replace("\r", "\n").split("\n"):
        matches = list(LRC_TIME_RE.finditer(raw_line))
        if not matches:
            continue
        line = LRC_TIME_RE.sub("", raw_line)
        line = re.sub(r"\s+", " ", line).strip(" \t|·•-—_")
        line = re.sub(r"^(?:歌词|lyrics?)\s*[:：]?\s*", "", line, flags=re.I)
        if not line:
            continue
        if len(line) > 160 or UI_ONLY_RE.fullmatch(line) or METADATA_RE.search(line) or NOISE_RE.search(line):
            continue
        if re.search(r"https?://|www\.|@\w+", line, re.I):
            continue
        normalized = normalize_lyric_text(line)
        if len(normalized) < 2:
            continue
        for match in matches:
            timestamp = match.group(1)
            parts = timestamp.split(":")
            if len(parts) == 3:
                hour, minute, second_text = int(parts[0]), int(parts[1]), parts[2]
            else:
                hour, minute, second_text = 0, int(parts[0]), parts[1]
            second_parts = re.split(r"[.:]", second_text, maxsplit=1)
            second = int(second_parts[0])
            fraction_text = second_parts[1] if len(second_parts) > 1 else ""
            if minute >= 60 or second >= 60:
                continue
            fraction = int(fraction_text) / (10 ** len(fraction_text)) if fraction_text else 0.0
            parsed.append({"start": float(hour * 3600 + minute * 60 + second + fraction), "text": line})

    grouped = {}
    script_counts = {}
    for item in parsed:
        timestamp = round(item["start"], 3)
        normalized = normalize_lyric_text(item["text"])
        if not normalized:
            continue
        group = grouped.setdefault(timestamp, [])
        if any(normalize_lyric_text(existing["text"]) == normalized for existing in group):
            continue
        group.append(item)
        script = _line_script(item["text"])
        script_counts[script] = script_counts.get(script, 0) + 1

    priority = _cfg("script_tracks.priority", list)
    primary_script = max(
        priority,
        key=lambda script: (script_counts.get(script, 0), -priority.index(script)),
    ) if script_counts else None
    ordered = []
    for timestamp in sorted(grouped):
        group = grouped[timestamp]
        selected = next((item for item in group if _line_script(item["text"]) == primary_script), group[0])
        ordered.append(selected)

    raw_intervals = [ordered[index + 1]["start"] - item["start"]
                     for index, item in enumerate(ordered[:-1])]
    for index, interval in enumerate(raw_intervals[:-1]):
        if interval >= _cfg_number("lrc_sync.local_support_time_tolerance"):
            continue
        repeated_intervals = sorted(
            raw_intervals[other_index]
            for other_index, other in enumerate(ordered[:-1])
            if other_index != index
            and lyric_similarity(other["text"], ordered[index]["text"]) >= _cfg_number("lrc_sync.local_support_similarity")
            and raw_intervals[other_index] >= _cfg_number("lrc_sync.local_support_time_tolerance")
        )
        if not repeated_intervals:
            continue
        middle = len(repeated_intervals) // 2
        inferred_interval = (repeated_intervals[middle] if len(repeated_intervals) % 2
                             else (repeated_intervals[middle - 1] + repeated_intervals[middle]) / 2)
        inferred_start = ordered[index]["start"] + inferred_interval
        following_start = ordered[index + 2]["start"]
        if inferred_start < following_start - _cfg_number("lrc_sync.min_clipped_duration"):
            ordered[index + 1]["start"] = inferred_start
    result = []
    for index, item in enumerate(ordered):
        end = ordered[index + 1]["start"] if index + 1 < len(ordered) else item["start"] + 4.0
        if end > item["start"]:
            result.append({"start": round(item["start"], 3), "end": round(end, 3), "text": item["text"]})
    return result


def lyric_similarity(first, second):
    a = normalize_lyric_text(first)
    b = normalize_lyric_text(second)
    if not a or not b:
        return 0.0
    ratio = SequenceMatcher(None, a, b, autojunk=False).ratio()
    if min(len(a), len(b)) >= 5 and (a in b or b in a):
        ratio = max(ratio, min(len(a), len(b)) / max(len(a), len(b)) * 0.92 + 0.08)
    return min(1.0, ratio)


def align_lyrics_to_segments(segments, lyric_lines, confidence_threshold=None):
    """动态规划单调对齐；保留 Whisper 时间轴，仅按置信度替换文本。"""
    confidence_threshold = ALIGNMENT_CONFIDENCE_THRESHOLD if confidence_threshold is None else confidence_threshold
    count = len(segments)
    lyric_count = len(lyric_lines)
    negative = -10**9
    scores = [[negative] * (lyric_count + 1) for _ in range(count + 1)]
    paths = [[None] * (lyric_count + 1) for _ in range(count + 1)]
    scores[0][0] = 0.0
    for i in range(count + 1):
        for j in range(lyric_count + 1):
            current = scores[i][j]
            if current == negative:
                continue
            if j < lyric_count and current > scores[i][j + 1]:
                scores[i][j + 1] = current
                paths[i][j + 1] = (i, j, "skip", 0.0, None)
            if i < count and current - 0.03 > scores[i + 1][j]:
                scores[i + 1][j] = current - 0.03
                paths[i + 1][j] = (i, j, "keep", 0.0, None)
            if i < count:
                for width in (1, 2):
                    if j + width > lyric_count:
                        continue
                    replacement = " ".join(lyric_lines[j:j + width])
                    confidence = lyric_similarity(segments[i].get("text", ""), replacement)
                    gain = confidence - confidence_threshold
                    if gain <= 0:
                        continue
                    target = current + gain + 0.04
                    if target > scores[i + 1][j + width]:
                        scores[i + 1][j + width] = target
                        paths[i + 1][j + width] = (i, j, "match", confidence, replacement)

    end_j = max(range(lyric_count + 1), key=lambda j: scores[count][j])
    matches = {}
    i, j = count, end_j
    while i or j:
        step = paths[i][j]
        if step is None:
            break
        previous_i, previous_j, action, confidence, replacement = step
        if action == "match":
            matches[previous_i] = (replacement, confidence)
        i, j = previous_i, previous_j

    corrected = []
    for index, segment in enumerate(segments):
        item = dict(segment)
        match = matches.get(index)
        if match and match[1] >= confidence_threshold:
            item["text"] = match[0]
            item["corrected"] = True
            item["confidence"] = round(match[1], 4)
        else:
            item["corrected"] = False
            item["confidence"] = round(match[1], 4) if match else 0.0
        corrected.append(item)
    return corrected


def _alignment_units(text):
    """把文本规范化为中文单字/英文单词；标点不进入时间对齐。"""
    value = html.unescape(str(text or "")).lower().translate(TRADITIONAL_TO_SIMPLIFIED)
    return re.findall(r"[a-z0-9]+|[\u3400-\u9fff]|[^\W_]", value, flags=re.UNICODE)


def _joined_alignment_units(units):
    result = ""
    for unit in units:
        if result and unit.isascii() and unit.isalnum() and result[-1].isascii() and result[-1].isalnum():
            result += " "
        result += unit
    return result


def _reasonable_lyric_part(units):
    chinese_count = sum(bool(re.fullmatch(r"[\u3400-\u9fff]", unit)) for unit in units)
    english_count = sum(bool(re.fullmatch(r"[a-z0-9]+", unit)) for unit in units)
    return chinese_count >= 5 or english_count >= 2


def interpolate_words_from_segments(segments):
    words = []
    for segment in segments:
        text = str(segment.get("text", "")).strip()
        units = _alignment_units(text)
        start = float(segment.get("start", 0.0))
        end = float(segment.get("end", start))
        if not units or end <= start:
            continue
        weights = [max(1, len(unit)) for unit in units]
        total = sum(weights)
        cursor = start
        for index, (unit, weight) in enumerate(zip(units, weights)):
            unit_end = end if index == len(units) - 1 else cursor + (end - cursor) * weight / total
            words.append({"text": unit, "start": cursor, "end": unit_end})
            cursor = unit_end
            total -= weight
    return words


def expand_whisper_words(words):
    expanded = []
    for word in words:
        text = str(word.get("text", "")).strip()
        start = float(word.get("start", 0.0))
        end = float(word.get("end", start))
        units = _alignment_units(text)
        if not units or end <= start:
            continue
        weights = [max(1, len(unit)) for unit in units]
        weight_sum = sum(weights)
        elapsed = 0
        for unit, weight in zip(units, weights):
            unit_start = start + (end - start) * elapsed / weight_sum
            elapsed += weight
            unit_end = start + (end - start) * elapsed / weight_sum
            expanded.append({"text": unit, "start": unit_start, "end": unit_end})
    return expanded[:MAX_ALIGNMENT_UNITS]


def _align_lyric_units_to_words(lyric_units, word_units):
    """半全局编辑距离：完整消耗识别流，歌词首尾可自由跳过，路径始终单调。"""
    m, n = len(lyric_units), len(word_units)
    if not m or not n or m > MAX_ALIGNMENT_UNITS or n > MAX_ALIGNMENT_UNITS or m * n > MAX_ALIGNMENT_CELLS:
        return {}, 0
    previous = [-(0.9 * j) for j in range(n + 1)]
    trace = [bytearray(n + 1) for _ in range(m + 1)]
    best_i, best_score = 0, previous[n]
    for i in range(1, m + 1):
        current = [0.0] + [0.0] * n
        lyric_unit = lyric_units[i - 1]
        for j in range(1, n + 1):
            exact = lyric_unit == word_units[j - 1]["text"]
            diagonal = previous[j - 1] + (2.0 if exact else -0.45)
            delete_lyric = previous[j] - 0.7
            insert_word = current[j - 1] - 0.9
            if diagonal >= delete_lyric and diagonal >= insert_word:
                current[j] = diagonal
                trace[i][j] = 1
            elif delete_lyric >= insert_word:
                current[j] = delete_lyric
                trace[i][j] = 2
            else:
                current[j] = insert_word
                trace[i][j] = 3
        if current[n] > best_score:
            best_i, best_score = i, current[n]
        previous = current

    i, j = best_i, n
    anchors = {}
    matched_words = set()
    while i > 0 and j > 0:
        action = trace[i][j]
        if action == 1:
            if lyric_units[i - 1] == word_units[j - 1]["text"]:
                anchors[i - 1] = word_units[j - 1]
                matched_words.add(j - 1)
            i -= 1
            j -= 1
        elif action == 2:
            i -= 1
        elif action == 3:
            j -= 1
        else:
            break
    return anchors, len(matched_words)


def _build_timed_lyrics_details(whisper_words, lyric_lines, clip_start, clip_end):
    words = expand_whisper_words(whisper_words)
    if not words or clip_end <= clip_start:
        return [], {"matched_word_count": 0, "word_count": len(words), "anchored_line_count": 0}

    lyric_units = []
    line_ranges = []
    usable_lines = []
    for line in lyric_lines[:300]:
        units = _alignment_units(line)
        if not units:
            continue
        first = len(lyric_units)
        lyric_units.extend(units)
        line_ranges.append((first, len(lyric_units)))
        usable_lines.append(str(line).strip())
        if len(lyric_units) >= MAX_ALIGNMENT_UNITS:
            break
    anchors, _ = _align_lyric_units_to_words(lyric_units, words)
    timed = []
    anchored_line_count = 0
    matched_word_count = 0
    for (first, last), text in zip(line_ranges, usable_lines):
        line_anchors = [(unit_index, anchors[unit_index]) for unit_index in range(first, last) if unit_index in anchors]
        unit_count = last - first
        if not line_anchors or (len(line_anchors) < 2 and unit_count > _cfg("timeline_quality.single_anchor_max_units", int)):
            timed.append(None)
            continue

        ranges = [(first, last, line_anchors)]
        split_ranges = []
        while ranges:
            range_first, range_last, range_anchors = ranges.pop(0)
            split_at = None
            largest_gap = 0.0
            for left_anchor, right_anchor in zip(range_anchors, range_anchors[1:]):
                gap = float(right_anchor[1]["start"]) - float(left_anchor[1]["end"])
                boundary = right_anchor[0]
                if (
                    gap >= PAUSE_SPLIT_THRESHOLD
                    and gap > largest_gap
                    and _reasonable_lyric_part(lyric_units[range_first:boundary])
                    and _reasonable_lyric_part(lyric_units[boundary:range_last])
                ):
                    split_at = boundary
                    largest_gap = gap
            if split_at is None:
                split_ranges.append((range_first, range_last, range_anchors))
                continue
            left_anchors = [item for item in range_anchors if item[0] < split_at]
            right_anchors = [item for item in range_anchors if item[0] >= split_at]
            ranges[0:0] = [
                (range_first, split_at, left_anchors),
                (split_at, range_last, right_anchors),
            ]

        for range_first, range_last, range_anchors in split_ranges:
            range_unit_count = range_last - range_first
            if not range_anchors or (len(range_anchors) < 2 and range_unit_count > _cfg("timeline_quality.single_anchor_max_units", int)):
                timed.append(None)
                continue
            anchor_ratio = len(range_anchors) / range_unit_count
            confidence = round(min(1.0, 0.35 + anchor_ratio * 0.65), 4)
            if confidence < _cfg_number("timeline_quality.per_line_minimum_confidence"):
                timed.append(None)
                continue
            start = range_anchors[0][1]["start"]
            end = range_anchors[-1][1]["end"] + LINE_END_PADDING
            if end <= start:
                end = start + LINE_END_PADDING
            timed.append({
                "text": text if range_first == first and range_last == last else _joined_alignment_units(lyric_units[range_first:range_last]),
                "start": start,
                "end": end,
                "corrected": True,
                "confidence": confidence,
                "_anchored": True,
            })
            anchored_line_count += 1
            matched_word_count += len(range_anchors)

    result = []
    for item in timed:
        if item is None or item["end"] <= clip_start or item["start"] >= clip_end:
            continue
        item = dict(item)
        item["start"] = max(float(clip_start), item["start"])
        item["end"] = min(float(clip_end), item["end"])
        if result and item["start"] < result[-1]["end"]:
            result[-1]["end"] = max(result[-1]["start"] + MINIMUM_PIECE_DURATION, item["start"])
        if item["end"] - item["start"] < MINIMUM_PIECE_DURATION:
            continue
        item.pop("_anchored", None)
        item["start"] = round(item["start"], 3)
        item["end"] = round(item["end"], 3)
        result.append(item)
    return result, {
        "matched_word_count": matched_word_count,
        "word_count": len(words),
        "anchored_line_count": anchored_line_count,
    }


def _clip_timed_lyric_piece(piece, clip_start, clip_end):
    item = dict(piece)
    original_start = float(item["start"])
    original_end = float(item["end"])
    item["start"] = max(float(clip_start), original_start)
    item["end"] = min(float(clip_end), original_end)
    original_duration = original_end - original_start
    retained_duration = item["end"] - item["start"]
    boundary_fragment = original_start < clip_start or original_end > clip_end
    if (retained_duration < _cfg_number("lrc_sync.min_clipped_duration")
            or boundary_fragment and retained_duration < original_duration * _cfg_number("lrc_sync.min_retained_ratio")):
        return None
    return item


def _best_monotonic_anchor_path(clustered, segment_count):
    tree = [None] * (segment_count + 2)
    scores = [candidate["similarity"] for candidate in clustered]
    counts = [1] * len(clustered)
    previous = [None] * len(clustered)

    def query(index):
        best = None
        while index > 0:
            candidate_index = tree[index]
            if candidate_index is not None:
                rank = (counts[candidate_index], scores[candidate_index])
                if best is None or rank > (counts[best], scores[best]):
                    best = candidate_index
            index -= index & -index
        return best

    def update(index, candidate_index):
        rank = (counts[candidate_index], scores[candidate_index])
        while index < len(tree):
            current = tree[index]
            if current is None or rank > (counts[current], scores[current]):
                tree[index] = candidate_index
            index += index & -index

    group_start = 0
    while group_start < len(clustered):
        group_end = group_start + 1
        while group_end < len(clustered) and clustered[group_end]["lrc_index"] == clustered[group_start]["lrc_index"]:
            group_end += 1
        for index in range(group_start, group_end):
            prior = query(clustered[index]["segment_index"] + 1)
            if prior is not None:
                counts[index] = counts[prior] + 1
                scores[index] = scores[prior] + clustered[index]["similarity"]
                previous[index] = prior
        for index in range(group_start, group_end):
            update(clustered[index]["segment_end"] + 1, index)
        group_start = group_end

    if not clustered:
        return [], (0, 0.0)
    cursor = max(range(len(clustered)), key=lambda index: (counts[index], scores[index]))
    rank = (counts[cursor], scores[cursor])
    path = []
    while cursor is not None:
        path.append(clustered[cursor])
        cursor = previous[cursor]
    return list(reversed(path)), rank


def sync_lrc_to_audio(lrc_lines, whisper_segments, clip_start, clip_end):
    """用单调文本锚点估计歌曲 LRC 到视频音轨的唯一平移量。"""
    clip_start = float(clip_start if clip_start is not None else 0.0)
    clip_end = float(clip_end if clip_end is not None else clip_start)
    details = {"anchor_count": 0, "offset_median": None, "offset_spread": None, "accepted": False,
               "timeline_model": "offset", "offset": None, "scale": 1.0, "rmse": None}
    if not lrc_lines or not whisper_segments or clip_end <= clip_start:
        return [], details
    candidates = []
    for lrc_index, line in enumerate(lrc_lines):
        for segment_index in range(len(whisper_segments)):
            for width in (1, 2, 3):
                if segment_index + width > len(whisper_segments):
                    continue
                text = " ".join(str(item.get("text", "")) for item in whisper_segments[segment_index:segment_index + width])
                similarity = lyric_similarity(line.get("text", ""), text)
                if similarity >= _cfg_number("lrc_sync.similarity_threshold"):
                    candidates.append({
                        "lrc_index": lrc_index,
                        "segment_index": segment_index,
                        "segment_end": segment_index + width,
                        "similarity": similarity,
                        "actual_start": float(whisper_segments[segment_index]["start"]),
                        "offset": float(whisper_segments[segment_index]["start"]) - float(line["start"]),
                    })
    candidates.sort(key=lambda item: (item["lrc_index"], item["segment_index"], item["segment_end"]))
    anchors = []
    best_rank = (0, 0.0)
    tolerance = _cfg_number("lrc_sync.offset_cluster_tolerance")
    offset_sorted = sorted(candidates, key=lambda item: item["offset"])
    offset_values = [candidate["offset"] for candidate in offset_sorted]
    seen_clusters = set()
    for seed in offset_sorted:
        left = bisect.bisect_left(offset_values, seed["offset"] - tolerance)
        right = bisect.bisect_right(offset_values, seed["offset"] + tolerance)
        cluster_key = (left, right)
        if cluster_key in seen_clusters:
            continue
        seen_clusters.add(cluster_key)
        clustered = sorted(
            offset_sorted[left:right],
            key=lambda item: (item["lrc_index"], item["segment_index"], item["segment_end"]),
        )
        path, rank = _best_monotonic_anchor_path(clustered, len(whisper_segments))
        if rank > best_rank:
            best_rank = rank
            anchors = path
    if not anchors:
        return [], details
    display_lead = _cfg_number("lrc_sync.display_lead_seconds")
    scale = 1.0
    linear_accepted = False
    linear_anchors, _ = _best_monotonic_anchor_path(candidates, len(whisper_segments))
    if len(linear_anchors) >= _cfg("lrc_sync.linear_min_anchors", int):
        linear_lrc_times = [float(lrc_lines[anchor["lrc_index"]]["start"]) for anchor in linear_anchors]
        linear_actual_times = [anchor["actual_start"] for anchor in linear_anchors]
        linear_span = max(linear_lrc_times) - min(linear_lrc_times)
        if linear_span >= _cfg_number("lrc_sync.linear_min_span"):
            linear_offsets = sorted(anchor["offset"] for anchor in linear_anchors)
            middle = len(linear_offsets) // 2
            linear_offset_median = (linear_offsets[middle] if len(linear_offsets) % 2
                                    else (linear_offsets[middle - 1] + linear_offsets[middle]) / 2)
            fixed_rmse = math.sqrt(sum(
                (y - (x + linear_offset_median)) ** 2
                for x, y in zip(linear_lrc_times, linear_actual_times)
            ) / len(linear_anchors))
            mean_x = sum(linear_lrc_times) / len(linear_lrc_times)
            mean_y = sum(linear_actual_times) / len(linear_actual_times)
            variance = sum((value - mean_x) ** 2 for value in linear_lrc_times)
            fitted_scale = sum(
                (x - mean_x) * (y - mean_y) for x, y in zip(linear_lrc_times, linear_actual_times)
            ) / variance if variance else 1.0
            fitted_offset = mean_y - fitted_scale * mean_x
            fitted_rmse = math.sqrt(sum(
                (y - (fitted_scale * x + fitted_offset)) ** 2
                for x, y in zip(linear_lrc_times, linear_actual_times)
            ) / len(linear_anchors))
            average_similarity = sum(anchor["similarity"] for anchor in linear_anchors) / len(linear_anchors)
            linear_accepted = (
                _cfg_number("lrc_sync.linear_scale_min") <= fitted_scale <= _cfg_number("lrc_sync.linear_scale_max")
                and fixed_rmse - fitted_rmse >= _cfg_number("lrc_sync.linear_rmse_improvement")
                and fitted_rmse <= _cfg_number("lrc_sync.max_offset_spread")
                and average_similarity >= _cfg_number("lrc_sync.similarity_threshold")
            )
            if linear_accepted:
                anchors = linear_anchors
                scale, model_offset, rmse = fitted_scale, fitted_offset, fitted_rmse
                details["timeline_model"] = "linear"
    offsets = sorted(anchor["offset"] for anchor in anchors)
    middle = len(offsets) // 2
    offset_median = offsets[middle] if len(offsets) % 2 else (offsets[middle - 1] + offsets[middle]) / 2
    offset_spread = max(offsets) - min(offsets)
    if not linear_accepted:
        model_offset = offset_median
        rmse = math.sqrt(sum(
            (anchor["actual_start"] - (float(lrc_lines[anchor["lrc_index"]]["start"]) + offset_median)) ** 2
            for anchor in anchors
        ) / len(anchors))

    def mapped_lrc_time(lrc_time, line_index):
        return float(lrc_time) * scale + model_offset - display_lead
    short_clip = clip_end - clip_start <= _cfg_number("lrc_sync.short_clip_max_duration")
    accepted = linear_accepted or (
        len(anchors) >= 2
        or short_clip and anchors[0]["similarity"] >= _cfg_number("lrc_sync.single_anchor_threshold")
    ) and offset_spread <= _cfg_number("lrc_sync.max_offset_spread")
    reliable_anchors = [{
        "lrc_index": anchor["lrc_index"],
        "lrc_time": round(float(lrc_lines[anchor["lrc_index"]]["start"]), 3),
        "actual_time": round(anchor["actual_start"], 3),
        "synced_time": round(float(lrc_lines[anchor["lrc_index"]]["start"]) + offset_median, 3),
    } for anchor in anchors]
    earliest_anchor = min(anchors, key=lambda item: item["lrc_index"])
    latest_anchor = max(anchors, key=lambda item: item["lrc_index"])
    earliest_anchor_time = mapped_lrc_time(lrc_lines[earliest_anchor["lrc_index"]]["start"], earliest_anchor["lrc_index"]) + display_lead
    latest_anchor_time = mapped_lrc_time(lrc_lines[latest_anchor["lrc_index"]]["start"], latest_anchor["lrc_index"]) + display_lead
    details.update({
        "anchor_count": len(anchors),
        "offset_median": round(offset_median, 4),
        "offset": round(model_offset, 4),
        "scale": round(scale, 6),
        "rmse": round(rmse, 4),
        "offset_spread": round(offset_spread, 4),
        "accepted": accepted,
        "average_confidence": round(sum(item["similarity"] for item in anchors) / len(anchors), 4),
        "high_confidence_ratio": round(sum(
            item["similarity"] >= _cfg_number("timeline_quality.high_confidence_threshold")
            for item in anchors
        ) / len(anchors), 4),
        "reliable_anchors": reliable_anchors,
        "earliest_anchor_time": round(earliest_anchor_time, 3),
        "pruned_unanchored_count": 0,
    })
    if not accepted:
        return [], details

    anchors_by_line = {anchor["lrc_index"]: anchor for anchor in anchors}
    normalized_counts = {}
    for line in lrc_lines:
        normalized = normalize_lyric_text(line.get("text", ""))
        normalized_counts[normalized] = normalized_counts.get(normalized, 0) + 1
    locally_supported_lines = {
        candidate["lrc_index"] for candidate in candidates
        if candidate["similarity"] >= _cfg_number("lrc_sync.local_support_similarity")
        and abs(candidate["actual_start"] - (mapped_lrc_time(lrc_lines[candidate["lrc_index"]]["start"], candidate["lrc_index"]) + display_lead)) <= _cfg_number("lrc_sync.local_support_time_tolerance")
    }
    repeated_fallback_support = set()
    for normalized, count in normalized_counts.items():
        if count <= 1 or any(normalize_lyric_text(lrc_lines[index].get("text", "")) == normalized
                             for index in anchors_by_line):
            continue
        local_matches = []
        for index, line in enumerate(lrc_lines):
            if normalize_lyric_text(line.get("text", "")) != normalized:
                continue
            shifted_start = mapped_lrc_time(line["start"], index)
            best_similarity = max((
                lyric_similarity(line.get("text", ""), " ".join(
                    str(item.get("text", "")) for item in whisper_segments[segment_index:segment_index + width]
                ))
                for segment_index in range(len(whisper_segments))
                for width in (1, 2, 3)
                if segment_index + width <= len(whisper_segments)
                and abs(float(whisper_segments[segment_index]["start"]) - shifted_start) <= _cfg_number("lrc_sync.local_support_time_tolerance")
            ), default=0.0)
            local_matches.append((best_similarity, index))
        best_similarity = max((item[0] for item in local_matches), default=0.0)
        if best_similarity >= _cfg_number("lrc_sync.repeated_fallback_similarity"):
            repeated_fallback_support.update(index for similarity, index in local_matches
                                             if similarity == best_similarity)
    prefix_boundary_time = min(
        [earliest_anchor_time] + [mapped_lrc_time(lrc_lines[index]["start"], index) + display_lead
                                  for index in repeated_fallback_support]
    )
    details["earliest_anchor_time"] = round(prefix_boundary_time, 3)
    result = []
    for index, line in enumerate(lrc_lines):
        shifted_start = mapped_lrc_time(line["start"], index)
        shifted_end = mapped_lrc_time(line["end"], index)
        normalized = normalize_lyric_text(line.get("text", ""))
        preserve_between_anchors = (
            _cfg("lrc_sync.preserve_between_anchors", bool)
            and earliest_anchor["lrc_index"] <= index <= latest_anchor["lrc_index"]
        )
        repeated_without_support = (normalized_counts.get(normalized, 0) > 1
                                    and index not in anchors_by_line
                                    and index not in locally_supported_lines
                                    and index not in repeated_fallback_support
                                    and not preserve_between_anchors)
        unsupported_prefix = (normalized_counts.get(normalized, 0) > 1
                              and shifted_start < prefix_boundary_time - _cfg_number("lrc_sync.prefix_tolerance")
                              and index not in locally_supported_lines
                              and index not in repeated_fallback_support)
        unsupported_tail = (index > latest_anchor["lrc_index"] + _cfg("lrc_sync.tail_allowance_lines", int)
                            and shifted_start > latest_anchor_time + _cfg_number("lrc_sync.tail_allowance_seconds")
                            and index not in locally_supported_lines)
        if repeated_without_support or unsupported_prefix or unsupported_tail:
            details["pruned_unanchored_count"] += 1
            continue
        if shifted_end <= clip_start or shifted_start >= clip_end:
            continue
        confidence = details["average_confidence"]
        anchor = anchors_by_line.get(index)
        if anchor:
            confidence = anchor["similarity"]
        for piece in [{"text": line["text"], "start": shifted_start, "end": shifted_end,
                       "corrected": True, "confidence": round(confidence, 4)}]:
            item = _clip_timed_lyric_piece(piece, clip_start, clip_end)
            if item is None:
                continue
            if result and item["start"] < result[-1]["end"]:
                result[-1]["end"] = max(result[-1]["start"] + MINIMUM_PIECE_DURATION, item["start"])
            if item["end"] - item["start"] < MINIMUM_PIECE_DURATION:
                continue
            item["start"] = round(item["start"], 3)
            item["end"] = round(item["end"], 3)
            result.append(item)
    return result, details


def _unwrap_search_url(href):
    try:
        absolute = urljoin("https://www.bing.com", html.unescape(href))
        parsed = urlparse(absolute)
        if parsed.hostname and parsed.hostname.endswith("bing.com"):
            query = parse_qs(parsed.query)
            for key in ("url", "u", "r"):
                if query.get(key):
                    absolute = unquote(query[key][0])
                    break
        elif parsed.hostname and parsed.hostname.endswith("duckduckgo.com"):
            query = parse_qs(parsed.query)
            if query.get("uddg"):
                absolute = unquote(query["uddg"][0])
        parsed = urlparse(absolute)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            return None
        if parsed.hostname.lower().endswith(("bing.com", "duckduckgo.com")):
            return None
        return absolute
    except ValueError:
        return None


def _allowed_lyrics_host(host):
    return any(host == allowed or host.endswith(f".{allowed}") for allowed in ALLOWED_LYRICS_HOSTS)


def _validate_public_url(url):
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("不允许的网页地址")
    host = parsed.hostname.lower().rstrip(".")
    if not _allowed_lyrics_host(host):
        raise ValueError("歌词站点不在允许列表")
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    addresses = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    public_addresses = []
    for address in addresses:
        ip = ipaddress.ip_address(address[4][0])
        if not ip.is_global:
            raise ValueError("拒绝访问本机或私网地址")
        public_addresses.append((address[0], address[4][0]))
    if not public_addresses:
        raise ValueError("网页域名无法解析")
    return parsed, public_addresses[0]


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    def __init__(self, host, ip_address, port, timeout):
        super().__init__(host, port=port, timeout=timeout, context=ssl.create_default_context())
        self._ip_address = ip_address

    def connect(self):
        raw_socket = socket.create_connection((self._ip_address, self.port), self.timeout)
        self.sock = self._context.wrap_socket(raw_socket, server_hostname=self.host)


def _fetch_public_text(url, timeout, max_bytes, allowed_types, deadline=None):
    current = url
    for _ in range(4):
        parsed, (_, ip_address) = _validate_public_url(current)
        request_timeout = _bounded_timeout(deadline, timeout) if deadline is not None else timeout
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        connection = (_PinnedHTTPSConnection(parsed.hostname, ip_address, port, request_timeout)
                      if parsed.scheme == "https"
                      else http.client.HTTPConnection(ip_address, port=port, timeout=request_timeout))
        path = parsed.path or "/"
        if parsed.query:
            path += f"?{parsed.query}"
        host_header = parsed.hostname if parsed.port is None else f"{parsed.hostname}:{parsed.port}"
        try:
            connection.request("GET", path, headers={
                "Host": host_header,
                "User-Agent": WEB_USER_AGENT,
                "Accept": "text/html,application/xhtml+xml,text/plain,application/json;q=0.8",
                "Connection": "close",
            })
            response = connection.getresponse()
            if response.status in {301, 302, 303, 307, 308} and response.headers.get("Location"):
                current = urljoin(current, response.headers["Location"])
                continue
            if response.status >= 400:
                raise HTTPError(current, response.status, response.reason, response.headers, None)
            content_type = response.headers.get_content_type().lower()
            if content_type not in allowed_types:
                raise ValueError(f"不支持的 Content-Type: {content_type}")
            content_length = response.headers.get("Content-Length")
            if content_length and int(content_length) > max_bytes:
                raise ValueError("网页响应体过大")
            body = response.read(max_bytes + 1)
            if len(body) > max_bytes:
                raise ValueError("网页响应体过大")
            charset = response.headers.get_content_charset() or "utf-8"
            try:
                return body.decode(charset, errors="replace"), current
            except LookupError:
                return body.decode("utf-8", errors="replace"), current
        finally:
            connection.close()
    raise ValueError("网页重定向次数过多")


def _bing_rss_results(page):
    results = []
    root = ET.fromstring(page)
    for node in root.findall(".//item"):
        url = _unwrap_search_url((node.findtext("link") or "").strip())
        title = " ".join((node.findtext("title") or "").split())
        snippet = " ".join((node.findtext("description") or "").split())
        if title and url:
            results.append({"url": url, "title": title, "snippet": snippet})
    return results


class _DuckDuckGoResultParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.results = []
        self._current = None
        self._capture = None

    def handle_starttag(self, tag, attrs):
        attrs_dict = dict(attrs)
        classes = attrs_dict.get("class", "").split()
        if tag == "a" and "result__a" in classes:
            url = _unwrap_search_url(attrs_dict.get("href", ""))
            self._current = {"url": url, "title": [], "snippet": []}
            self._capture = "title"
        elif self._current and "result__snippet" in classes:
            self._capture = "snippet"

    def handle_data(self, data):
        if self._current and self._capture:
            self._current[self._capture].append(data)

    def handle_endtag(self, tag):
        if not self._current:
            return
        if tag == "a" and self._capture == "title":
            title = " ".join("".join(self._current["title"]).split())
            if title and self._current["url"]:
                self._current["title"] = title
                self._capture = None
            else:
                self._current = None
        elif self._capture == "snippet" and tag in {"a", "div"}:
            self._current["snippet"] = " ".join("".join(self._current["snippet"]).split())
            self.results.append(self._current)
            self._current = None
            self._capture = None


LOW_QUALITY_SEARCH_HOSTS = (
    "bilibili.com", "youtube.com", "youtu.be", "douyin.com", "v.qq.com", "iqiyi.com", "youku.com",
    "baike.baidu.com", "tieba.baidu.com", "zhidao.baidu.com", "weibo.com", "douban.com",
)


def _search_result_score(item, search_query):
    parsed = urlparse(item.get("url", ""))
    host = (parsed.hostname or "").lower()
    if not host or host.endswith(("bing.com", "duckduckgo.com")):
        return -100
    text = f"{item.get('title', '')} {item.get('snippet', '')}".lower()
    score = 0
    if re.search(r"歌词|lyrics?|歌詞|lrc", text, re.I):
        score += 6
    if re.search(r"歌手|演唱|歌曲", text):
        score += 2
    terms = [term.lower() for term in re.findall(r"[A-Za-z0-9\u3400-\u9fff]{2,}", search_query) if term not in {"歌词", "lyrics"}]
    score += sum(2 for term in terms if term in text)
    if any(host == bad or host.endswith(f".{bad}") for bad in LOW_QUALITY_SEARCH_HOSTS):
        score -= 8
    if re.search(r"论坛|评论|问答|百科|视频|mv|在线观看", text, re.I):
        score -= 4
    return score


def _search_query_variants(search_queries):
    if isinstance(search_queries, str):
        search_queries = [search_queries]
    return list(dict.fromkeys(
        re.sub(r"\s+", " ", query).strip() for query in search_queries if str(query).strip()
    ))[:MAX_SEARCH_QUERIES]


def _rank_search_results(results, search_query):
    candidates = []
    seen = set()
    ranked = sorted(enumerate(results), key=lambda pair: (-_search_result_score(pair[1], search_query), pair[0]))
    for _, item in ranked:
        url = item.get("url")
        if not url or url in seen or _search_result_score(item, search_query) <= 0:
            continue
        seen.add(url)
        candidates.append(item)
        if len(candidates) >= MAX_CANDIDATES:
            break
    return candidates


def _candidate_metadata(metadata, track_name, artist_name, duration, url, title, provider, lrc_text="", plain_text="", **extra):
    track_score = lyric_similarity(metadata.get("song_title", ""), track_name or "")
    artist_score = lyric_similarity(metadata.get("artist", ""), artist_name or "") if metadata.get("artist") and artist_name else 0.0
    expected = metadata.get("duration")
    duration_score = 0.0 if not expected or not duration else max(0.0, 1.0 - abs(float(expected) - float(duration)) / max(10.0, float(expected)))
    return {
        "provider": provider, "title": title or track_name or "", "track_name": track_name or "",
        "artist_name": artist_name or "", "duration": duration, "source_url": url, "url": url,
        "lrc_text": lrc_text or "", "plain_text": plain_text or "",
        "metadata_score": round(track_score * 0.62 + artist_score * 0.23 + duration_score * 0.15, 4),
        **extra,
    }


def fetch_bilibili_subtitle_candidates(video_info, deadline=None):
    video_info = video_info or {}
    bvid, cid = video_info.get("bvid"), video_info.get("cid")
    if not bvid or not cid:
        return []
    title = str(video_info.get("title") or "")
    uploader = str(video_info.get("uploader") or video_info.get("channel") or "")
    metadata = extract_song_metadata(title, uploader, video_info)
    try:
        raw, url = _fetch_public_text(
            f"https://api.bilibili.com/x/player/v2?bvid={quote_plus(str(bvid))}&cid={quote_plus(str(cid))}",
            PAGE_TIMEOUT, MAX_PAGE_BYTES, {"application/json", "text/json", "text/plain"}, deadline,
        )
        payload = json.loads(raw)
    except (HTTPError, URLError, OSError, ValueError, UnicodeError):
        return []
    subtitles = (((payload.get("data") or {}).get("subtitle") or {}).get("subtitles") or [])
    candidates = []
    for item in subtitles:
        label = str(item.get("lan_doc") or item.get("lan") or "")
        sub_url = item.get("subtitle_url") or ""
        if sub_url.startswith("//"):
            sub_url = "https:" + sub_url
        elif sub_url.startswith("/"):
            sub_url = urljoin("https://www.bilibili.com", sub_url)
        if not sub_url:
            continue
        try:
            body, final_url = _fetch_public_text(sub_url, PAGE_TIMEOUT, MAX_PAGE_BYTES, {"application/json", "text/json", "text/plain"}, deadline)
            entries = (json.loads(body).get("body") or [])
        except (HTTPError, URLError, OSError, ValueError, UnicodeError):
            continue
        lrc_lines = []
        plain_lines = []
        for entry in entries:
            text = str(entry.get("content") or entry.get("text") or "").strip()
            if not text:
                continue
            start = float(entry.get("from", entry.get("start", 0)) or 0)
            lrc_lines.append(f"[{int(start // 60):02d}:{start % 60:05.2f}]{text}")
            plain_lines.append(text)
        if not plain_lines:
            continue
        is_auto = bool(re.search(r"ai|自动|自動|机器|機器", f"{item.get('lan', '')} {label}", re.I))
        is_chinese = bool(re.search(r"zh|中文|简体|繁体|漢語|汉语", f"{item.get('lan', '')} {label}", re.I))
        quality = (2 if is_chinese else 0) + (1 if not is_auto else 0)
        candidate_title = " ".join(part for part in (
            metadata.get("artist"), metadata.get("song_title"), f"B站字幕：{label}",
        ) if part)
        candidates.append(_candidate_metadata(
            metadata, str(metadata.get("song_title") or title), str(metadata.get("artist") or uploader), video_info.get("duration"),
            final_url, candidate_title, "bilibili_subtitle", "\n".join(lrc_lines), "\n".join(plain_lines),
            language=str(item.get("lan") or ""), is_automatic=is_auto, is_chinese=is_chinese, subtitle_label=label, subtitle_quality=quality,
        ))
    return sorted(candidates, key=lambda item: (item.get("subtitle_quality", 0), item.get("metadata_score", 0)), reverse=True)


def fetch_netease_candidates(metadata, deadline=None):
    if not _cfg("search.netease_enabled", bool) or not metadata.get("song_title"):
        return []
    query = " ".join(part for part in (metadata.get("artist"), metadata.get("song_title")) if part)
    try:
        search_url = "https://music.163.com/api/search/get/web?" + urlencode({"s": query, "type": 1, "offset": 0, "total": "true", "limit": 10})
        raw, _ = _fetch_public_text(search_url, SEARCH_TIMEOUT, MAX_SEARCH_BYTES, {"application/json", "text/json", "text/plain"}, deadline)
        songs = ((json.loads(raw).get("result") or {}).get("songs") or [])
    except (HTTPError, URLError, OSError, ValueError, UnicodeError):
        return []
    candidates = []
    for song in songs[:_cfg("search.netease_max_results", int)]:
        artists = "/".join(str(a.get("name") or "") for a in (song.get("artists") or []) if a.get("name"))
        duration = float(song.get("duration") or 0) / 1000
        track = str(song.get("name") or "")
        if lyric_similarity(metadata.get("song_title"), track) < 0.55:
            continue
        lyric_url = f"https://music.163.com/api/song/lyric?id={quote_plus(str(song.get('id')))}&lv=1&kv=1&tv=-1"
        try:
            lyric_raw, final_url = _fetch_public_text(lyric_url, PAGE_TIMEOUT, MAX_PAGE_BYTES, {"application/json", "text/json", "text/plain"}, deadline)
            lyric_payload = json.loads(lyric_raw)
        except (HTTPError, URLError, OSError, ValueError, UnicodeError):
            continue
        lrc = str(((lyric_payload.get("lrc") or {}).get("lyric")) or "")
        plain = str(((lyric_payload.get("tlyric") or {}).get("lyric")) or "") or lrc
        if lrc or plain:
            candidates.append(_candidate_metadata(metadata, track, artists, duration, final_url, f"{track} - {artists}".strip(" -"), "netease", lrc, plain))
    return candidates


def fetch_lrclib_candidates(metadata, deadline=None):
    if not _cfg("search.lrclib_enabled", bool) or not metadata.get("song_title"):
        return []
    param_sets = []
    if metadata.get("artist"):
        param_sets.append({"track_name": metadata["song_title"], "artist_name": metadata["artist"]})
    param_sets.append({"track_name": metadata["song_title"]})
    payload = []
    seen_ids = set()
    for params in param_sets:
        query = "&".join(f"{key}={quote_plus(value)}" for key, value in params.items())
        url = f"https://lrclib.net/api/search?{query}"
        try:
            page, _ = _fetch_public_text(
                url, max(15, SEARCH_TIMEOUT), MAX_SEARCH_BYTES,
                {"application/json", "text/json", "text/plain"}, deadline,
            )
            results = json.loads(page)
        except (HTTPError, URLError, OSError, ValueError, UnicodeError):
            continue
        if not isinstance(results, list):
            continue
        for item in results:
            item_id = item.get("id") if isinstance(item, dict) else None
            if item_id not in seen_ids:
                seen_ids.add(item_id)
                payload.append(item)
    candidates = []
    max_results = _cfg("search.lrclib_max_results", int)

    def safe_duration(value):
        try:
            duration = float(value)
            return duration if math.isfinite(duration) else None
        except (TypeError, ValueError):
            return None

    expected_duration = safe_duration(metadata.get("duration"))
    expected_track = str(metadata.get("song_title") or "")
    expected_artist = str(metadata.get("artist") or "")

    def duration_distance(item):
        duration = safe_duration(item.get("duration"))
        return abs(duration - expected_duration) if expected_duration is not None and duration is not None else float("inf")

    def metadata_rank(item):
        track_similarity = lyric_similarity(expected_track, item.get("trackName", ""))
        artist_value = str(item.get("artistName") or "")
        artist_similarity = lyric_similarity(expected_artist, artist_value) if expected_artist and artist_value else 0.0
        return track_similarity, artist_similarity, duration_distance(item)

    ranked_payload = []
    for item in payload:
        if not isinstance(item, dict):
            continue
        track_similarity, artist_similarity, _ = metadata_rank(item)
        if track_similarity < 0.55 or (expected_artist and artist_similarity < 0.45 and track_similarity < 0.82):
            continue
        ranked_payload.append((item, track_similarity, artist_similarity))
    ranked_payload.sort(key=lambda row: (-row[1], -row[2], duration_distance(row[0])))
    for item, track_similarity, artist_similarity in ranked_payload[:max_results]:
        synced = str(item.get("syncedLyrics") or "").strip()
        plain = str(item.get("plainLyrics") or "").strip()
        if not synced and not plain:
            continue
        url = f"https://lrclib.net/api/get/{item.get('id')}"
        candidate = _candidate_metadata(
            metadata, str(item.get("trackName") or ""), str(item.get("artistName") or ""), item.get("duration"), url,
            f"{item.get('trackName') or ''} - {item.get('artistName') or ''}".strip(" -"), "lrclib", synced, plain,
        )
        candidate["snippet"] = plain[:500]
        candidates.append(candidate)
    return candidates


def search_lyrics_candidates(search_queries, deadline=None):
    variants = _search_query_variants(search_queries)
    all_candidates = []
    seen = set()

    def add_ranked(results, variant):
        for item in _rank_search_results(results, variant)[:MAX_RESULTS_PER_QUERY]:
            url = item.get("url")
            if url and url not in seen:
                seen.add(url)
                all_candidates.append(item)
                if len(all_candidates) >= MAX_CANDIDATES:
                    return True
        return False

    for variant in variants:
        results = []
        rss_url = f"https://www.bing.com/search?format=rss&q={quote_plus(variant)}"
        try:
            page, _ = _fetch_public_text(
                rss_url, SEARCH_TIMEOUT, MAX_SEARCH_BYTES,
                {"application/rss+xml", "application/xml", "text/xml", "text/plain"}, deadline,
            )
            results = _bing_rss_results(page)
        except (ET.ParseError, HTTPError, URLError, OSError, ValueError, UnicodeError):
            pass
        if not results:
            html_url = f"https://www.bing.com/search?q={quote_plus(variant)}&count={MAX_RESULTS_PER_QUERY + 3}&setlang=zh-Hans"
            try:
                page, _ = _fetch_public_text(
                    html_url, SEARCH_TIMEOUT, MAX_SEARCH_BYTES, {"text/html", "application/xhtml+xml"}, deadline,
                )
                parser = _SearchResultParser()
                parser.feed(page)
                results = parser.results
            except (HTTPError, URLError, OSError, ValueError, UnicodeError):
                pass
        if not results:
            fallback_url = f"https://html.duckduckgo.com/html/?q={quote_plus(variant)}"
            try:
                page, _ = _fetch_public_text(
                    fallback_url, SEARCH_TIMEOUT, MAX_SEARCH_BYTES,
                    {"text/html", "application/xhtml+xml"}, deadline,
                )
                parser = _DuckDuckGoResultParser()
                parser.feed(page)
                results = parser.results
            except (HTTPError, URLError, OSError, ValueError, UnicodeError):
                pass
        if add_ranked(results, variant):
            break
    return all_candidates


def _json_ld_text(raw_chunks):
    text_parts = []
    raw = "".join(raw_chunks).strip()
    if not raw:
        return ""
    try:
        payload = json.loads(raw)
    except (ValueError, TypeError):
        return ""

    def visit(value, key=""):
        if isinstance(value, dict):
            for child_key, child in value.items():
                visit(child, str(child_key).lower())
        elif isinstance(value, list):
            for child in value:
                visit(child, key)
        elif isinstance(value, str) and key in {"lyrics", "lyric", "text", "description", "articlebody"}:
            text_parts.append(value)

    visit(payload)
    return "\n".join(text_parts)


def _line_script(line):
    if re.search(r"[\u3040-\u30ff]", line):
        return "ja"
    han_count = len(re.findall(r"[\u3400-\u9fff]", line))
    latin_count = len(re.findall(r"[A-Za-z]", line))
    if latin_count > han_count:
        return "latin"
    return "han" if han_count else "other"


def _select_lyric_script(lines, reference_text=""):
    if not _cfg("script_tracks.enabled", bool):
        return lines
    priority = _cfg("script_tracks.priority", list)
    if set(priority) != {"ja", "han", "latin", "other"}:
        raise ValueError("歌词配置项 script_tracks.priority 必须恰好包含 ja/han/latin/other")
    groups = {script: [] for script in priority}
    for line in lines:
        groups[_line_script(line)].append(line)
    substantial = {script: values for script, values in groups.items()
                   if len(values) >= _cfg("script_tracks.minimum_lines", int)}
    if len(substantial) < _cfg("script_tracks.minimum_substantial_tracks", int):
        return lines
    reference_script = _line_script(reference_text)
    if reference_script in substantial:
        return substantial[reference_script]
    ranked = []
    references = [part for part in re.split(r"[\r\n]+", reference_text) if normalize_lyric_text(part)]
    for script, values in substantial.items():
        similarity = sum(max((lyric_similarity(line, ref) for ref in references), default=0.0) for line in values)
        ranked.append((similarity, len(values), -priority.index(script), script))
    return substantial[max(ranked)[3]] if ranked else lines


def extract_lyrics_from_page(page, reference_text=""):
    parser = _LyricsPageParser()
    parser.feed(page)
    sources = [
        "".join(parser.preferred_parts),
        _json_ld_text(parser.json_ld),
        "".join(parser.body_parts),
    ]
    best = []
    for index, source in enumerate(sources):
        lines = _select_lyric_script(clean_lyrics_text(source), reference_text)
        if len(lines) > len(best):
            best = lines
        if len(lines) >= _cfg("script_tracks.preferred_source_minimum_lines", int) and index == 0:
            break
    return best


def _snippet_lyrics_lines(snippet):
    value = re.sub(r"^.*?(?:歌词详情|歌词)\s*[:：]", "", str(snippet or ""), flags=re.I)
    value = re.sub(r"[。.!！？]+", "\n", value)
    return clean_lyrics_text(value)


def _empty_correction(search_query, reason, search_queries=None):
    return {
        "applied": False,
        "mode": "none",
        "matched_count": 0,
        "timed_line_count": 0,
        "total_count": 0,
        "source_url": None,
        "source_title": None,
        "search_query": search_query,
        "search_queries": list(search_queries or ([search_query] if search_query else [])),
        "reason": reason,
        "provider": None,
        "confidence": 0.0,
        "match_type": "fallback",
        "metadata": {"song_title": "", "detected_artist": "", "source_artist": "", "language": "auto", "version": ""},
        "timeline": {"model": None, "offset": None, "scale": 1.0, "rmse": None, "anchor_count": 0},
        "attempted_providers": [],
    }


def _source_matches_subject(source_text, metadata, timeline_rank):
    source_normalized = normalize_lyric_text(source_text)
    song = normalize_lyric_text(metadata.get("song_title", ""))
    artist = normalize_lyric_text(metadata.get("artist", ""))
    if not song:
        return False
    strong_audio_match = (
        timeline_rank[3] >= 2
        and timeline_rank[3] * timeline_rank[4] >= 2
        and timeline_rank[1] >= _cfg_number("candidate_filter.strong_audio_coverage")
        and timeline_rank[2] >= _cfg_number("candidate_filter.strong_audio_average_confidence")
        and timeline_rank[4] >= _cfg_number("candidate_filter.strong_audio_high_confidence_ratio")
    )
    source_parts = [source_text] + re.split(r"[\s\-—–_|｜:：/]+", str(source_text or ""))
    song_match = song in source_normalized or max(
        (lyric_similarity(song, part) for part in source_parts), default=0.0
    ) >= _cfg_number("candidate_filter.source_title_similarity_threshold")
    if not song_match:
        return False
    return not artist or artist in source_normalized or strong_audio_match


def _filter_candidate_title_lines(lyric_lines, source_title):
    normalized_title = normalize_lyric_text(source_title)
    return [
        line for line in lyric_lines
        if not normalized_title or lyric_similarity(line, source_title) < _cfg_number("candidate_filter.source_title_similarity_threshold")
    ]


def _timeline_candidate(segments, words, lyric_lines, clip_start, clip_end):
    timed, details = _build_timed_lyrics_details(words, lyric_lines, clip_start, clip_end)
    timed_count = len(timed)
    word_count = details["word_count"]
    coverage = details["matched_word_count"] / max(1, word_count)
    average_confidence = sum(item["confidence"] for item in timed) / max(1, timed_count)
    anchored_count = details["anchored_line_count"]
    high_confidence_ratio = sum(
        item["confidence"] >= _cfg_number("timeline_quality.high_confidence_threshold") for item in timed
    ) / max(1, anchored_count)
    if timed_count == 1:
        acceptable = (
            anchored_count == 1
            and coverage >= _cfg_number("timeline_quality.single_line_coverage")
            and average_confidence >= _cfg_number("timeline_quality.single_line_average_confidence")
        )
    else:
        acceptable = (
            timed_count >= _cfg("timeline_quality.min_lines", int)
            and anchored_count >= _cfg("timeline_quality.min_anchored_lines", int)
            and high_confidence_ratio >= _cfg_number("timeline_quality.high_confidence_ratio")
            and average_confidence >= _cfg_number("timeline_quality.average_confidence")
            and coverage >= _cfg_number("timeline_quality.coverage")
        )
    return timed, (timed_count, coverage, average_confidence, anchored_count, high_confidence_ratio), acceptable


def correct_segments_from_web(
        segments, title, uploader="", words=None, clip_start=None, clip_end=None, video_info=None, deadline=None,
        cancel_check=None):
    cancel_check = cancel_check or (lambda: None)
    cancel_check()
    metadata = extract_song_metadata(title, uploader, video_info)
    search_queries = metadata["search_queries"]
    search_query = search_queries[0] if search_queries else ""
    correction = _empty_correction(search_query, "未找到质量足够的歌词候选", search_queries)
    correction["total_count"] = len(segments)
    correction["metadata"].update({
        "song_title": metadata.get("song_title", ""), "detected_artist": metadata.get("artist", ""),
        "language": metadata.get("language", "auto"), "version": metadata.get("version", ""),
    })
    if not segments:
        correction["reason"] = "没有可校正的识别分段"
        return segments, correction
    if not metadata["song_title"]:
        correction["reason"] = "视频标题不足以生成歌词搜索词"
        return segments, correction

    clip_start = float(clip_start if clip_start is not None else segments[0]["start"])
    clip_end = float(clip_end if clip_end is not None else segments[-1]["end"])
    words = list(words or interpolate_words_from_segments(segments))
    reference_text = "\n".join(str(segment.get("text", "")) for segment in segments)
    best_lrc = None
    best_timeline = None
    best_text = None
    minimum_matches = (
        _cfg("candidate_filter.text_only_minimum_single", int)
        if len(segments) == 1
        else max(
            _cfg("candidate_filter.text_only_minimum_multiple", int),
            math.ceil(len(segments) * _cfg_number("candidate_filter.text_only_minimum_ratio")),
        )
    )

    def provider_quality(provider):
        return _cfg_number(f"provider_quality.{provider if provider in _cfg('provider_quality', dict) else 'web'}")

    def consider(lyric_lines, source_url, source_title, source_context="", candidate=None):
        nonlocal best_timeline, best_text
        candidate = candidate or {}
        quality = provider_quality(candidate.get("provider", "web"))
        metadata_score = float(candidate.get("metadata_score") or 0.0)
        lyric_lines = _filter_candidate_title_lines(lyric_lines, source_title)
        if len(lyric_lines) < _cfg("candidate_filter.minimum_candidate_lines", int):
            return
        timed, timeline_rank, acceptable = _timeline_candidate(segments, words, lyric_lines, clip_start, clip_end)
        source_consistent = _source_matches_subject(
            f"{source_title} {source_context}", metadata, timeline_rank,
        )
        acceptable = acceptable and source_consistent
        weighted_timeline_rank = (timeline_rank[0], timeline_rank[1], timeline_rank[2], timeline_rank[3], timeline_rank[4], quality, metadata_score)
        if acceptable and (best_timeline is None or weighted_timeline_rank > best_timeline[0]):
            best_timeline = (weighted_timeline_rank, timed, source_url, source_title, candidate)
        aligned = align_lyrics_to_segments(segments, lyric_lines)
        matched = sum(1 for item in aligned if item["corrected"])
        confidence_sum = sum(item["confidence"] for item in aligned if item["corrected"])
        average_text_confidence = confidence_sum / max(1, matched)
        text_rank = (matched, confidence_sum, quality, metadata_score)
        text_consistent = source_consistent or (
            matched >= _cfg("candidate_filter.text_consistency_min_matches", int)
            and average_text_confidence >= _cfg_number("candidate_filter.text_consistency_average_confidence")
        )
        if text_consistent and (best_text is None or text_rank > best_text[0]):
            best_text = (text_rank, aligned, source_url, source_title, candidate)

    def evaluate_candidates(candidates):
        nonlocal best_lrc
        combined_snippet_lines = []
        for candidate in candidates:
            provider = candidate.get("provider", "web")
            if provider not in correction["attempted_providers"]:
                correction["attempted_providers"].append(provider)
            cancel_check()
            if deadline is not None and _remaining(deadline) <= 0:
                raise TimeoutError("歌词联网校正超出请求预算")
            snippet_lines = _snippet_lyrics_lines(candidate.get("snippet", ""))
            combined_snippet_lines.extend(snippet_lines)
            try:
                if candidate.get("lrc_text") or candidate.get("plain_text"):
                    page = candidate.get("lrc_text") or candidate.get("plain_text") or ""
                    final_url = candidate["url"]
                else:
                    page, final_url = _fetch_public_text(
                        candidate["url"], PAGE_TIMEOUT, MAX_PAGE_BYTES,
                        {"text/html", "application/xhtml+xml", "text/plain"}, deadline,
                    )
                lrc_lines = parse_lrc_timed_lines(page)
                lrc_timed, lrc_details = sync_lrc_to_audio(lrc_lines, segments, clip_start, clip_end)
                source_consistent = _source_matches_subject(
                    f"{candidate['title']} {candidate.get('snippet', '')}",
                    metadata,
                    (len(lrc_timed), 1.0, lrc_details.get("average_confidence", 0.0),
                     lrc_details.get("anchor_count", 0), lrc_details.get("high_confidence_ratio", 0.0)),
                )
                lrc_details["accepted"] = bool(lrc_details.get("accepted") and source_consistent and lrc_timed)
                if lrc_details["accepted"]:
                    lrc_rank = (
                        lrc_details.get("anchor_count", 0), lrc_details.get("average_confidence", 0.0),
                        -lrc_details.get("offset_spread", 999.0), len(lrc_timed),
                        provider_quality(candidate.get("provider", "web")), float(candidate.get("metadata_score") or 0.0),
                    )
                    if best_lrc is None or lrc_rank > best_lrc[0]:
                        best_lrc = (lrc_rank, lrc_timed, final_url, candidate["title"], candidate, lrc_details)
                page_lines = extract_lyrics_from_page(page, reference_text)
                consider(
                    page_lines if len(page_lines) >= _cfg("candidate_filter.page_lines_preferred_minimum", int) else snippet_lines,
                    final_url,
                    candidate["title"],
                    candidate.get("snippet", ""), candidate,
                )
            except TimeoutError:
                raise
            except (HTTPError, URLError, OSError, ValueError, UnicodeError) as exc:
                logger.warning("歌词候选处理失败 url=%s error=%s", candidate.get("url"), exc)
                consider(snippet_lines, candidate["url"], candidate["title"], candidate.get("snippet", ""), candidate)

        if len(combined_snippet_lines) >= _cfg("candidate_filter.combined_snippet_minimum", int) and candidates:
            consider(
                combined_snippet_lines,
                candidates[0]["url"],
                f"搜索摘要：{candidates[0]['title']}",
                candidates[0].get("snippet", ""),
            )

    cancel_check()
    structured_candidates = []
    structured_candidates.extend(fetch_bilibili_subtitle_candidates(video_info or {}, deadline))
    structured_candidates.extend(fetch_netease_candidates(metadata, deadline))
    structured_candidates.extend(fetch_lrclib_candidates(metadata, deadline))
    evaluate_candidates(structured_candidates)
    cancel_check()
    has_reliable_result = (
        best_lrc is not None
        or best_timeline is not None
        or _cfg("fallback.enable_text_only", bool)
        and best_text is not None
        and best_text[0][0] >= minimum_matches
    )
    if not has_reliable_result:
        cancel_check()
        evaluate_candidates(search_lyrics_candidates(search_queries, deadline))
    cancel_check()

    def selection_fields(candidate, timeline=None, confidence=0.0):
        candidate = candidate or {}
        source_artist = str(candidate.get("artist_name") or "")
        detected_artist = str(metadata.get("artist") or "")
        same_artist = not detected_artist or not source_artist or lyric_similarity(detected_artist, source_artist) >= _cfg_number("candidate_filter.artist_match_threshold")
        if metadata.get("is_live"):
            match_type = "live"
        elif metadata.get("is_cover") or not same_artist:
            match_type = "cover"
        elif metadata.get("version"):
            match_type = "version"
        else:
            match_type = "exact"
        return {
            "provider": candidate.get("provider", "web"), "confidence": round(float(confidence), 4), "match_type": match_type,
            "metadata": {**correction["metadata"], "source_artist": source_artist},
            "timeline": {
                "model": (timeline or {}).get("timeline_model"), "offset": (timeline or {}).get("offset"),
                "scale": (timeline or {}).get("scale", 1.0), "rmse": (timeline or {}).get("rmse"),
                "anchor_count": (timeline or {}).get("anchor_count", 0),
            },
        }

    if best_lrc is not None:
        timed_count = len(best_lrc[1])
        correction.update({
            "applied": True,
            "mode": "lrc_timeline_synced",
            "matched_count": timed_count,
            "timed_line_count": timed_count,
            "total_count": timed_count,
            "source_url": best_lrc[2],
            "source_title": best_lrc[3],
            "reason": f"已同步歌词时间轴，共 {timed_count} 行",
            **selection_fields(best_lrc[4], best_lrc[5], best_lrc[5].get("average_confidence", 0.0)),
        })
        return best_lrc[1], correction

    if best_timeline is not None:
        timed_count = len(best_timeline[1])
        correction.update({
            "applied": True,
            "mode": "lyrics_timeline_rebuilt",
            "matched_count": timed_count,
            "timed_line_count": timed_count,
            "total_count": timed_count,
            "source_url": best_timeline[2],
            "source_title": best_timeline[3],
            "reason": f"已根据歌词重建 {timed_count} 行时间轴",
            **selection_fields(best_timeline[4], confidence=best_timeline[0][2]),
        })
        return best_timeline[1], correction

    if (_cfg("fallback.enable_text_only", bool)
            and best_text is not None and best_text[0][0] >= minimum_matches):
        matched_count = best_text[0][0]
        correction.update({
            "applied": True,
            "mode": "text_only",
            "matched_count": matched_count,
            "source_url": best_text[2],
            "source_title": best_text[3],
            "reason": f"已按歌词校正 {matched_count}/{len(segments)} 句",
            **selection_fields(best_text[4], confidence=best_text[0][1] / max(1, matched_count)),
        })
        return best_text[1], correction

    correction["reason"] = "歌词匹配置信度不足，已保留模型识别结果"
    return segments, correction


def _run_transcription(request, cancel_event=None, stage_callback=None):
    from job_manager import JobCancelled

    acquired = False
    try:
        acquired = _processing_slots.acquire(blocking=False)
        if not acquired:
            if cancel_event is None:
                raise HTTPException(status_code=429, detail="识别任务繁忙，请稍后重试")
            while not acquired:
                if cancel_event.is_set():
                    raise JobCancelled("识别任务已取消")
                acquired = _processing_slots.acquire(timeout=0.1)
        if cancel_event is not None and cancel_event.is_set():
            raise JobCancelled("识别任务已取消")
        return transcribe_request(
            request,
            settings=SETTINGS,
            vad_filter=VAD_FILTER,
            cache=_result_cache,
            lyrics_config=LYRICS_CONFIG,
            get_model=get_model,
            run_command=run_command,
            run_model_transcription=run_model_transcription,
            bounded_timeout=_bounded_timeout,
            remaining=_remaining,
            extract_song_metadata=extract_song_metadata,
            empty_correction=_empty_correction,
            correct_segments_from_web=correct_segments_from_web,
            interpolate_words_from_segments=interpolate_words_from_segments,
            lyrics_web_correction=LYRICS_WEB_CORRECTION,
            cancel_event=cancel_event,
            stage_callback=stage_callback,
        )
    finally:
        if acquired:
            _processing_slots.release()


_job_manager = JobManager(
    _run_transcription,
    workers=SETTINGS.max_concurrent_tasks,
    queue_size=SETTINGS.job_queue_size,
    retention_seconds=SETTINGS.job_retention_seconds,
)


@app.get("/auth/session")
def auth_session(request: Request, response: Response):
    origin = request.headers.get("origin", "")
    extension_id = request.headers.get("x-sing-reactor-extension", "").strip()
    valid_extension_id = bool(re.fullmatch(r"[a-p]{32}", extension_id))
    if origin:
        allowed = _is_extension_origin(origin)
        if allowed and extension_id:
            allowed = urlparse(origin).netloc == extension_id
    else:
        allowed = valid_extension_id
    if not allowed:
        raise HTTPException(status_code=403, detail="仅允许浏览器扩展获取会话 token")
    response.headers["Cache-Control"] = "no-store"
    return {"token": _API_TOKEN}


@app.get("/health")
def health():
    return {
        "ok": True,
        "model": MODEL_NAME,
        "device": DEVICE,
        "compute_type": COMPUTE_TYPE,
        "lyrics_web_correction": LYRICS_WEB_CORRECTION,
        "lyrics_config_path": str(LYRICS_CONFIG_PATH),
        "lyrics_config_schema_version": _cfg("schema_version", int),
        "max_duration": MAX_DURATION,
        "jobs": _job_manager.capabilities(),
        "cache": _result_cache.capabilities(),
    }


@app.post("/transcribe")
def transcribe(request: TranscribeRequest, x_sing_reactor_token: str = Header(default="")):
    _require_token(x_sing_reactor_token)
    validate_request(request)
    try:
        return _run_transcription(request)
    except HTTPException:
        raise
    except TimeoutError as exc:
        raise HTTPException(status_code=504, detail="请求处理超时") from exc
    except Exception as exc:
        logger.exception("识别请求失败")
        raise HTTPException(status_code=500, detail=f"识别失败：{exc}") from exc


@app.post("/transcribe/jobs", status_code=202)
def create_transcribe_job(request: TranscribeRequest, x_sing_reactor_token: str = Header(default="")):
    _require_token(x_sing_reactor_token)
    validate_request(request)
    try:
        return _job_manager.create(request)
    except JobQueueFull as exc:
        raise HTTPException(status_code=429, detail=str(exc)) from exc


@app.get("/transcribe/jobs/{job_id}")
def get_transcribe_job(job_id: str, x_sing_reactor_token: str = Header(default="")):
    _require_token(x_sing_reactor_token)
    job = _job_manager.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="识别任务不存在或已过期")
    return job


@app.delete("/transcribe/jobs/{job_id}")
def cancel_transcribe_job(job_id: str, x_sing_reactor_token: str = Header(default="")):
    _require_token(x_sing_reactor_token)
    job = _job_manager.cancel(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="识别任务不存在或已过期")
    return job


if __name__ == "__main__":
    uvicorn.run(app, host=HOST, port=PORT)
