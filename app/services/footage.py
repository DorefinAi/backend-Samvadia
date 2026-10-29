"""Find a reporter's own footage on the newsroom FTP, read-only.

Reporters upload to their district's top folder, in date folders that every district spells its own way:

    /Ajmer/2026/OCT 2026/151026/161026_AJM_HADSA_AV_SHUBHAM        year / month / day / story
    /ALWER/OCTOBER 2026/161026/161026_alw17_baris_av1_tapukada.mp4  loose files named with the slug
    /BARAN/oct2026/161026/...    /Barmer/OCT_2026/161026    /Beawar/OCTOBER-26/191026
    /NAGOUR/2026/October_2026/19_OCT/...    /Jhalawad/OCTOBER-2026/19-10-26/...    /Pratapgarh/October/16/...
    /JAIPUR RULAR/OCTOBER 2026/CHOMU( JAIPUR)/<story>                a tehsil folder in between

So the walk is generic: each folder name is read as a year, a month (any spelling, with or without a year) or a day
(DDMMYY, DDMMYYYY, DD-MM-YY, 22_OCT, Oct _17, 16, 161026+). Only folders that fit one of the dates we look for are
opened; a folder without digits that is not a date (a tehsil) is opened once on the way. Per-district overrides
live in locations.folder_rules: {"containers": false} stops opening non-date folders, {"max_depth": n}.

Matching, for an item with a district and a slug key, in the day folders of the received date and the slug's date
(and the day before and after each, because folders are named by upload day):
  1. a folder whose normalised name equals the slug key, or starts with it, or the other way round;
  2. loose files in the day folder whose names start with the slug;
  3. only when nothing matched: a fuzzy match, and only when the date, the reporter and the type in the folder name
     all agree with the slug (loose guessing was wrong 2 times in 10 in the survey) and one candidate stands out.
Directory listings are cached for FOOTAGE_LIST_CACHE_SECONDS, so a scan of many items costs a few listings.
"""
import logging
import posixpath
import re
import threading
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from difflib import SequenceMatcher

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.services import ftp
from app.services.districts import District
from app.services.reporter_slug import match_key, parse
from app.services.slug import IST

log = logging.getLogger("footage")

_MONTHS = {"jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6, "jul": 7, "aug": 8, "sep": 9, "oct": 10,
           "nov": 11, "dec": 12}
_FULL_MONTHS = ("january", "february", "march", "april", "may", "june", "july", "august", "september", "october",
                "november", "december")
MAX_DEPTH = 5
MIN_PREFIX = 12  # a prefix match needs at least this many key characters on the shorter side
FUZZY_MIN = 0.75
FUZZY_MARGIN = 0.08
SKIP_SUFFIXES = (".part",)


# --- reading folder names -------------------------------------------------------------------------

@dataclass(frozen=True)
class Role:
    kind: str  # year | month | day
    year: int | None = None
    month: int | None = None
    day: int | None = None


def _month_of(word: str) -> int | None:
    """oct, OCT, October, OCTober, Octobar -> 10: the first three letters name the month, and the word is a prefix
    of the full name or a close misspelling of it (so 'octopus' is not October)."""
    w = word.lower()
    m = _MONTHS.get(w[:3])
    if not m or len(w) < 3:
        return None
    full = _FULL_MONTHS[m - 1]
    if full.startswith(w) or (abs(len(w) - len(full)) <= 2 and SequenceMatcher(None, w, full).ratio() >= 0.75):
        return m
    return None


def _valid(d: int, m: int | None, y: int | None) -> bool:
    if not 1 <= d <= 31 or (m is not None and not 1 <= m <= 12):
        return False
    if m is not None and y is not None:
        try:
            date(y, m, d)
        except ValueError:
            return False
    return True


def _year(text: str) -> int:
    y = int(text)
    return y + 2000 if len(text) <= 2 else y


