"""Read the slug a reporter typed: the email subject, else the first line of the text.

Real styles (30 days of mail, 7 days of FTP folders):

    161026_AJM_HADSA_AV_SHUBHAM                    DDMMYY
    19102026_BNS4_Bagidora_AV_Aag_Dharmendra       DDMMYYYY
    2610_JPR_MEDICAL_FOOD_KASER_PKG_VIKAS          DDMM (year from the received date)
    26.10.2026_khad vibhag news _av_pankaj         DD.MM.YYYY
    22 oct यात्रा धौलपुर                             DD Mon
    16,,10,,26 Kuchaman Aag A V B Murari           DD,,MM,,YY
    21_10_26_av_Oct_jhalawar_khanpur_...           DD_MM_YY (also DD-MM-YY, DD/MM/YYYY)
    2110026_jayal_BETHAK_av_ngr3_mahendra          DDMM0YY (and DD0MM0YY, 0DD0MM0YY: padded with zeros)
    JPR3-22102026-JPR3-POORNIMA-SANGANER-AVB-DINESH   up to two words before the date
    161025_dausa_election_exxlusive_laxmikant_wt   the type last, the name right before it

parse() returns None when the text is not a slug (breaking flashes, press notes, plain headlines).
A date that cannot be read for certain is None; it is never guessed. Pure functions, no database.
Every regex that reads a date or a type uses re.ASCII, and every dictionary lookup is guarded, so no input
raises (tests/test_reporter_slug.py fuzzes it).
"""
import re
import unicodedata
from dataclasses import dataclass
from datetime import date, timedelta

from app.services.districts import SEED_ALIASES, SEED_HINDI, WEAK_ALIASES, letters
from app.services.translit import transliterate_word

FOLDER_MAX = 96
FOLDER_MIN = 12  # shorter than this ("271026_AV") is too thin to name a folder

_A = re.ASCII
# Prefixes in front of the slug: replies/forwards, the "script" label in Hindi or English, "FTP".
_PREFIX = re.compile(r"^\s*(?:(?:re|fw|fwd|aw|wg)\s*:\s*|स्क्रिप्ट\s*[:：]?\s*-?\s*|script\s*[:_\-]+\s*|ftp\s+(?=\d))",
                     re.I)
_SEP = re.compile(r"[\s_\-,;:|/.+()\[\]]+")
_MONTH_NAMES = {
    "jan": 1, "january": 1, "feb": 2, "february": 2, "mar": 3, "march": 3, "apr": 4, "april": 4, "may": 5,
    "jun": 6, "june": 6, "jul": 7, "july": 7, "aug": 8, "august": 8, "sep": 9, "sept": 9, "september": 9,
    "oct": 10, "october": 10, "nov": 11, "november": 11, "dec": 12, "december": 12,
}
_MONTH_RE = "|".join(sorted(_MONTH_NAMES, key=len, reverse=True))

_DOTTED = re.compile(r"(\d{1,2})\.(\d{1,2})\.(\d{4}|\d{2})(?!\d)", _A)
_SEPARATED = re.compile(r"(\d{1,2})([-/_])(\d{1,2})\2(\d{4}|\d{2})(?!\d)", _A)
_COMMAS = re.compile(r"(\d{1,2})\s*,{1,3}\s*(\d{1,2})\s*,{1,3}\s*(\d{4}|\d{2})(?!\d)", _A)
_DAY_MONTH = re.compile(rf"(\d{{1,2}})[\s_\-]*({_MONTH_RE})(?![a-z])", re.I | _A)
_DIGITS = re.compile(r"(\d{4,9})(?!\d)", _A)
_LEAD_WORD = re.compile(r"([A-Za-z]{2,14}\d{0,3})[\s_\-]+", _A)
_STRONG = ("DDMMYY", "DDMMYYYY", "DD0MM0YY", "DD.MM.YYYY", "DD-MM-YYYY", "DD,,MM,,YY")
_LOOSE = ("DDMM", "DD Mon")

# Types as reporters write them -> the contract's types (AV, AVB, PKG, SPL, WT, 121, PTC, GFX, VOXPOP, BYTE,
# LIVE, TEXT, BRIEF). Intake keeps the ones in slug.STORY_TYPES as the item's story type.
_TYPES = {"AV": "AV", "AVB": "AVB", "PKG": "PKG", "PKJ": "PKG", "PACKAGE": "PKG", "SPL": "SPL", "SPECIAL": "SPL",
          "EXCLUSIVE": "SPL", "EXL": "SPL", "WT": "WT", "WALKTHROUGH": "WT", "121": "121", "PTC": "PTC",
          "GFX": "GFX", "VOXPOP": "VOXPOP",
          "BYTE": "BYTE", "BYTES": "BYTE", "LIVE": "LIVE", "TEXT": "TEXT", "BRIEF": "BRIEF", "BRIEFS": "BRIEF"}
