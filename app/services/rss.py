"""The Octopus RSS feed: story text and XML. Pure functions, no database.

Mapping confirmed in the 14–16 Oct tests (Final Build Plan §2.2) and the production template
(phase-1/vartmaan-production-template.xml):

    <title>        FLASH / UPDATE / KILL prefix if needed, then the slug   -> Octopus title (the slug)
    <description>  city line, body, reporter line; (( )) block on top      -> story body; (( )) not read
    <category>     district name                                          -> subcategory
    <pubDate>      published time, RFC 2822                               -> received / sort order
    <guid>         vartmaan-{slug}-v{version}, generated once, stored     -> dedupe key, never reused
    <link>         story permalink

No dc:creator, dc:subject or priority element: Octopus ignores them.
"""
import re
from datetime import datetime, timezone
from email.utils import format_datetime

from lxml import etree

from app.services.slug import IST

SECONDS_PER_WORD = 0.33
ATOM = "http://www.w3.org/2005/Atom"


def guid_for(slug: str, version: int) -> str:
    return f"vartmaan-{slug}-v{version}"


def title_for(slug: str, kind: str, priority: str) -> str:
    if kind == "update":
        return f"UPDATE {slug}"
    if kind == "kill":
        return f"KILL {slug}"
    return f"FLASH {slug}" if priority == "breaking" else slug


def description_for(*, kind: str, city_hi: str, body: str, reporter: str, replaces_slug: str | None = None) -> str:
    """City line, body, reporter line. An UPDATE or KILL starts with a (( )) note naming the slug it replaces;
    Octopus leaves (( )) text out of the read time, so the anchor never reads it on air."""
    parts = []
    if kind == "update" and replaces_slug:
        parts.append(f"((पिछला संस्करण रद्द करें: {replaces_slug}))")
    if kind == "kill":
        parts.append(f"((यह खबर रद्द है, प्रसारित न करें: {replaces_slug}))")
        return "\n\n".join(parts)
    # No city (an unknown sender with a hand-typed slug): leave out the city line and the bureau
    byline = f"संवाददाता: {reporter}, {city_hi} ब्यूरो" if city_hi else f"संवाददाता: {reporter}"
    parts += [f"{city_hi}:" if city_hi else "", body.strip(), byline]
    return "\n\n".join(p for p in parts if p)


def read_seconds(description: str) -> int:
    spoken = re.sub(r"\(\(.*?\)\)", " ", description, flags=re.S)
    return round(len(spoken.split()) * SECONDS_PER_WORD)


def rfc2822(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return format_datetime(dt.astimezone(IST))


def _cdata(parent, tag: str, text: str):
    el = etree.SubElement(parent, tag)
    el.text = etree.CDATA(text)
    return el


def _text(parent, tag: str, text: str, **attrs):
    el = etree.SubElement(parent, tag, **attrs)
    el.text = text
    return el


def build_feed(stories: list[dict], *, self_url: str, link_base: str) -> bytes:
    """stories: newest first, each {title, description, district, guid, slug, published_at}."""
    rss = etree.Element("rss", version="2.0", nsmap={"atom": ATOM})
    channel = etree.SubElement(rss, "channel")
    _text(channel, "title", "Vartmaan AI News Wire")
    _text(channel, "link", link_base.rsplit("/", 1)[0] or link_base)
    etree.SubElement(channel, f"{{{ATOM}}}link", href=self_url, rel="self", type="application/rss+xml")
    _text(channel, "description", "Vartmaan AI Automated Newsroom Ingestion Feed")
    _text(channel, "language", "hi")
    _text(channel, "ttl", "60")
    newest = stories[0]["published_at"] if stories else datetime.now(timezone.utc)
    _text(channel, "lastBuildDate", rfc2822(newest))
    for s in stories:
        item = etree.SubElement(channel, "item")
        _cdata(item, "title", s["title"])
        _cdata(item, "description", s["description"])
        _text(item, "link", f"{link_base.rstrip('/')}/{s['slug']}")
        _text(item, "guid", s["guid"], isPermaLink="false")
        _cdata(item, "category", s["district"])
        _text(item, "pubDate", rfc2822(s["published_at"]))
    return etree.tostring(rss, xml_declaration=True, encoding="UTF-8", pretty_print=True)
