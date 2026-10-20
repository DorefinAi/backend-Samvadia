from datetime import datetime, timezone

from lxml import etree

from app.services.rss import build_feed, description_for, guid_for, read_seconds, rfc2822, title_for

SLUG = "141026_UDR_PARYATAN_SEASON_AV_SUNITA"
AT = datetime(2026, 10, 14, 16, 0, tzinfo=timezone.utc)  # 21:30 IST


def test_title_prefixes():
    assert title_for(SLUG, "original", "normal") == SLUG
    assert title_for(SLUG, "original", "breaking") == "FLASH " + SLUG
    assert title_for(SLUG, "update", "breaking") == "UPDATE " + SLUG
    assert title_for(SLUG, "kill", "normal") == "KILL " + SLUG


def test_guid_is_deterministic():
    assert guid_for(SLUG, 1) == f"vartmaan-{SLUG}-v1"


def test_description_matches_production_template():
    d = description_for(kind="original", city_hi="उदयपुर", body="झीलों की नगरी में पर्यटन सीजन शुरू।",
                        reporter="सुनीता मेहता")
    assert d == "उदयपुर:\n\nझीलों की नगरी में पर्यटन सीजन शुरू।\n\nसंवाददाता: सुनीता मेहता, उदयपुर ब्यूरो"


def test_description_without_city_has_no_empty_city_line():
    d = description_for(kind="original", city_hi="", body="अजमेर में मेला।", reporter="New Stringer")
    assert d == "अजमेर में मेला।\n\nसंवाददाता: New Stringer"


def test_update_starts_with_cancel_note_not_read_aloud():
    d = description_for(kind="update", city_hi="उदयपुर", body="नई जानकारी।", reporter="सुनीता", replaces_slug=SLUG)
    assert d.startswith(f"((पिछला संस्करण रद्द करें: {SLUG}))\n\nउदयपुर:")
    assert read_seconds(d) == round(len("उदयपुर: नई जानकारी। संवाददाता: सुनीता, उदयपुर ब्यूरो".split()) * 0.33)


def test_pubdate_in_india_time():
    assert rfc2822(AT) == "Wed, 14 Oct 2026 21:30:00 +0530"


def test_feed_xml_uses_cdata_and_parses():
    xml = build_feed([{"title": "FLASH " + SLUG, "description": "उदयपुर:\n\n<b>&</b> पाठ", "district": "Udaipur",
                       "guid": guid_for(SLUG, 1), "slug": SLUG, "published_at": AT}],
                     self_url="https://api.example/feed/s/feed.xml", link_base="https://dorefinai.com/wire")
    assert b"<![CDATA[FLASH " + SLUG.encode() + b"]]>" in xml
    assert b"<![CDATA[Udaipur]]>" in xml
    assert b"dc:creator" not in xml
    root = etree.fromstring(xml)
    item = root.find("channel/item")
    assert item.findtext("description") == "उदयपुर:\n\n<b>&</b> पाठ"
    assert item.findtext("guid") == f"vartmaan-{SLUG}-v1" and item.find("guid").get("isPermaLink") == "false"
    assert item.findtext("link") == f"https://dorefinai.com/wire/{SLUG}"
    assert root.findtext("channel/language") == "hi"
