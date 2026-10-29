from datetime import datetime, timezone

import pytest

from app import models
from app.services.slug import STORY_TYPES, SlugError, build_slug, slug_error, subject_words, unique_slug
from app.services.translit import transliterate_word

OCT14_IST_MORNING = datetime(2026, 10, 14, 4, 30, tzinfo=timezone.utc)  # 10:00 IST


def test_plan_example():
    assert build_slug(OCT14_IST_MORNING, "UDR", "Paryatan season", "AV", "SUNITA") == "141026_UDR_PARYATAN_SEASON_AV_SUNITA"


def test_hindi_subject_is_transliterated():
    assert build_slug(OCT14_IST_MORNING, "UDR", "पर्यटन सीजन", "AV", "Sunita") == "141026_UDR_PARYATAN_SIJAN_AV_SUNITA"


def test_hindi_headline_drops_stopwords():
    assert subject_words("जयपुर में भारी बारिश, कई इलाकों में जलभराव") == ["JAYPUR", "BHARI", "BARISH"]


def test_date_is_india_time():
    late_utc = datetime(2026, 10, 14, 20, 0, tzinfo=timezone.utc)  # 01:30 IST on the 15th
    assert build_slug(late_utc, "JPR", "metro", "PKG", "RAMESH").startswith("151026_JPR_")


def test_subject_is_limited_to_three_words():
    assert subject_words("metro phase two work starts today") == ["METRO", "PHASE", "TWO"]


@pytest.mark.parametrize("city,reporter,kind", [("", "SUNITA", "AV"), ("UDR", "", "AV"), ("UDR", "SUNITA", "XX")])
def test_missing_parts_raise(city, reporter, kind):
    with pytest.raises(SlugError):
        build_slug(OCT14_IST_MORNING, city, "paryatan", kind, reporter)


def test_unique_slug_adds_suffix():
    base = "141026_UDR_PARYATAN_AV_SUNITA"
    assert unique_slug(base, set()) == base
    assert unique_slug(base, {base}) == base + "_2"
    assert unique_slug(base, {base, base + "_2"}) == base + "_3"


def test_slug_error():
    assert slug_error("141026_UDR_PARYATAN_SEASON_AV_SUNITA") is None
    assert slug_error("141026_UDR_PARYATAN_AV_SUNITA_2") is None
    assert slug_error("JPR_METRO_PHASE2") is not None


@pytest.mark.parametrize("tail", ["\n", "\r\n", "\n\n", " ", "\t"])
def test_slug_error_refuses_trailing_whitespace(tail):
    # review M1: SLUG_RE.match with "$" let a trailing newline through, and ftplib later refused the folder name
    assert slug_error("141026_UDR_PARYATAN_SEASON_AV_SUNITA" + tail) is not None
    assert slug_error(tail + "141026_UDR_PARYATAN_SEASON_AV_SUNITA") is not None


@pytest.mark.parametrize("hindi,latin", [
    ("पर्यटन", "paryatan"), ("जयपुर", "jaypur"), ("बारिश", "barish"), ("राजस्थान", "rajasthan"),
    ("कमला", "kamla"), ("भारी", "bhari"), ("क्षेत्र", "kshetr"), ("ज़िला", "zila"),
])
def test_transliteration(hindi, latin):
    assert transliterate_word(hindi) == latin


def test_story_types_match_the_database():
    assert STORY_TYPES == models.STORY_TYPES
    assert STORY_TYPES == ("AV", "AVB", "PKG", "SF", "WT", "PTC", "BYTE", "LIVE", "TEXT", "BRIEF")


@pytest.mark.parametrize("kind", STORY_TYPES)
def test_every_story_type_builds_a_valid_slug(kind):
    slug = build_slug(OCT14_IST_MORNING, "UDR", "Paryatan season", kind.lower(), "SUNITA")
    assert slug == f"141026_UDR_PARYATAN_SEASON_{kind}_SUNITA"
    assert slug_error(slug) is None and slug_error(slug + "_2") is None


@pytest.mark.parametrize("kind", ["SPL", "121", "GFX", "VOXPOP", "AVBB"])
def test_types_outside_the_list_are_refused(kind):
    with pytest.raises(SlugError):
        build_slug(OCT14_IST_MORNING, "UDR", "paryatan", kind, "SUNITA")
    assert slug_error(f"141026_UDR_PARYATAN_{kind}_SUNITA") is not None


def test_subject_word_that_is_a_type_still_matches():
    assert slug_error("141026_JPR_CM_BYTE_JAIPUR_AV_POOJA") is None