# Also plain words ("CM BYTE", "LIVE telecast"): a type only when the slug has no other type, so
# ..._AV_BYTE_RAMESH stays AV and ..._BYTE_RAMESH is BYTE.
_WEAK_TYPES = {"BYTE", "LIVE", "TEXT", "BRIEF"}
_TYPE_MODIFIERS = {"VOKTHRU", "WOKTHRU", "VOKTRU", "WALKTHRU", "BYTE", "BYTES", "VISUAL", "VISUALS", "FEED"}
_NOT_CITIES = {"DAY", "PLAN", "BREAKING", "BIG", "NEWS", "SPEED", "FLASH", "PRESS", "NOTE", "RE", "FWD", "FTP",
               "SCRIPT", "AV", "AVB", "PKG", "THE"}
_DEVANAGARI = re.compile(r"[ऀ-ॿ]")
_DEVANAGARI_RUN = re.compile(r"[ऀ-ॿ]+")


@dataclass
class ParsedSlug:
    reporter_slug: str  # as typed, minus a Re:/Fwd:/script prefix
    slug_key: str  # lowercase a-z 0-9 only: for matching resends and FTP folders
    source: str  # subject | body
    date: date | None
    date_style: str | None
    city_token: str | None
    type: str | None
    reporter_name: str | None
    subject: str = ""  # the words between the city and the type: what the fuzzy FTP match compares

    def parsed(self) -> dict:
        """The `parsed` object of the API."""
        return {"date": self.date.isoformat() if self.date else None, "city_token": self.city_token,
                "type": self.type, "reporter_name": self.reporter_name}

    def stored(self) -> dict:
        """What items.parsed keeps: the API fields plus the subject words and date style (internal)."""
        return {**self.parsed(), "subject": self.subject, "date_style": self.date_style}


PARSED_FIELDS = ("date", "city_token", "type", "reporter_name")


def parsed_out(stored: dict | None) -> dict:
    """items.parsed -> the API's `parsed` (all null when nothing was parsed)."""
    return {k: (stored or {}).get(k) for k in PARSED_FIELDS}


def ascii_digits(text: str) -> str:
    """२२१०२६ -> 221026. One character in, one out, so positions stay the same."""
    out = []
    for c in text or "":
        if not c.isascii():
            d = unicodedata.decimal(c, None)
            if d is not None:
                out.append(str(d))
                continue
        out.append(c)
    return "".join(out)


def _translit_runs(text: str) -> str:
    return _DEVANAGARI_RUN.sub(lambda m: f" {transliterate_word(m.group(0))} ", text)


def slug_key(text: str | None) -> str:
    """Lowercase a-z 0-9 only (Hindi transliterated first): 161026_AJM_HADSA_AV_SHUBHAM -> 161026ajmhadsaavshubham.
    Used on both sides of a match (the reporter's slug and FTP folder/file names), so they compare alike."""
    text = _translit_runs(ascii_digits(unicodedata.normalize("NFC", text or "")))
    return re.sub(r"[^a-z0-9]", "", text.lower())


def _ascii_folder(text: str, limit: int = FOLDER_MAX) -> str:
    safe = re.sub(r"[^A-Za-z0-9]+", "_", _translit_runs(ascii_digits(text))).strip("_")
    return safe[:limit].rstrip("_")


def _thin(safe: str) -> bool:
    if len(safe) < FOLDER_MIN:
        return True
    words = [p for p in safe.split("_") if re.fullmatch(r"[A-Za-z]{3,}", p, _A) and p.upper() not in _TYPES
             and not re.fullmatch(r"AV+B*", p.upper(), _A)]
    return not words


def slug_part(reporter_slug: str) -> str:
    """The slug without a Hindi description typed after it ("..._Av_Younus दशहरा महोत्सव"), when what remains is
    enough to name a story; otherwise the whole text ("22 oct यात्रा धौलपुर")."""
    text = unicodedata.normalize("NFC", reporter_slug or "").strip()
    words = text.split()
    while words and _DEVANAGARI.search(words[-1]) and not re.search(r"[A-Za-z0-9]", words[-1]):
        words.pop()
    trimmed = " ".join(words)
    return trimmed if not _thin(_ascii_folder(trimmed, 10_000)) else text