def folder_roles(name: str) -> list[Role]:
    """What a folder name says as a date: [] when it is not a date folder. Some names are read two ways
    (OCTOBER-26: the month of 2026, or 26 October), and both roles are returned."""
    n = name.strip().lower().rstrip("+").strip()
    if not n or re.search(r"[^a-z0-9\s_\-.,/']", n):
        return []
    toks = re.findall(r"[a-z]+|\d+", n)
    alpha = [t for t in toks if t.isalpha()]
    nums = [t for t in toks if t.isdigit()]
    roles: list[Role] = []
    if not alpha:
        if len(nums) == 1:
            t = nums[0]
            if len(t) == 4 and re.fullmatch(r"20\d\d", t):
                roles.append(Role("year", year=int(t)))
            if len(t) == 6 and _valid(int(t[:2]), int(t[2:4]), _year(t[4:])):
                roles.append(Role("day", _year(t[4:]), int(t[2:4]), int(t[:2])))
            elif len(t) == 8 and _valid(int(t[:2]), int(t[2:4]), int(t[4:])) and t[4:6] == "20":
                roles.append(Role("day", int(t[4:]), int(t[2:4]), int(t[:2])))
            elif len(t) == 4 and _valid(int(t[:2]), int(t[2:]), None):
                roles.append(Role("day", None, int(t[2:]), int(t[:2])))
            elif len(t) <= 2 and _valid(int(t), None, None):
                roles.append(Role("day", None, None, int(t)))
        elif len(nums) in (2, 3) and all(len(x) <= 2 for x in nums[:2]) and (len(nums) == 2 or len(nums[2]) in (2, 4)):
            d, m = int(nums[0]), int(nums[1])
            y = _year(nums[2]) if len(nums) == 3 else None
            if _valid(d, m, y):
                roles.append(Role("day", y, m, d))
        return roles
    if len(alpha) != 1:
        return []
    m = _month_of(alpha[0])
    if not m:
        return []
    if not nums:
        return [Role("month", None, m)]
    if len(nums) == 1:
        x = nums[0]
        if len(x) == 4 and 2000 <= int(x) <= 2099:
            return [Role("month", int(x), m)]
        if len(x) <= 2:
            if _valid(int(x), m, None):
                roles.append(Role("day", None, m, int(x)))
            if n.index(alpha[0]) < n.index(x):  # OCTOBER-26: also the month of 2026 (16_OCT is only a day)
                roles.append(Role("month", 2000 + int(x), m))
        return roles
    if len(nums) == 2 and len(nums[0]) <= 2 and len(nums[1]) in (2, 4) and _valid(int(nums[0]), m, _year(nums[1])):
        return [Role("day", _year(nums[1]), m, int(nums[0]))]
    return []


# --- listings with a short cache -------------------------------------------------------------------

_cache: dict[str, tuple[float, list[ftp.Entry]]] = {}
_cache_lock = threading.Lock()


class Lister:
    """Directory listings through one remote, cached across calls (FOOTAGE_LIST_CACHE_SECONDS)."""

    def __init__(self, remote: ftp.Remote, ttl: float | None = None):
        """ttl 0: every folder is read fresh once (the desk's recheck); within one Lister a folder is never read
        twice either way."""
        self.remote = remote
        self.ttl = get_settings().footage_list_cache_seconds if ttl is None else ttl
        self.calls = 0
        self._seen: dict[str, list[ftp.Entry]] = {}

    def entries(self, path: str) -> list[ftp.Entry]:
        path = ftp.clean_path(path) or "/"
        if path in self._seen:
            return self._seen[path]
        with _cache_lock:
            hit = _cache.get(path)
        if hit and time.monotonic() - hit[0] < self.ttl:
            self._seen[path] = hit[1]
            return hit[1]
        self.calls += 1
        found = self.remote.scan(path)
        self._seen[path] = found
        with _cache_lock:
            _cache[path] = (time.monotonic(), found)
            if len(_cache) > 5000:  # keep the cache small: drop the oldest half
                for key, _ in sorted(_cache.items(), key=lambda kv: kv[1][0])[:2500]:
                    _cache.pop(key, None)
        return found

    def forget(self, path: str) -> None:
        with _cache_lock:
            _cache.pop(ftp.clean_path(path) or "/", None)


