"""The slug the reporter typed: real styles from 30 days of mail and 7 days of FTP folders (build 1, #1)."""
import random
import re
from datetime import date

import pytest

from app.services import reporter_slug as rs
from app.services.reporter_slug import folder_slug, match_key, normalise_type, parse, slug_key, strip_prefix

RECEIVED = date(2026, 10, 27)

# typed -> (date, city_token, type, reporter_name)
REAL = [
    ("161026_AJM_HADSA_AV_SHUBHAM", ("2026-10-16", "AJM", "AV", "SHUBHAM")),
    ("26.10.2026_khad vibhag news _av_pankaj", ("2026-10-26", None, "AV", "PANKAJ")),
    ("2610_JPR_MEDICAL_FOOD_KASER_PKG_GFX_VIKAS", ("2026-10-26", "JPR", "PKG", "VIKAS")),
    ("261026_Sirohi_Hadsa_Av_Ashok", ("2026-10-26", "Sirohi", "AV", "ASHOK")),
    ("161026_alw17_baris_av1_tapukada", ("2026-10-16", "alw17", "AV", "TAPUKADA")),
    ("19102026_BNS4_Bagidora_AV_Aag_Dharmendra", ("2026-10-19", "BNS4", "AV", "AAG DHARMENDRA")),
    ("161025_dausa_election_exxlusive_laxmikant_wt", ("2025-10-16", "dausa", "WT", "LAXMIKANT")),  # type last
    ("16 oct यात्रा धौलपुर", ("2026-10-16", "धौलपुर", None, None)),
    ("16,,10,,26 Kuchaman Aag se lakhon ka nuksan A V B Murari", ("2026-10-16", "Kuchaman", "AVB", "MURARI")),
    ("161026_BAL 2_DEATH_AV_BANSHI", ("2026-10-16", "BAL 2", "AV", "BANSHI")),
    ("JPR3-22102026-JPR3-POORNIMA-SANGANER-AVB-DINESH", ("2026-10-22", "JPR3", "AVB", "DINESH")),
    ("21_10_26_av_Oct_jhalawar_khanpur_ravan_dahan_av_JHL-5", ("2026-10-21", "jhalawar", "AV", None)),
    ("2110026_jayal_BETHAK_av_ngr3_mahendra_ngr", ("2026-10-21", "jayal", "AV", "MAHENDRA")),
    ("स्क्रिप्ट :-23010026_merta_PDONNTI_av_ngr8_s_puri_ngr", ("2026-10-23", "merta", "AV", "S PURI")),
    ("SCRIPT _211026_SIKRAI_NOMINATION_AV_DINESH JANGID _VISUAL,", ("2026-10-21", "SIKRAI", "AV", "DINESH JANGID")),
    ("Re: FW: 221026_BRN04_MANDI_BHAV_AV_KAMAL", ("2026-10-22", "BRN04", "AV", "KAMAL")),
    ("221026_ALW09_THANA_GHERAV_AVBB_SURESH", ("2026-10-22", "ALW09", "AVB", "SURESH")),
    ("221026_SKR15_KHATU_MELA_AVVVB_MAHESH", ("2026-10-22", "SKR15", "AVB", "MAHESH")),
    ("221026_KOTA_COACHING_PKJ_ANIL", ("2026-10-22", "KOTA", "PKG", "ANIL")),
    ("221026_JSL_SAM_DUNES_SPL_DEEPAK", ("2026-10-22", "JSL", "SPL", "DEEPAK")),
    ("221026_BKN_MLA_121_HARI", ("2026-10-22", "BKN", "121", "HARI")),
    ("221026_TONK_NAKA_PTC_WAJAHAT", ("2026-10-22", "TONK", "PTC", "WAJAHAT")),
    ("221026_JPR_BUDGET_GFX_POOJA", ("2026-10-22", "JPR", "GFX", "POOJA")),
    ("221026_JPR_MAHANGAI_VOX_POP_POOJA", ("2026-10-22", "JPR", "VOXPOP", "POOJA")),
    ("JDP_POLICE_KARYWAHI_AV_RAJIV", (None, "JDP", "AV", "RAJIV")),
    ("221026_BRN_CHUNAV_AVB_alok_badal अंता में मतदान", ("2026-10-22", "BRN", "AVB", "ALOK BADAL")),
    ("221026_UDR_RALLY_AVBBB_SUNITA", ("2026-10-22", "UDR", "AVB", "SUNITA")),
    ("221026_JPR_VIDHANSABHA_WT_POOJA", ("2026-10-22", "JPR", "WT", "POOJA")),
    ("221026_JPR_CM_BYTE_POOJA", ("2026-10-22", "JPR", "BYTE", "POOJA")),
    ("221026_JPR_MELA_LIVE_POOJA", ("2026-10-22", "JPR", "LIVE", "POOJA")),
    ("221026_KOTA_MANDI_BHAV_TEXT_ANIL", ("2026-10-22", "KOTA", "TEXT", "ANIL")),
    ("221026_AJM_DIN_BHAR_BRIEFS_SHUBHAM", ("2026-10-22", "AJM", "BRIEF", "SHUBHAM")),
    ("221026_JPR_CM_BYTE_JAIPUR_AV_POOJA", ("2026-10-22", "JPR", "AV", "POOJA")),  # BYTE in the subject
    ("221026_JPR_SABHA_AV_BYTE_POOJA", ("2026-10-22", "JPR", "AV", "POOJA")),  # BYTE as a qualifier
    ("221026_JPR_MELA_AV_LIVE_POOJA", ("2026-10-22", "JPR", "AV", "POOJA")),
]