def match_key(reporter_slug: str | None) -> str:
    """slug_key of slug_part(): what is compared with FTP folder and file names."""
    return slug_key(slug_part(reporter_slug or ""))


def folder_safe(reporter_slug: str) -> str:
    """ASCII letters and digits, everything else collapsed to _, at most 96 characters, the reporter's letter case
    kept. A Hindi description after the slug is left out (slug_part); Hindi elsewhere is transliterated
    (अंता -> anta)."""
    return _ascii_folder(slug_part(reporter_slug))


def folder_slug(reporter_slug: str | None) -> str | None:
    """folder_safe(), or None when the result is too thin to name a folder."""
    safe = folder_safe(reporter_slug or "")
    return None if _thin(safe) else safe


def strip_prefix(text: str) -> str:
    text = (text or "").strip()
    for _ in range(6):
        cut = _PREFIX.sub("", text, count=1)
        if cut == text:
            break
        text = cut
    return text.strip()


def _make_date(d: int, m: int, year: int | str) -> date | None:
    y = int(year)
    if isinstance(year, str) and len(year) == 2:
        y += 2000
    if not 2000 <= y <= 2099:
        return None
    try:
        return date(y, m, d)
    except ValueError:
        return None


def _near(d: int, m: int, received: date | None) -> date | None:
    """A date without a year: the received year, or the year before when that would put it well in the future."""
    if received is None:
        return None
    found = _make_date(d, m, received.year)
    if found and found > received + timedelta(days=7):
        found = _make_date(d, m, received.year - 1)
    return found


def _read_digits(digits: str, received: date | None) -> tuple[date | None, str | None]:
    n = len(digits)
    if n == 6:
        found = _make_date(int(digits[:2]), int(digits[2:4]), digits[4:])
        return (found, "DDMMYY") if found else (None, None)
    if n == 8:
        found = _make_date(int(digits[:2]), int(digits[2:4]), digits[4:])
        if found:
            return found, "DDMMYYYY"
    if n in (7, 8, 9):
        # zero padding: 2110026 (DD MM 0YY), 23010026 (DD 0MM 0YY), 023010026 (0DD 0MM 0YY)
        for pattern in (r"(\d{2})(\d{2})0(\d{2})", r"(\d{2})0(\d{2})0(\d{2})", r"0(\d{2})0(\d{2})0(\d{2})"):
            p = re.fullmatch(pattern, digits, _A)
            if p and (found := _make_date(int(p[1]), int(p[2]), p[3])):
                return found, "DD0MM0YY"
    if n == 4:
        day, month = int(digits[:2]), int(digits[2:])
        if 1 <= day <= 31 and 1 <= month <= 12:
            return _near(day, month, received), "DDMM"
    return None, None


def _read_date(text: str, received: date | None) -> tuple[date | None, str | None, int] | None:
    """(date, style, characters used) for a date at the start of text; None if it does not start with digits."""
    if m := _DOTTED.match(text):
        return _make_date(int(m[1]), int(m[2]), m[3]), "DD.MM.YYYY", m.end()
    if m := _SEPARATED.match(text):
        return _make_date(int(m[1]), int(m[3]), m[4]), "DD-MM-YYYY", m.end()
    if m := _COMMAS.match(text):
        return _make_date(int(m[1]), int(m[2]), m[3]), "DD,,MM,,YY", m.end()
    if m := _DAY_MONTH.match(text):
        month = _MONTH_NAMES.get(m[2].lower())
        return (_near(int(m[1]), month, received) if month else None), "DD Mon", m.end()
    if m := _DIGITS.match(text):
        found, style = _read_digits(m[1], received)
        return found, style, m.end()  # style None: digits where a date belongs, but not a readable one
    return None


def tokens(text: str) -> list[str]:
    return [t for t in _SEP.split(text or "") if t]


def normalise_type(token: str | None) -> str | None:
    """AV1 -> AV, AVVB / AVBB / AVBBB / AVB2 -> AVB, PKJ -> PKG, EXCLUSIVE -> SPL, BYTES -> BYTE; None when not a type."""
    t = (token or "").upper()
    if not t.isascii():
        return None
    if re.fullmatch(r"AV+B+\d?", t, _A):
        return "AVB"
    if re.fullmatch(r"AV+\d?", t, _A):
        return "AV"
    return _TYPES.get(t)


