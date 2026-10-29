"""THE slug builder. Every channel (portal, email, WhatsApp) and the desk go through here.

Format: DDMMYY_CITY_SUBJECT_TYPE_REPORTER, uppercase ASCII and underscores.
    141026_UDR_PARYATAN_SEASON_AV_SUNITA
A second story with the same slug gets _2, _3 ... at the end.
The slug is also the FTP folder name: <FTP_BASE_DIR>/<slug>/.
"""
import re
from datetime import datetime, timedelta, timezone

from app.services.translit import has_devanagari, transliterate

IST = timezone(timedelta(hours=5, minutes=30))
# Same list as app.models.STORY_TYPES (the items check constraint); kept here so this module stays
# free of the database. tests/test_slug.py checks the two match.
STORY_TYPES = ("AV", "AVB", "PKG", "SF", "WT", "PTC", "BYTE", "LIVE", "TEXT", "BRIEF")
SUBJECT_MAX_CHARS = 30

SLUG_RE = re.compile(r"^\d{6}_[A-Z]{2,5}_[A-Z0-9]+(?:_[A-Z0-9]+)*_(?:" + "|".join(STORY_TYPES)
                     + r")_[A-Z0-9]+(?:_\d+)?$")

# Words that carry no subject. Hindi ones are removed before transliteration.
_HINDI_STOPWORDS = {
    "में", "की", "का", "के", "से", "को", "ने", "पर", "और", "है", "हैं", "था", "थी", "थे", "एक", "यह", "वह",
    "इस", "उस", "लिए", "तक", "भी", "ही", "तो", "गया", "गई", "गए", "हुआ", "हुई", "हुए", "कर", "किया", "रहा",
    "रही", "रहे", "साथ", "बाद", "द्वारा", "नहीं", "जा", "हो",
}
_ENGLISH_STOPWORDS = {"THE", "A", "AN", "OF", "IN", "ON", "AT", "TO", "FOR", "AND", "IS", "ARE", "WITH", "BY", "FROM"}


class SlugError(ValueError):
    """Raised when a slug cannot be built; the message is shown to the desk as is."""


def code(value: str | None) -> str:
    """Uppercase letters and digits only: ' udr ' -> 'UDR'."""
    return re.sub(r"[^A-Z0-9]", "", (value or "").upper())


def subject_words(text: str, max_words: int = 3) -> list[str]:
    """Keywords for the SUBJECT part, from English text or a Hindi headline."""
    words = []
    for raw in re.split(r"[\s,.;:!?।|/\\()\[\]\"'“”‘’\-–—]+", text or ""):
        if not raw:
            continue
        if has_devanagari(raw):
            if raw in _HINDI_STOPWORDS:
                continue
            raw = transliterate(raw)
        for word in re.split(r"[^A-Z0-9]+", raw.upper()):
            if word and word not in _ENGLISH_STOPWORDS:
                words.append(word)
    picked: list[str] = []
    for word in words:
        if len(picked) == max_words or len("_".join(picked + [word])) > SUBJECT_MAX_CHARS:
            break
        picked.append(word)
    return picked


def date_part(received_at: datetime) -> str:
    if received_at.tzinfo is None:
        received_at = received_at.replace(tzinfo=timezone.utc)
    return received_at.astimezone(IST).strftime("%d%m%y")


def build_slug(received_at: datetime, city_code: str | None, subject: str, story_type: str | None,
               reporter_code: str | None, max_words: int = 3) -> str:
    city = code(city_code)
    reporter = code(reporter_code)
    kind = code(story_type)
    words = subject_words(subject, max_words)
    if not city:
        raise SlugError("The reporter has no city code.")
    if not reporter:
        raise SlugError("Pick a reporter: the slug needs a reporter code.")
    if kind not in STORY_TYPES:
        raise SlugError(f"Story type must be one of {', '.join(STORY_TYPES)}.")
    if not words:
        raise SlugError("Add subject keywords: nothing usable in the headline.")
    slug = "_".join([date_part(received_at), city, *words, kind, reporter])
    error = slug_error(slug)
    if error:
        raise SlugError(error)
    return slug


def with_suffix(slug: str, n: int) -> str:
    return slug if n < 2 else f"{slug}_{n}"


def unique_slug(slug: str, taken: set[str]) -> str:
    n = 1
    while with_suffix(slug, n) in taken:
        n += 1
    return with_suffix(slug, n)


def slug_error(slug: str) -> str | None:
    if not slug:
        return "Slug is required."
    if not SLUG_RE.match(slug):
        return "Slug must look like DDMMYY_CITY_SUBJECT_TYPE_REPORTER, e.g. 141026_UDR_PARYATAN_SEASON_AV_SUNITA."
    return None
