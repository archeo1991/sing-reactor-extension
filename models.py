import math
import re

from pydantic import BaseModel, ConfigDict, field_validator, model_validator


MAX_DURATION = 15 * 60
LANGUAGE_RE = re.compile(r"^[a-z]{2,3}(?:-[A-Z]{2})?$")
SUPPORTED_LANGUAGES = {
    "af", "am", "ar", "as", "az", "ba", "be", "bg", "bn", "bo", "br", "bs", "ca", "cs", "cy", "da",
    "de", "el", "en", "es", "et", "eu", "fa", "fi", "fo", "fr", "gl", "gu", "ha", "haw", "he", "hi",
    "hr", "ht", "hu", "hy", "id", "is", "it", "ja", "jw", "ka", "kk", "km", "kn", "ko", "la", "lb",
    "ln", "lo", "lt", "lv", "mg", "mi", "mk", "ml", "mn", "mr", "ms", "mt", "my", "ne", "nl", "nn",
    "no", "oc", "pa", "pl", "ps", "pt", "ro", "ru", "sa", "sd", "si", "sk", "sl", "sn", "so", "sq",
    "sr", "su", "sv", "sw", "ta", "te", "tg", "th", "tk", "tl", "tr", "tt", "uk", "ur", "uz", "vi",
    "yi", "yo", "yue", "zh", "zu",
}


class TranscribeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    url: str
    start: float
    end: float
    language: str = "auto"

    @field_validator("start", "end")
    @classmethod
    def finite_time(cls, value):
        if not math.isfinite(value):
            raise ValueError("时间必须是有限数字")
        return value

    @field_validator("language")
    @classmethod
    def valid_language(cls, value):
        if value in {"", "auto"}:
            return value
        if not LANGUAGE_RE.fullmatch(value) or value not in SUPPORTED_LANGUAGES:
            raise ValueError("不支持的 language")
        return value

    @model_validator(mode="after")
    def valid_range(self):
        if self.start < 0 or self.end <= self.start:
            raise ValueError("时间范围无效：必须满足 0 <= start < end")
        if self.end - self.start > MAX_DURATION:
            raise ValueError("识别片段最长为 15 分钟")
        return self