@pytest.mark.parametrize("typed,expected", REAL, ids=[t for t, _ in REAL])
def test_real_slugs(typed, expected):
    found = parse(typed, None, received=RECEIVED)
    assert found is not None
    p = found.parsed()
    assert (p["date"], p["city_token"], p["type"], p["reporter_name"]) == expected
    assert found.source == "subject"


NOT_SLUGS = [
    "PANI SAMASYA", "झालावाड़ ब्रेकिंग", "BREAKING 03 JAISALMER", "Speed news/KTR", "Day plan",
    "Press Release: collector meeting", "प्रेस नोट", "[IMD-NewDelhi] National Bulletin No. 3",
    "Undeliverable: 271026_dausa_mp_bayan_safai_avb_laxmikant", "2026 elections results", "अजमेर में मेला",
    "1210 villages", "3 May rally in Jaipur", "12/10/2026 को जयपुर में बैठक", "", "   ", "221026",
]


@pytest.mark.parametrize("text", NOT_SLUGS)
def test_not_a_slug(text):
    assert parse(text, None, received=RECEIVED) is None


def test_subject_first_then_the_first_body_line():
    found = parse("PANI SAMASYA", "231026_ AJM _ PANI SAMASYA _ AVB _ SHUBHAM JAIN", received=RECEIVED)
    assert found.source == "body" and found.reporter_slug == "231026_ AJM _ PANI SAMASYA _ AVB _ SHUBHAM JAIN"
    assert found.parsed() == {"date": "2026-10-23", "city_token": "AJM", "type": "AVB", "reporter_name": "SHUBHAM JAIN"}
    assert parse("221026_JDP_X_AV_RAJIV", "271026_dausa_x_av_ram").source == "subject"


def test_prefix_is_removed_the_rest_kept_as_typed():
    assert parse("Fwd: Re: 271026_dausa_mp_bayan_safai_avb_laxmikant", None).reporter_slug == \
        "271026_dausa_mp_bayan_safai_avb_laxmikant"
    assert strip_prefix("RE:  FWD: x") == "x"


def test_dates_without_a_year_are_never_guessed():
    assert parse("2210_JDP_POLICE_KARYWAHI_AV_RAJIV", None).date is None
    assert parse("3012_JDP_X_AV_RAJIV", None, received=date(2027, 1, 2)).date == date(2026, 12, 30)


def test_impossible_date_is_none_but_still_a_slug():
    found = parse("320126_JDP_X_AV_RAJIV", None, received=RECEIVED)
    assert found is not None and found.date is None and found.type == "AV"


@pytest.mark.parametrize("token,expected", [("AV1", "AV"), ("AVVB", "AVB"), ("AVBB", "AVB"), ("AVVVB", "AVB"),
                                             ("avb2", "AVB"), ("PKJ", "PKG"), ("EXCLUSIVE", "SPL"), ("wt", "WT"),
                                             ("VOXPOP", "VOXPOP"), ("AVBBB", "AVB"), ("ptc", "PTC"),
                                             ("BYTE", "BYTE"), ("bytes", "BYTE"), ("Live", "LIVE"), ("TEXT", "TEXT"),
                                             ("BRIEFS", "BRIEF"), ("HADSA", None), ("", None)])
def test_type_normalised(token, expected):
    assert normalise_type(token) == expected