def clear_cache() -> None:
    with _cache_lock:
        _cache.clear()


# --- finding the day folders ------------------------------------------------------------------------

@dataclass
class _Ctx:
    year: int | None = None
    month: int | None = None
    in_container: bool = False


def _month_fits(role: Role, ctx: _Ctx, dates: set[date]) -> bool:
    year = role.year or ctx.year
    return any(t.month == role.month and (year is None or year == t.year) for t in dates)


def _day_fits(role: Role, ctx: _Ctx, dates: set[date]) -> bool:
    month = role.month or ctx.month
    year = role.year or ctx.year
    if month is None:
        return False  # a bare "16" only counts inside a month folder we already matched
    return any(t.day == role.day and t.month == month and (year is None or year == t.year) for t in dates)


def find_containers(lister: Lister, root: str, dates: set[date], rules: dict | None = None) -> list[tuple[str, str]]:
    """[(path, 'day' | 'month')]: the day folders for these dates under a district folder, and the matching month
    folders (some districts put story folders straight into the month folder)."""
    rules = rules or {}
    max_depth = int(rules.get("max_depth") or MAX_DEPTH)
    open_containers = rules.get("containers", True) is not False
    out: list[tuple[str, str]] = []
    seen: set[str] = set()

    def walk(path: str, depth: int, ctx: _Ctx) -> None:
        if depth >= max_depth:
            return
        for e in lister.entries(path):
            if not e.is_dir:
                continue
            child = posixpath.join(path, e.name)
            roles = folder_roles(e.name)
            if not roles:
                if open_containers and not ctx.in_container and not re.search(r"\d", e.name) and depth < 3:
                    if ctx.month is not None and (child, "month") not in seen:
                        seen.add((child, "month"))  # /JAIPUR RULAR/OCTOBER 2026/CHOMU( JAIPUR)/<story>
                        out.append((child, "month"))
                    walk(child, depth + 1, _Ctx(ctx.year, ctx.month, True))
                continue
            for role in roles:
                if role.kind == "year" and any(t.year == role.year for t in dates) and ctx.year is None:
                    walk(child, depth + 1, _Ctx(role.year, ctx.month, ctx.in_container))
                elif role.kind == "month" and ctx.month is None and _month_fits(role, ctx, dates):
                    if (child, "month") not in seen:
                        seen.add((child, "month"))
                        out.append((child, "month"))
                    walk(child, depth + 1, _Ctx(role.year or ctx.year, role.month, ctx.in_container))
                elif role.kind == "day" and _day_fits(role, ctx, dates):
                    if (child, "day") not in seen:
                        seen.add((child, "day"))
                        out.append((child, "day"))
                        walk_day(child, depth + 1, _Ctx(role.year or ctx.year, role.month or ctx.month, True))

    def walk_day(path: str, depth: int, ctx: _Ctx) -> None:
        """A day folder inside a day folder (/Jhalawad/OCTOBER-2026/111026/19-10-26) is a day folder too."""
        if depth >= max_depth:
            return
        for e in lister.entries(path):
            if e.is_dir and any(r.kind == "day" and _day_fits(r, ctx, dates) for r in folder_roles(e.name)):
                child = posixpath.join(path, e.name)
                if (child, "day") not in seen:
                    seen.add((child, "day"))
                    out.append((child, "day"))

    walk(ftp.clean_path(root), 0, _Ctx())
    return out


# --- matching --------------------------------------------------------------------------------------------

def _stem(name: str) -> str:
    base, ext = posixpath.splitext(name)
    return base if 1 <= len(ext) <= 6 else name


def loose_key(key: str) -> str:
    """av1 / avb2 -> av / avb, so 161026_alw17_baris_av2_tapukada.mp4 belongs to the av1 slug's story."""
    return re.sub(r"(av+b*)\d+", r"\1", key)


