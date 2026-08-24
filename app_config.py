import json
import os
from dataclasses import dataclass
from pathlib import Path


_BASE_DIR = Path(__file__).resolve().parent
_CONFIG_DEFAULT_PATH = _BASE_DIR / "lyrics_config.json"


def _env_bool(name, default):
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"true", "1", "yes", "on"}


def _env_int(name, default, minimum=1):
    value = int(os.getenv(name, str(default)))
    if value < minimum:
        raise ValueError(f"{name} 必须不小于 {minimum}")
    return value


def _csv_env(name):
    return tuple(dict.fromkeys(item.strip() for item in os.getenv(name, "").split(",") if item.strip()))


def _load_lyrics_config(path):
    try:
        with path.open("r", encoding="utf-8") as stream:
            config = json.load(stream)
    except FileNotFoundError as exc:
        raise RuntimeError(f"歌词配置文件不存在: {path}") from exc
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"歌词配置 JSON 非法: {path}:{exc.lineno}:{exc.colno}: {exc.msg}") from exc
    except OSError as exc:
        raise RuntimeError(f"无法读取歌词配置文件 {path}: {exc}") from exc
    if not isinstance(config, dict):
        raise TypeError("歌词配置项 <root> 必须是 object")
    return config


@dataclass(frozen=True)
class Settings:
    host: str
    port: int
    model_name: str
    device: str
    compute_type: str
    vad_filter: bool
    lyrics_web_correction: bool
    api_token: str
    extension_origins: tuple
    max_concurrent_tasks: int
    job_queue_size: int
    job_retention_seconds: int
    cache_max_entries: int
    cache_ttl_seconds: int
    cache_dir: Path
    request_timeout: int
    ffmpeg_timeout: int
    ytdlp_socket_timeout: int
    ytdlp_retries: int
    max_download_bytes: int
    allowed_lyrics_hosts: tuple


LYRICS_CONFIG_PATH = Path(os.getenv("LYRICS_CONFIG_PATH", str(_CONFIG_DEFAULT_PATH))).expanduser().resolve()
LYRICS_CONFIG = _load_lyrics_config(LYRICS_CONFIG_PATH)
SETTINGS = Settings(
    host=os.getenv("SING_REACTOR_HOST", "127.0.0.1"),
    port=_env_int("SING_REACTOR_PORT", 8765),
    model_name=os.getenv("WHISPER_MODEL", "small"),
    device=os.getenv("WHISPER_DEVICE", "auto"),
    compute_type=os.getenv("WHISPER_COMPUTE_TYPE", "default"),
    vad_filter=_env_bool("WHISPER_VAD_FILTER", False),
    lyrics_web_correction=_env_bool("LYRICS_WEB_CORRECTION", True),
    api_token=os.getenv("SING_REACTOR_TOKEN", ""),
    extension_origins=_csv_env("SING_REACTOR_EXTENSION_ORIGINS"),
    max_concurrent_tasks=_env_int("SING_REACTOR_MAX_TASKS", 2),
    job_queue_size=_env_int("SING_REACTOR_JOB_QUEUE_SIZE", 8),
    job_retention_seconds=_env_int("SING_REACTOR_JOB_RETENTION", 3600),
    cache_max_entries=_env_int("SING_REACTOR_CACHE_ENTRIES", 64),
    cache_ttl_seconds=_env_int("SING_REACTOR_CACHE_TTL", 86400),
    cache_dir=Path(os.getenv("SING_REACTOR_CACHE_DIR", str(_BASE_DIR / ".transcribe-cache"))).expanduser().resolve(),
    request_timeout=_env_int("SING_REACTOR_REQUEST_TIMEOUT", 300),
    ffmpeg_timeout=_env_int("SING_REACTOR_FFMPEG_TIMEOUT", 120),
    ytdlp_socket_timeout=_env_int("SING_REACTOR_YTDLP_SOCKET_TIMEOUT", 15),
    ytdlp_retries=_env_int("SING_REACTOR_YTDLP_RETRIES", 2, 0),
    max_download_bytes=_env_int("SING_REACTOR_MAX_DOWNLOAD_BYTES", 250 * 1024 * 1024),
    allowed_lyrics_hosts=_csv_env("SING_REACTOR_LYRICS_HOSTS"),
)