def test_slug_key_is_lowercase_ascii_letters_and_digits_only():
    assert slug_key("161026_AJM_HADSA_AV_SHUBHAM") == "161026ajmhadsaavshubham"
    assert re.fullmatch(r"[a-z0-9]+", slug_key("22102026_BRN_1_अंता_AVB २"))
    # a Hindi description after the slug is not part of the key, so it still equals the FTP folder name
    assert match_key("201026_Nagaur_dussehra _Av_Younus दशहरा महोत्सव") == "201026nagaurdussehraavyounus"


def test_resends_share_a_slug_key():
    a = parse("221026-JP-RAVI JAIN 181 VISIT-AV-DINESH KASANA", None)
    b = parse("Re: 221026_JP_RAVI_JAIN_181_VISIT_AV_DINESH_KASANA", None)
    assert a.slug_key == b.slug_key


@pytest.mark.parametrize("typed,folder", [
    ("161026_AJM_HADSA_AV_SHUBHAM", "161026_AJM_HADSA_AV_SHUBHAM"),
    ("221026-JP-RAVI JAIN 181 VISIT-AV-DINESH KASANA", "221026_JP_RAVI_JAIN_181_VISIT_AV_DINESH_KASANA"),
    ("16,,10,,26 Kuchaman Aag A V B Murari", "16_10_26_Kuchaman_Aag_A_V_B_Murari"),
    ("22102026_BRN_1_अंता_पंचायत_AVB_alok_badal", "22102026_BRN_1_anta_panchayat_AVB_alok_badal"),
    ("221026_BRN_CHUNAV_AVB_alok_badal अंता में मतदान", "221026_BRN_CHUNAV_AVB_alok_badal"),
    ("16 oct यात्रा धौलपुर", "16_oct_yatra_dhaulpur"),
])
def test_folder_slug_is_ascii(typed, folder):
    assert folder_slug(typed) == folder


def test_folder_slug_too_thin_is_none_and_long_is_capped():
    assert folder_slug("271026_AV") is None
    long = folder_slug("221026_" + "_".join(["ABCDEFGHI"] * 20))
    assert len(long) <= 96 and not long.endswith("_")


def test_fuzz_never_raises_and_folder_slugs_stay_ascii():
    """The trial parser raised KeyError on some inputs and let Devanagari into folder names."""
    alphabet = list("0123456789 _-,.:;/|+()AVBPKGavbpkgjanmaydecoct") + [
        "ſ", "K", "ﬀ", "İ", "दौसा", "२", "स्क्रिप्ट", ":-", "Re:", "\t", "​", " ", "अंता", "ॐ", "।"]
    rng = random.Random(7)
    for _ in range(20000):
        text = "".join(rng.choice(alphabet) for _ in range(rng.randint(0, 30)))
        found = rs._parse_one(text, "subject", date(2026, 1, 3), rs.SEED_ALIASES, rs.SEED_HINDI)
        if found:
            assert re.fullmatch(r"[a-z0-9]*", found.slug_key)
            folder = folder_slug(found.reporter_slug)
            assert folder is None or (re.fullmatch(r"[A-Za-z0-9_]+", folder) and len(folder) <= 96)


@pytest.mark.parametrize("line", ["BREAKING-JAIPUR-LIVE-UPDATE", "JPR_CM_BYTE_ON_BUDGET", "AJM_NEWS_TEXT_FOR_DESK"])
def test_weak_type_alone_does_not_make_an_undated_line_a_slug(line):
    assert parse(line, None, received=RECEIVED) is None


@pytest.mark.parametrize("line", ["2026 elections LIVE updates", "22 Oct LIVE coverage of CM rally",
                                  "1210 villages get TEXT alerts", "2026 budget BRIEF for desk",
                                  "3 May CM BYTE on budget"])
def test_weak_type_after_a_loose_or_unreadable_date_is_a_headline(line):
    assert parse(line, None, received=RECEIVED) is None


@pytest.mark.parametrize("line, kind", [("221026_JPR_CM_BYTE_POOJA", "BYTE"), ("2210_JPR_CM_LIVE_POOJA", "LIVE"),
                                        ("22 oct JPR rally LIVE pooja", None)])
def test_weak_type_still_counts_in_a_slug_built_like_one(line, kind):
    found = parse(line, None, received=RECEIVED)
    if kind is None:  # loose date, one separator run, no real type: a headline
        assert found is None
    else:
        assert found is not None and found.type == kind and found.reporter_name == "POOJA"
