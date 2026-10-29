"""The email text cleaner on the shapes of real scripts (build 1, #9). Short, anonymised excerpts: names, places
and numbers are made up; no reporter addresses or phone numbers."""
from app.services.mail import clean_body

SCRIPT = (
    "हैडर: बाजार में भीड़\n"
    "एंकर इंट्रो :- शहर में त्योहार से पहले बाजारों में रौनक रही।\n"
    "वीओ- सुबह से ही लोग खरीदारी करने पहुंचे।\n"
    "नोटः खबर की फीड FTP पर भेज दी है।"
)


def test_a_plain_script_is_kept_whole():
    assert clean_body(SCRIPT) == SCRIPT


def test_note_header_and_anchor_lines_survive_a_signature():
    out = clean_body(SCRIPT + "\n\nSent from my iPhone\n\nFrom: Desk <desk@example.com>\nold thread")
    assert out == SCRIPT


def test_resend_keeps_the_quoted_script():
    """'Feed sent' above, the script itself only in the quoted original."""
    body = ("FEED SEND BY TOWN 171026 STORY AV\n\nREPORTER NAME\nTOWN (DISTRICT)\n\n"
            "On Sat, 17 Oct, 2026, 12:15 Reporter Name, <reporter@example.com> wrote:\n" + SCRIPT)
    out = clean_body(body)
    assert out.startswith("FEED SEND BY TOWN")
    for line in SCRIPT.splitlines():
        assert line in out
    assert "wrote:" not in out


def test_forward_of_own_mail_keeps_the_forwarded_script_without_headers():
    body = ("---------- Forwarded message ---------\n"
            "From: Reporter Name <reporter@example.com>\nDate: Fri, 16 Oct 2026, 3:33 pm\n"
            "Subject: 161026_TOWN_STORY_AV_NAME\nTo: <desk@example.com>\n\n" + SCRIPT)
    assert clean_body(body) == SCRIPT


def test_outlook_forward_with_only_a_sign_off_above():
    body = ("Get Outlook for iOS\n________________________________\n"
            "From: Reporter Name <reporter@example.com>\nSent: Friday, October 9, 2026 6:21 PM\n"
            "To: Desk <desk@example.com>\nSubject: चंडीगढ़\n\n" + SCRIPT)
    assert clean_body(body) == SCRIPT


def test_reply_header_on_top_is_dropped_and_the_script_kept():
    body = "On Sun, Oct 11, 2026, 2:53 PM Reporter Name <reporter@example.com> wrote:\n" + SCRIPT
    assert clean_body(body) == SCRIPT


def test_a_correction_above_a_quoted_script_keeps_only_the_correction_when_it_has_its_own_script():
    body = SCRIPT + "\n\nOn Wed, 14 Oct, 2026, 14:07 Reporter Name, <reporter@example.com> wrote:\nएंकर पुराना"
    assert clean_body(body) == SCRIPT


def test_quoted_reply_without_a_script_is_still_dropped():
    body = "सांशोधन: गांव का नाम सही करें\n\nOn Wed, 14 Oct, 2026, 14:07 Reporter, <r@example.com> wrote:\nपुराना पाठ"
    assert clean_body(body) == "सांशोधन: गांव का नाम सही करें"