def _prefix(a: str, b: str) -> bool:
    short, long_ = sorted((a, b), key=len)
    return (len(short) >= MIN_PREFIX and long_.startswith(short) and re.search(r"[a-z]", short) is not None
            and len(short) >= 0.5 * len(long_))


@dataclass
class Match:
    path: str
    how: str  # exact | prefix | files | fuzzy
    files: list[dict] = field(default_factory=list)
    bytes: int = 0


def _file_out(name: str, e: ftp.Entry) -> dict:
    return {"name": name, "size": e.size,
            "modified": e.modified.astimezone(timezone.utc).isoformat().replace("+00:00", "Z") if e.modified else None}


def list_files(lister: Lister, path: str) -> list[dict]:
    """Files in a story folder and one level of subfolders, as {name, size, modified}."""
    out = []
    for e in lister.entries(path):
        if e.name.endswith(SKIP_SUFFIXES):
            continue
        if e.is_dir:
            for sub in lister.entries(posixpath.join(path, e.name)):
                if not sub.is_dir and not sub.name.endswith(SKIP_SUFFIXES):
                    out.append(_file_out(f"{e.name}/{sub.name}", sub))
        else:
            out.append(_file_out(e.name, e))
    return sorted(out, key=lambda f: f["name"])


def _folder_match(lister: Lister, path: str, how: str) -> Match:
    files = list_files(lister, path)
    return Match(path, how, files, sum(f["size"] or 0 for f in files))


def _agree(a: str | None, b: str | None) -> bool:
    a, b = re.sub(r"[^A-Z]", "", (a or "").upper()), re.sub(r"[^A-Z]", "", (b or "").upper())
    if not a or not b:
        return False
    return a == b or (min(len(a), len(b)) >= 4 and (a.startswith(b) or b.startswith(a)))


def find(lister: Lister, district: District, key: str, *, received: datetime, parsed: dict | None,
         subject: str | None = None) -> Match | None:
    """The best match for one slug key under the district's folder, or None. subject: the slug's subject words
    (ParsedSlug.subject), needed only for the fuzzy step."""
    if not district.ftp_folder or not key:
        return None
    parsed = parsed or {}
    received_day = received.astimezone(IST).date()
    slug_day = None
    if parsed.get("date"):
        try:
            slug_day = date.fromisoformat(parsed["date"])
        except ValueError:
            slug_day = None
    dates = set()
    for d in filter(None, (received_day, slug_day)):
        dates |= {d - timedelta(days=1), d, d + timedelta(days=1)}
    root = posixpath.join("/", district.ftp_folder)
    containers = find_containers(lister, root, dates, district.folder_rules)
    # Folders named for the slug's own day first, then the received day, then the neighbours
    preferred = {d.strftime("%d%m%y") for d in filter(None, (slug_day, received_day))}
    containers.sort(key=lambda c: (c[1] != "day", not any(p in c[0] for p in preferred)))

    loose = loose_key(key)
    best: tuple[int, int, str] | None = None
    best_match: Match | None = None
    for path, _kind in containers:
        entries = lister.entries(path)
        for e in entries:
            if not e.is_dir:
                continue
            k = match_key(e.name)
            if k == key:
                rank = 0
            elif k and (_prefix(k, key) or _prefix(loose_key(k), loose)):
                rank = 1
            else:
                continue
            score = (rank, abs(len(k) - len(key)), posixpath.join(path, e.name))
            if best is None or score < best:
                best = score
        if best is not None and best[0] == 0:
            break
    if best is not None:
        return _folder_match(lister, best[2], "exact" if best[0] == 0 else "prefix")

    # Loose files in a day folder, named with the slug (ALWER)
    for path, _kind in containers:
        hits = [e for e in lister.entries(path) if not e.is_dir and not e.name.endswith(SKIP_SUFFIXES)
                and (sk := loose_key(match_key(_stem(e.name)))) and (sk.startswith(loose) or _prefix(sk, loose))]
        if hits:
            files = sorted((_file_out(e.name, e) for e in hits), key=lambda f: f["name"])
            best_match = Match(path, "files", files, sum(f["size"] or 0 for f in files))
            break
    if best_match:
        return best_match
    return _fuzzy(lister, containers, key, received_day, parsed, subject)


