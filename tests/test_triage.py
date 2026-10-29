"""Item kind (build 1, #8): rules measured on 30 days of real mail. Senders here are made up."""
import pytest

from app.services.districts import letters, match_token, seed_districts
from app.services.triage import classify

CASES = [
    # subject, first line, sender, sender name, known reporter, has slug -> kind
    ("Undeliverable: 221026_DAY PLAN", "Delivery has failed", "postmaster@example.com", "Microsoft Outlook",
     False, False, "not_a_story"),
    ("[IMD-NewDelhi] SPECIAL MESSAGE NO. 1", "Respected Sir/Madam,", "cwd@example.gov.in", "cyclone warning division",
     False, False, "not_a_story"),
    ("Your Zoom password has been reset.", "Hi", "no-reply@zoom.us", "Zoom", False, False, "not_a_story"),
    ("Weekly Listings Sports Network", "Hi,", "someone@pr-agency.example", "PR Person", False, False, "not_a_story"),
    ("221026_DAY PLAN", "", "reporter@example.com", "A Reporter", True, True, "day_plan"),
    ("Press Release | New store opens", "Hello,", "pr@agency.example", "PR", False, False, "press_release"),
    ("प्रेस नोट: रोजगार मेला", "", "pro@example.gov.in", "PRO", False, False, "press_release"),
    ("161026_AJM_HADSA_AV_SHUBHAM", "", "stranger@example.com", "Stranger", False, True, "story"),
    ("चंडीगढ़", "चंडीगढ़:", "known@example.com", "Known Reporter", True, False, "story"),
]


@pytest.mark.parametrize("subject,line,sender,name,known,slug,kind", CASES)
def test_kinds(subject, line, sender, name, known, slug, kind):
    assert classify(source="outlook", subject=subject, first_line=line, sender_address=sender, sender_name=name,
                    known_reporter=known, has_slug=slug) == kind


def test_known_reporter_is_never_not_a_story():
    # even with a notification-looking address or an auto-reply subject
    for subject, sender in [("Automatic reply: away", "reporter@example.com"), ("x", "notifications@example.com")]:
        assert classify(source="outlook", subject=subject, sender_address=sender, known_reporter=True) != "not_a_story"


def test_unknown_whatsapp_sender_is_a_story_and_portal_always_is():
    assert classify(source="whatsapp", subject=None, first_line="अजमेर में मेला", sender_address="919999000111") \
        == "story"
    assert classify(source="portal", subject=None, first_line="anything") == "story"


@pytest.mark.parametrize("token,code", [("AJM03", "AJM"), ("alw17", "ALW"), ("BAL 2", "BLT"), ("BRN04", "BRN"),
                                        ("JPR3", "JPR"), ("dausa", "DAU"), ("Sirohi", "SIR"), ("BNS4", "BSW"),
                                        ("धौलपुर", "DHO"), ("CHOMU", "JRL"), ("XYZ", None), ("", None)])
def test_city_token_to_district(token, code):
    found = match_token(token, seed_districts())
    assert (found.code if found else None) == code


def test_every_district_has_a_folder_and_unique_aliases():
    districts = seed_districts()
    assert all(d.ftp_folder for d in districts)
    seen: dict[str, str] = {}
    for d in districts:
        for alias in d.aliases:
            assert alias == letters(alias), alias
            assert seen.setdefault(alias, d.code) == d.code, f"{alias} in {seen[alias]} and {d.code}"
