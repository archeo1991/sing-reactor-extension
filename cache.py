import hashlib
import json
import os
import threading
import time
from collections import OrderedDict
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse


CACHE_VERSION = 5


class ResultCache:
    def __init__(self, directory, max_entries=64, ttl_seconds=86400):
        self.directory = Path(directory)
        self.max_entries = max_entries
        self.ttl_seconds = ttl_seconds
        self._lock = threading.Lock()
        self._memory = OrderedDict()
        self.directory.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def normalize_page_url(value):
        parsed = urlparse(str(value))
        query = [(key, item) for key, item in parse_qsl(parsed.query, keep_blank_values=True) if key.lower() in {"p"}]
        path = parsed.path.rstrip("/") or "/"
        return urlunparse((parsed.scheme.lower(), (parsed.hostname or "").lower(), path, "", urlencode(query), ""))

    @classmethod
    def make_key(cls, request, model_params, lyrics_config):
        payload = {
            "url": cls.normalize_page_url(request.url),
            "start": round(float(request.start), 3),
            "end": round(float(request.end), 3),
            "language": request.language or "auto",
            "model": model_params,
            "lyrics_config_hash": hashlib.sha256(
                json.dumps(lyrics_config, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
            ).hexdigest(),
        }
        encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    def _path(self, key):
        return self.directory / f"{key}.json"

    @staticmethod
    def _valid_value(value):
        return isinstance(value, dict) and isinstance(value.get("final"), dict)

    def _read_entry(self, path, now):
        with path.open("r", encoding="utf-8") as stream:
            stored = json.load(stream)
        if isinstance(stored, dict) and stored.get("version") == CACHE_VERSION:
            value = stored.get("value")
            created_at = float(stored.get("created_at"))
            accessed_at = float(stored.get("accessed_at", created_at))
        else:
            value = stored
            created_at = path.stat().st_mtime
            accessed_at = created_at
        if not self._valid_value(value) or created_at <= 0 or accessed_at <= 0:
            raise ValueError("invalid cache entry")
        if now - created_at > self.ttl_seconds:
            path.unlink(missing_ok=True)
            return None
        return {"version": CACHE_VERSION, "created_at": created_at, "accessed_at": accessed_at, "value": value}

    def _write_entry(self, path, entry):
        temporary = path.with_suffix(f".{os.getpid()}.{threading.get_ident()}.tmp")
        try:
            with temporary.open("w", encoding="utf-8") as stream:
                json.dump(entry, stream, ensure_ascii=False, separators=(",", ":"))
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
        finally:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass

    def get(self, key):
        now = time.time()
        path = self._path(key)
        with self._lock:
            cached = self._memory.get(key)
            if cached and now - cached["created_at"] <= self.ttl_seconds:
                cached["accessed_at"] = now
                self._memory.move_to_end(key)
                try:
                    self._write_entry(path, cached)
                except OSError:
                    pass
                return cached["value"]
            if cached:
                self._memory.pop(key, None)
        try:
            entry = self._read_entry(path, now)
            if entry is None:
                return None
            entry["accessed_at"] = now
            self._write_entry(path, entry)
        except (OSError, ValueError, TypeError, OverflowError):
            return None
        with self._lock:
            self._memory[key] = entry
            self._trim_locked()
        return entry["value"]

    def set(self, key, value):
        if not self._valid_value(value):
            return
        now = time.time()
        entry = {"version": CACHE_VERSION, "created_at": now, "accessed_at": now, "value": value}
        path = self._path(key)
        self._write_entry(path, entry)
        with self._lock:
            self._memory[key] = entry
            self._trim_locked()
            self._trim_disk_locked()

    def _trim_locked(self):
        while len(self._memory) > self.max_entries:
            self._memory.popitem(last=False)

    def _trim_disk_locked(self):
        entries = []
        now = time.time()
        try:
            for path in self.directory.glob("*.json"):
                try:
                    entry = self._read_entry(path, now)
                    if entry is not None:
                        entries.append((entry["accessed_at"], path))
                except (OSError, ValueError, TypeError, OverflowError):
                    continue
            entries.sort(key=lambda item: item[0], reverse=True)
            for _, path in entries[self.max_entries:]:
                path.unlink(missing_ok=True)
        except OSError:
            pass

    def capabilities(self):
        return {"enabled": True, "type": "disk_lru_ttl", "max_entries": self.max_entries, "ttl_seconds": self.ttl_seconds}