def _fuzzy(lister: Lister, containers, key: str, received_day: date, parsed: dict,
           subject: str | None) -> Match | None:
    """Date, reporter and type must all agree, the subject words must be nearly the same (spelling slips:
    bahishkar / bahiskaar), and one candidate must stand out."""
    if not (parsed.get("date") and parsed.get("reporter_name") and parsed.get("type")):
        return None
    want = loose_key(match_key(subject or ""))
    if len(want) < 4:
        return None  # no subject to compare: two stories of one reporter on one day look alike
    scored: list[tuple[float, str, bool, ftp.Entry]] = []
    for path, _kind in containers:
        for e in lister.entries(path):
            name = e.name if e.is_dir else _stem(e.name)
            p = parse(name, None, received=received_day)
            if p is None or p.date is None or p.date.isoformat() != parsed["date"]:
                continue
            if p.type != parsed["type"] or not _agree(p.reporter_name, parsed["reporter_name"]):
                continue
            got = loose_key(match_key(p.subject))
            if len(got) < 4:
                continue
            ratio = SequenceMatcher(None, got, want).ratio()
            if ratio >= FUZZY_MIN:
                scored.append((ratio, path, e.is_dir, e))
    if not scored:
        return None
    scored.sort(key=lambda s: -s[0])
    top = scored[0]
    if top[2]:
        rivals = [s for s in scored[1:] if s[2] and top[0] - s[0] < FUZZY_MARGIN]
        if rivals:
            return None  # two folders fit about equally: leave it to the desk
        return _folder_match(lister, posixpath.join(top[1], top[3].name), "fuzzy")
    files = [s for s in scored if not s[2] and s[1] == top[1] and top[0] - s[0] < FUZZY_MARGIN]
    out = sorted((_file_out(s[3].name, s[3]) for s in files), key=lambda f: f["name"])
    return Match(top[1], "fuzzy", out, sum(f["size"] or 0 for f in out))


# --- browsing and checking a path the desk picked ----------------------------------------------------------

def stat_path(lister: Lister, path: str) -> ftp.Entry | None:
    """The entry for path (as listed in its parent), or None when it does not exist."""
    path = ftp.clean_path(path)
    if path in ("", "/"):
        return ftp.Entry("/", True)
    parent, name = posixpath.split(path)
    return next((e for e in lister.entries(parent or "/") if e.name == name), None)


def files_at(lister: Lister, path: str) -> Match | None:
    """The footage at a path the desk picked: a folder (its files) or one file."""
    entry = stat_path(lister, path)
    if entry is None:
        return None
    if entry.is_dir:
        return _folder_match(lister, ftp.clean_path(path), "manual")
    f = _file_out(entry.name, entry)
    return Match(ftp.clean_path(path), "manual", [f], f["size"] or 0)


def under_district(path: str, districts: list[District]) -> District | None:
    """The district whose top folder holds path (case as on the server), or None."""
    clean = ftp.clean_path(path)
    for d in districts:
        if d.ftp_folder:
            top = "/" + d.ftp_folder.strip("/")
            if clean == top or clean.startswith(top + "/"):
                return d
    return None


# --- item status -----------------------------------------------------------------------------------------

def wait_hours() -> timedelta:
    return timedelta(hours=get_settings().footage_wait_hours)


def eligible(item) -> bool:
    """A story with a district (that has an FTP folder) and a reporter slug: something to match on."""
    return (item.kind == "story" and bool(item.slug_key) and item.district is not None
            and bool(item.district.ftp_folder))


