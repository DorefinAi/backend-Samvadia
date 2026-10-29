import hashlib
import hmac

import pytest

from app.services.mail import clean_body
from app.services.reporter_slug import parse
from app.services.slug import STORY_TYPES
from app.services.whatsapp import parse_webhook, verify_signature


def test_clean_body_cuts_reply_and_signature():
    body = (
        "जयपुर में आज सुबह से भारी बारिश।\r\n\r\nफोटो संलग्न हैं।\r\n\r\n"
        "Sent from my iPhone\r\n\r\nFrom: Desk <desk@example.com>\r\nOld thread"
    )
    assert clean_body(body) == "जयपुर में आज सुबह से भारी बारिश।\n\nफोटो संलग्न हैं।"


def test_clean_body_drops_quoted_lines_and_disclaimer():
    body = "Press note attached.\n> earlier text\n\nDISCLAIMER: This message is confidential."
    assert clean_body(body) == "Press note attached."


def test_clean_body_keeps_text_when_marker_is_first():
    assert clean_body("From: PIB Jaipur\nCM visits Udaipur today.") == "From: PIB Jaipur\nCM visits Udaipur today."


def test_signature():
    body = b'{"object":"whatsapp_business_account"}'
    good = "sha256=" + hmac.new(b"secret", body, hashlib.sha256).hexdigest()
    assert verify_signature("secret", body, good)
    assert not verify_signature("secret", body, "sha256=00")
    assert not verify_signature("", body, good)


def test_parse_webhook_text_and_image():
    payload = {"entry": [{"changes": [{"value": {
        "contacts": [{"wa_id": "919876543210", "profile": {"name": "Sunita"}}],
        "messages": [
            {"id": "wamid.1", "from": "919876543210", "timestamp": "1792130000", "type": "text",
             "text": {"body": "उदयपुर में पर्यटन सीजन शुरू"}},
            {"id": "wamid.2", "from": "919876543210", "timestamp": "1792130010", "type": "image",
             "image": {"id": "MEDIA1", "mime_type": "image/jpeg", "caption": "झील"}},
            {"id": "wamid.3", "from": "919876543210", "timestamp": "1792130020", "type": "sticker",
             "sticker": {"id": "S1"}},
        ],
    }}]}]}
    msgs = parse_webhook(payload)
    assert [m.id for m in msgs] == ["wamid.1", "wamid.2"]
    assert msgs[0].sender_name == "Sunita" and msgs[0].text.startswith("उदयपुर")
    assert msgs[1].media_id == "MEDIA1" and msgs[1].mime_type == "image/jpeg" and msgs[1].text == "झील"


# intake.py sets item.story_type from the typed slug only when the parsed type is one of STORY_TYPES
@pytest.mark.parametrize("typed,story_type", [
    ("161026_AJM_HADSA_AV_SHUBHAM", "AV"),
    ("221026_UDR_RALLY_AVBBB_SUNITA", "AVB"),
    ("16,,10,,26 Kuchaman Aag A V B Murari", "AVB"),
    ("221026_KOTA_COACHING_PKJ_ANIL", "PKG"),
    ("161025_dausa_election_exxlusive_laxmikant_wt", "WT"),
    ("221026_TONK_NAKA_PTC_WAJAHAT", "PTC"),
    ("221026_JPR_CM_BYTE_POOJA", "BYTE"),
    ("221026_JPR_MELA_LIVE_POOJA", "LIVE"),
    ("221026_KOTA_MANDI_BHAV_TEXT_ANIL", "TEXT"),
    ("221026_AJM_DIN_BHAR_BRIEF_SHUBHAM", "BRIEF"),
    ("221026_JSL_SAM_DUNES_SPL_DEEPAK", None),  # parsed, but not a story type: left for the desk
    ("221026_BKN_MLA_121_HARI", None),
    ("221026_JPR_MAHANGAI_VOX_POP_POOJA", None),
])
def test_typed_slug_gives_story_type(typed, story_type):
    parsed = parse(typed, None)
    assert parsed is not None
    assert (parsed.type if parsed.type in STORY_TYPES else None) == story_type