def _type_at(toks: list[str], i: int) -> tuple[str, int] | None:
    """(type, tokens used) when a type starts at toks[i]. Handles A V B and VOX POP."""
    kind = normalise_type(toks[i])
    if kind:
        return kind, 1
    t = toks[i].upper()
    if t == "VOX" and i + 1 < len(toks) and toks[i + 1].upper() == "POP":
        return "VOXPOP", 2
    if t == "ONE" and toks[i + 1:i + 3] and [x.upper() for x in toks[i + 1:i + 3]] == ["TO", "ONE"]:
        return "121", 3
    spelled = []
    for tok in toks[i:i + 4]:
        if len(tok) != 1 or not tok.isascii() or not tok.isalpha():
            break
        spelled.append(tok.upper())
    joined = "".join(spelled)
    if len(spelled) >= 2 and re.fullmatch(r"AV+B*", joined, _A):
        return ("AVB" if "B" in joined else "AV"), len(spelled)
    return None


def _find_type(toks: list[str], start: int) -> tuple[str, int, int] | None:
    """The first type that has a word after it, else the last one: (type, index, length)."""
    found = []
    i = start
    while i < len(toks):
        hit = _type_at(toks, i)
        if hit:
            found.append((hit[0], i, hit[1]))
            i += hit[1]
        else:
            i += 1
    found = [f for f in found if f[0] not in _WEAK_TYPES] or found
    if not found:
        return None
    with_name = [f for f in found if f[1] + f[2] < len(toks) and not _DEVANAGARI.search(toks[f[1] + f[2]])]
    later = [f for f in with_name if f[1] > start]  # 21_10_26_av_Oct_..._av_JHL-5: not the one right at the start
    return (later or with_name)[0] if with_name else found[-1]


def _is_place(word: str, aliases: frozenset[str], hindi: frozenset[str]) -> bool:
    key = letters(word)
    return (bool(key) and key in aliases) or unicodedata.normalize("NFC", word) in hindi


def _name_after(words: list[str], aliases: frozenset[str], hindi: frozenset[str]) -> str | None:
    words = list(words)
    while words:  # more types and qualifiers right after the type: PKG_GFX_VIKAS, AV vokthru subhash
        if words[0].upper() in _TYPE_MODIFIERS:
            words.pop(0)
        elif hit := _type_at(words, 0):
            del words[:hit[1]]
        else:
            break
    for i, w in enumerate(words):  # a Hindi description after the name
        if _DEVANAGARI.search(w) and i > 0:
            words = words[:i]
            break

    def noise(w: str) -> bool:
        return any(c.isdigit() for c in w) or _is_place(w, aliases, hindi) or w.upper() in _TYPE_MODIFIERS

    while len(words) > 1 and noise(words[0]):
        words.pop(0)
    while len(words) > 1 and noise(words[-1]):
        words.pop()
    words = [w for w in words if not any(c.isdigit() for c in w) and w.upper() not in _TYPE_MODIFIERS][:3]
    name = " ".join(words).upper()
    return name if name and not _DEVANAGARI.search(name) else None


def _name_before(toks: list[str], index: int, start: int, aliases, hindi) -> str | None:
    """The type came last (..._laxmikant_wt): the word right before it is the name."""
    if index - 1 < start:
        return None
    word = toks[index - 1]
    if re.fullmatch(r"[A-Za-z]{3,}", word, _A) and not _is_place(word, aliases, hindi):
        return word.upper()
    return None


def _city_token(toks: list[str], aliases: frozenset[str], hindi: frozenset[str]) -> tuple[str | None, int]:
    """(city token as typed, tokens used): a known code/place in the first three words (a weak code such as
    BAL or DID only in the first position), else an unknown capitalised code like XYZ12 in the first position."""
    for i, tok in enumerate(toks[:3]):
        if _type_at(toks, i):
            continue
        key = letters(tok)
        known = (key and key in aliases and (i == 0 or key not in WEAK_ALIASES)
                 and re.fullmatch(r"[A-Za-z]{2,14}\d{0,3}", tok, _A))
        if known or unicodedata.normalize("NFC", tok) in hindi:
            # "BAL 2", "BRN 3": a short number after the code belongs to it
            if i + 1 < len(toks) and re.fullmatch(r"\d{1,2}", toks[i + 1], _A) and not any(c.isdigit() for c in tok):
                return f"{tok} {toks[i + 1]}", i + 2
            return tok, i + 1
    if toks:
        first = toks[0]
        if (re.fullmatch(r"[A-Z]{2,6}\d{0,3}", first, _A) and letters(first) not in _NOT_CITIES
                and _type_at(toks, 0) is None):
            return first, 1
    return None, 0


