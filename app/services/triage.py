"""What kind of message an item is: story | press_release | day_plan | not_a_story. Pure functions.

Measured on 4,821 real emails (30 days). Rules, in this order:
1. A known reporter's mail is never not_a_story.
2. Delivery failures and auto-replies (Undeliverable: ..., postmaster, mailer-daemon, "Microsoft Outlook")
   -> not_a_story, even when the bounced subject says "day plan".
3. "day plan" in the subject or first line -> day_plan, for everyone.
4. "press note / press release / प्रेस नोट / प्रेस विज्ञप्ति" -> press_release, for everyone.
5. A known reporter, or a message carrying a reporter's slug -> story.
6. Notifications: "[IMD-..." bulletins, social/sign-in/file-transfer senders, no-reply addresses -> not_a_story.
7. Anything else from an unknown email sender (no slug) -> not_a_story: newsletters, offers, spam.
   An unknown WhatsApp sender stays a story: a person messaging the newsroom number is a tip, not a newsletter.
"""
import re

KINDS = ("story", "press_release", "day_plan", "not_a_story")

# (?<![a-z]) instead of \b: in "271026_DAY_PLAN" the underscore before DAY counts as a word character for \b
_DAY_PLAN = re.compile(r"(?<![a-z])day[\s_-]*plan(?![a-z])|डे\s*प्लान", re.I | re.A)
_PRESS = re.compile(r"(?<![a-z])press[\s_-]*(?:release|note|notes|statement)(?![a-z])"
                    r"|प्रेस\s*(?:नोट|विज्ञप्ति|रिलीज|रिलीज़)", re.I)
_BOUNCE_SUBJECT = re.compile(
    r"^\s*(?:undeliverable|undelivered mail|delivery status notification|delivery has failed|"
    r"mail delivery (?:failed|subsystem)|returned mail|failure notice|automatic reply|auto(?:matic)?[- ]?reply|"
    r"out of office)\b", re.I)
_BOUNCE_LOCAL = re.compile(r"^(?:mailer-daemon|postmaster)$", re.I)
_SYSTEM_NAMES = re.compile(r"^(?:microsoft outlook|mail delivery (?:system|subsystem)|postmaster|mailer-daemon)$", re.I)
_IMD_BULLETIN = re.compile(r"^\s*(?:(?:re|fw|fwd)\s*:\s*)*\[IMD-", re.I)
_NOTIFY_DOMAINS = ("zoom.us", "facebookmail.com", "linkedin.com", "linkedinmail.com", "accounts.google.com",
                   "google.com", "microsoft.com", "microsoftonline.com", "messaging.microsoft.com", "twitter.com",
                   "x.com", "youtube.com", "transfernow.net", "meta.com", "atlassian.com", "grok.com")
# The whole local part, so ravi.notifications.desk@gmail.com is not a notification
_NOTIFY_LOCAL = re.compile(r"^(?:no-?reply|do-?not-?reply|notifications?|bounces?|newsletter|news-?letter)$", re.I)


def is_bounce(subject: str, address: str, sender_name: str) -> bool:
    local = (address or "").lower().partition("@")[0]
    return bool(_BOUNCE_SUBJECT.search(subject or "") or _SYSTEM_NAMES.match((sender_name or "").strip())
                or (local and _BOUNCE_LOCAL.match(local)))


def is_notification(subject: str, address: str) -> bool:
    if _IMD_BULLETIN.search(subject or ""):
        return True
    local, _, domain = (address or "").lower().partition("@")
    if domain and (domain in _NOTIFY_DOMAINS or any(domain.endswith("." + d) for d in _NOTIFY_DOMAINS)):
        return True
    return bool(local and domain and _NOTIFY_LOCAL.match(local))


def classify(*, source: str, subject: str | None, first_line: str | None = None, sender_address: str | None = None,
             sender_name: str | None = None, known_reporter: bool = False, has_slug: bool = False) -> str:
    """source: portal | outlook | whatsapp. subject: the email subject (None for WhatsApp).
    has_slug: a reporter's slug was read from this message."""
    if source == "portal":
        return "story"  # only reporters can log in to the portal
    subject = (subject or "").strip()
    headline = f"{subject}\n{(first_line or '').strip()}"
    if not known_reporter and is_bounce(subject, sender_address or "", sender_name or ""):
        return "not_a_story"
    if _DAY_PLAN.search(headline):
        return "day_plan"
    if _PRESS.search(headline):
        return "press_release"
    if known_reporter or has_slug:
        return "story"
    if is_notification(subject, sender_address or ""):
        return "not_a_story"
    return "not_a_story" if source == "outlook" else "story"