def start(item, now: datetime | None = None) -> None:
    """Called when an item gets its slug, district or kind: start (or stop) looking for its footage."""
    if item.footage_status == "manual":
        return
    if eligible(item):
        if item.footage_status == "not_checked":
            item.footage_status = "waiting"
            item.footage_wait_until = item.received_at + wait_hours()
    elif item.footage_status in ("waiting", "not_found", "found"):
        item.footage_status, item.footage_path, item.footage_files, item.footage_bytes = "not_checked", None, [], 0
        item.footage_wait_until = None


def footage_out(item) -> dict:
    def iso(dt):
        return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z") if dt else None

    return {"status": item.footage_status, "path": item.footage_path, "files": list(item.footage_files or []),
            "bytes": int(item.footage_bytes or 0), "checked_at": iso(item.footage_checked_at),
            "wait_until": iso(item.footage_wait_until)}


def _district_of(item) -> District | None:
    loc = item.district
    if loc is None:
        return None
    return District(loc.code, loc.name, loc.name_hi, tuple(loc.aliases or ()), loc.ftp_folder,
                    dict(loc.folder_rules or {}))


def compute(lister: Lister, item) -> Match | None:
    """The footage for an item as it is now on the FTP (manual: what is at the desk's path)."""
    if item.footage_status == "manual":
        return files_at(lister, item.footage_path) if item.footage_path else None
    district = _district_of(item)
    if district is None or not eligible(item):
        return None
    return find(lister, district, item.slug_key, received=item.received_at, parsed=item.parsed,
                subject=(item.parsed or {}).get("subject"))


def apply(item, match: Match | None, now: datetime) -> bool:
    """Write a check's result onto the item. Returns True when what the desk sees changed."""
    manual = item.footage_status == "manual"
    if match is not None and ((match.how == "manual") != manual or (
            manual and match.path != ftp.clean_path(item.footage_path or ""))):
        # Computed for another state (the desk picked, changed or cleared a manual path while the FTP was being
        # read): files from folder A must never land under path B. Leave the item as the desk set it.
        return False
    before = (item.footage_status, item.footage_path, item.footage_bytes, len(item.footage_files or []))
    item.footage_checked_at = now
    if item.footage_status == "manual":
        if match is not None:
            item.footage_files, item.footage_bytes = match.files, match.bytes
    elif not eligible(item):
        start(item, now)
    elif match is not None:
        item.footage_status, item.footage_path = "found", match.path
        item.footage_files, item.footage_bytes = match.files, match.bytes
    elif item.footage_status == "found" and item.footage_path:
        pass  # matched before, nothing now (FTP hiccup or a renamed folder): keep what we had
    elif item.footage_wait_until and now >= item.footage_wait_until:
        item.footage_status, item.footage_path, item.footage_files, item.footage_bytes = "not_found", None, [], 0
    else:
        item.footage_status = "waiting"
    return before != (item.footage_status, item.footage_path, item.footage_bytes, len(item.footage_files or []))


def due_query(now: datetime, limit: int):
    """Items to check now: waiting ones, and found ones until one check after wait_until (files keep arriving),
    each at most once per FOOTAGE_RECHECK_SECONDS."""
    from app.models import Item

    recheck = now - timedelta(seconds=get_settings().footage_recheck_seconds)
    return (select(Item.id)
            .where(Item.footage_status.in_(("waiting", "found")), Item.kind == "story",
                   or_(Item.footage_checked_at.is_(None), Item.footage_checked_at <= recheck),
                   or_(Item.footage_status == "waiting", Item.footage_checked_at.is_(None),
                       Item.footage_wait_until.is_(None), Item.footage_checked_at <= Item.footage_wait_until))
            .order_by(Item.footage_checked_at.asc().nullsfirst(), Item.received_at)
            .limit(limit))


def due_ids(db: Session, now: datetime, limit: int | None = None) -> list:
    return list(db.scalars(due_query(now, limit or get_settings().footage_batch)))