def _parse_one(text: str, source: str, received: date | None, aliases, hindi) -> ParsedSlug | None:
    raw = strip_prefix(text)
    if not raw:
        return None
    norm = ascii_digits(raw)  # same length as raw: positions carry over
    lead: list[str] = []
    rest = norm
    read = _read_date(rest, received)
    if read is None:  # up to two words before a full date: JPR3-22102026-..., VIJUL_FTP_22102026_...
        probe = norm
        for _ in range(2):
            m = _LEAD_WORD.match(probe)
            if not m:
                break
            lead.append(m[1])
            probe = probe[m.end():]
            found = _read_date(probe, received)
            if found is not None and found[1] in _STRONG:
                rest, read = probe, found
                break
        if read is None:
            lead = []
    separators = len(re.findall(r"[_\-]", raw))
    if read is not None:
        when, style, used = read
        after = tokens(rest[used:])
        if not any(re.search(r"[^\W\d_]", t) for t in after):
            return None  # nothing but the date
        typed = _find_type(after, 0)
        # a weak type is a plain word ("LIVE updates", "TEXT alerts"): after a loose or unreadable date it does
        # not make a headline a slug; only a real type or a line built with separators does
        strong_typed = typed if typed and typed[0] not in _WEAK_TYPES else None
        if style is None and (strong_typed is None or len(after) < 2):
            return None  # "2026 elections results": digits that are not a date, and no story type
        hindi_place = any(unicodedata.normalize("NFC", t) in hindi for t in after[:3])
        if style in _LOOSE and strong_typed is None and separators < 2 and not hindi_place:
            return None  # "3 May rally in Jaipur", "1210 villages": a headline, not a slug
        if style in _STRONG and not any(re.search(r"[A-Za-z]", t) for t in after):
            return None  # a dated Hindi press note: "12/10/2026 को जयपुर में बैठक"
    else:
        # No date: still a slug when it is built like one (JDP_POLICE_KARYWAHI_AV_RAJIV)
        if separators < 3 or not re.match(r"[A-Za-z]{2,6}\d{0,3}[_\-]", norm, _A):
            return None
        when, style, after = None, None, tokens(norm)
        typed = _find_type(after, 1)
        # a weak type alone (BREAKING-JAIPUR-LIVE-UPDATE) does not make an undated line a slug
        if typed is None or typed[0] in _WEAK_TYPES or typed[1] + typed[2] >= len(after):
            return None
    city, start = _city_token(after, aliases, hindi)
    if lead and letters(lead[0]) in aliases:  # JPR3-22102026-...: the code came first
        city = lead[0]
        if after and letters(after[0]) == letters(lead[0]):
            start = max(start, 1)  # ...and again after the date
    typed = _find_type(after, start)
    kind = name = None
    subject_words = after[start:]
    if typed:
        kind, index, length = typed
        tail = after[index + length:]
        name = _name_after(tail, aliases, hindi) if tail else _name_before(after, index, start, aliases, hindi)
        subject_words = after[start:index]
        if not tail and name and subject_words and subject_words[-1].upper() == name:
            subject_words = subject_words[:-1]
    return ParsedSlug(reporter_slug=raw, slug_key=match_key(raw), source=source, date=when, date_style=style,
                      city_token=city, type=kind, reporter_name=name, subject=" ".join(subject_words))


def parse(subject: str | None, body_first_line: str | None = None, *, received: date | None = None,
          aliases: frozenset[str] = SEED_ALIASES, hindi: frozenset[str] = SEED_HINDI) -> ParsedSlug | None:
    """The subject first, then the first body line. `received` (India date) gives DDMM and "22 oct" their year;
    without it those dates stay None. Never raises."""
    for text, source in ((subject, "subject"), (body_first_line, "body")):
        if not text or not text.strip():
            continue
        line = text.strip().splitlines()[0][:300]
        try:
            found = _parse_one(line, source, received, aliases, hindi)
        except Exception:  # a parser bug must never stop a message from being stored
            found = None
        if found:
            return found
    return None
