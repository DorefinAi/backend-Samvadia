from datetime import datetime, timezone

import pytest

from app.services.slug import SlugError, build_slug, slug_error, subject_words, unique_slug
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


@pytest.mark.parametrize("hindi,latin", [
    ("पर्यटन", "paryatan"), ("जयपुर", "jaypur"), ("बारिश", "barish"), ("राजस्थान", "rajasthan"),
    ("कमला", "kamla"), ("भारी", "bhari"), ("क्षेत्र", "kshetr"), ("ज़िला", "zila"),
])
def test_transliteration(hindi, latin):
    assert transliterate_word(hindi) == latin
