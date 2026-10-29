"""Districts: the newsroom FTP has one top folder per district, and reporters write a city token in their slug
(AJM, AJM03, alw17, BAL 2, dausa, Sirohi). This module maps a token or a reporter to a district.

The districts live in the `locations` table (code, name, name_hi, aliases, ftp_folder, folder_rules), so the slug
builder, the story's city line and the FTP matcher all read the same rows. SEED is what migration 0004 loads; the
migration keeps its own copy so it never changes later.

    match_token("AJM03", districts)   -> AJM   (letters only, then code / aliases / Hindi name)
    match_token("BAL 2", districts)   -> BLT
    match_token("धौलपुर", districts)  -> DHO
"""
import re
import unicodedata
from dataclasses import dataclass, field

# code, name, name_hi, ftp_folder (the real top folder, spelling as on the server), aliases (letters only, uppercase).
# Aliases: codes and place names seen in real slugs (FTP survey 16-23 Oct, 30 days of mail); digits are ignored when
# matching, so AJM03, AJM07, BRN04, JPR3, alw17 and "BAL 2" need no entry of their own.
SEED: list[tuple[str, str, str, str | None, tuple[str, ...]]] = [
    ("JPR", "Jaipur", "जयपुर", "Jaipur", ("JP", "JPR", "JAIPUR", "SANGANER")),
    ("JDH", "Jodhpur", "जोधपुर", "JODHPUR", ("JDP", "JDR", "JDH", "JODHPUR")),
    ("KOT", "Kota", "कोटा", "Kota", ("KTA", "KOT", "KOTA", "KTR", "RAMGANJMANDI", "RAWATBHATA")),
    ("UDR", "Udaipur", "उदयपुर", "Udaipur", ("UDR", "UDP", "UDI", "UDAIPUR")),
    ("AJM", "Ajmer", "अजमेर", "Ajmer", ("AJM", "AJMER", "PUSHKAR", "KISHANGARH")),
    ("BKN", "Bikaner", "बीकानेर", "BIKANER", ("BKN", "BIKANER", "FST")),
    ("ALW", "Alwar", "अलवर", "ALWER", ("ALW", "ALWAR", "ALWER", "THANAGAZI")),
    ("BHL", "Bhilwara", "भीलवाड़ा", "Bhilwara", ("BHL", "BHW", "BHILWARA", "GULABPURA")),
    ("SKR", "Sikar", "सीकर", "Sikar", ("SKR", "SIKAR", "KHATU")),
    ("BRT", "Bharatpur", "भरतपुर", "BHARATPUR", ("BRT", "BTP", "BHARATPUR")),
    ("SGN", "Sri Ganganagar", "श्रीगंगानगर", "SRIGANGANAGAR",
     ("SGN", "SGNR", "GGN", "SRIGANGANAGAR", "GANGANAGAR", "SURATGARH", "RAISINGHNAGAR", "SRIVIJAYNAGAR")),
    ("SWM", "Sawai Madhopur", "सवाई माधोपुर", "Sawaimadhopur", ("SWM", "SAWAIMADHOPUR")),
    ("BMR", "Barmer", "बाड़मेर", "Barmer", ("BMR", "BAR", "BARMER")),
    ("DEL", "Delhi / National", "दिल्ली", "Delhi", ("DEL", "DELHI")),
    ("DAU", "Dausa", "दौसा", "Dausa", ("DAU", "DUA", "DAUSA", "SIKRAI")),
    ("BUN", "Bundi", "बूंदी", "Bundi", ("BUN", "BND", "BUNDI", "HINDOLI", "NAINWAN")),
    ("TNK", "Tonk", "टोंक", "TONK", ("TNK", "TONK")),
    ("PTG", "Pratapgarh", "प्रतापगढ़", "Pratapgarh", ("PTG", "PRATAPGARH")),
    ("DID", "Didwana-Kuchaman", "डीडवाना-कुचामन", "DIDWANA-KUCHAMAN",
     ("DID", "DIDWANA", "KUCHAMAN", "LADNUN", "MAKRANA")),
    ("NGR", "Nagaur", "नागौर", "NAGOUR", ("NGR", "NAGAUR", "NAGOUR", "MERTA", "JAYAL", "PARBATSAR")),
    ("PAL", "Pali", "पाली", "pali", ("PAL", "PALI", "SOJAT", "ROHAT", "SUMERPUR")),
    ("CHR", "Churu", "चूरू", "CHURU", ("CHR", "CHURU", "SUJANGARH", "SARDARSHAHAR")),
    ("JLO", "Jalore", "जालौर", "JALORE", ("JLO", "JALORE", "SAYLA")),
    ("JSL", "Jaisalmer", "जैसलमेर", "jaisalmer", ("JSL", "JSM", "JAISALMER")),
    ("JLW", "Jhalawar", "झालावाड़", "Jhalawad",
     ("JLW", "JHL", "JHALAWAR", "JHALAWAD", "PIRAWA", "BAKANI", "AKLERA", "KHANPUR")),
    ("BRN", "Baran", "बारां", "BARAN", ("BRN", "BARAN", "KELWARA", "SHAHBAD")),
    ("BSW", "Banswara", "बांसवाड़ा", "Banswara", ("BSW", "BNS", "BANSWARA", "GHATOL", "BAGIDORA")),
    ("DNP", "Dungarpur", "डूंगरपुर", "DUNGARPUR", ("DNP", "DUG", "DUNG", "DUNGARPUR", "SAGWARA")),
    ("CHI", "Chittorgarh", "चित्तौड़गढ़", "CHITORGARH", ("CHI", "CHITORGARH", "CHITTORGARH", "BEGUN", "NIMBAHERA")),
    ("RJS", "Rajsamand", "राजसमंद", "Rajsamand", ("RJS", "RSM", "RAJ", "RAJSAMAND")),
    ("HMH", "Hanumangarh", "हनुमानगढ़", "HAUMANGARH", ("HMH", "HUN", "HANUMANGARH")),
    ("JJN", "Jhunjhunu", "झुंझुनूं", "Jhunjhunu", ("JJN", "JHUNJHUNU", "CHIRAWA", "SINGHANA", "UDAIPURWATI")),
    ("KRL", "Karauli", "करौली", "KARAULI", ("KRL", "KRO", "KARAULI")),
    ("DHO", "Dholpur", "धौलपुर", "dholpur", ("DHO", "DHL", "DHOLPUR")),
    ("DEG", "Deeg", "डीग", "DEEG", ("DEG", "DEEG")),
    ("BEA", "Beawar", "ब्यावर", "Beawar", ("BEA", "BEAWAR")),
    ("KPB", "Kotputli-Behror", "कोटपूतली-बहरोड़", "KOTPUTLI - BEHROR", ("KPB", "KOTPUTLI", "BEHROR")),
    ("KTJ", "Khairthal-Tijara", "खैरथल-तिजारा", "Khairthal-Tijara", ("KTJ", "KHAIRTHAL", "TIJARA", "KOTKASIM")),
    ("NMK", "Neem Ka Thana", "नीम का थाना", "Neemkathana", ("NMK", "NEEMKATHANA")),
    ("SLB", "Salumbar", "सलूंबर", "Salumbar", ("SLB", "SALUMBAR")),
    ("SIR", "Sirohi", "सिरोही", "SIROHI", ("SIR", "SIROHI", "ABURAJ", "ABUROAD")),
    ("BLT", "Balotra", "बालोतरा", "balotra", ("BLT", "BAL", "BALOTRA", "SAMDARI")),
    ("DUD", "Dudu", "दूदू", "Dudu(Jaipur Rural)", ("DUD", "DUDU")),
    ("JRL", "Jaipur Rural", "जयपुर ग्रामीण", "JAIPUR RULAR",
     ("JRL", "CHOMU", "RENWAL", "KALADERA", "PAOTA", "SHAHPURA")),
    ("HR", "Haryana", "हरियाणा", "HARYANA", ("HR", "HARYANA", "CHD")),
    ("MP", "Madhya Pradesh", "मध्य प्रदेश", "MADHYA PRADESH", ("MP",)),
    ("UP", "Uttar Pradesh", "उत्तर प्रदेश", "UP", ("UP",)),
    ("UK", "Uttarakhand", "उत्तराखंड", "UTTARAKHAND", ("UK",)),
]

# Short codes that are also everyday words or story types: only trusted as the slug's city token, i.e. in the
# position right after the date, never when found further along.
WEAK_ALIASES = frozenset({"DID", "SIR", "BAR", "RAJ", "PAL", "BUN", "BEA", "CHI", "DEL", "DAU", "MP", "UP", "UK",
                          "HR", "JP", "BAL", "KOT", "DUA", "FST"})


@dataclass(frozen=True)
class District:
    code: str
    name: str
    name_hi: str | None = None
    aliases: tuple[str, ...] = ()
    ftp_folder: str | None = None
    folder_rules: dict = field(default_factory=dict, compare=False, hash=False)


def seed_districts() -> list[District]:
    return [District(code, name, hi, aliases, folder) for code, name, hi, folder, aliases in SEED]


def letters(token: str | None) -> str:
    """AJM03 -> AJM, "BAL 2" -> BAL, alw17 -> ALW: ASCII letters only, uppercase."""
    return re.sub(r"[^A-Za-z]", "", token or "").upper()


def _nfc(text: str | None) -> str:
    return unicodedata.normalize("NFC", text or "").strip()


def match_token(token: str | None, districts: list[District]) -> District | None:
    """The city token as typed -> district: by code, alias or the Hindi name (exact word)."""
    key = letters(token)
    if key:
        for d in districts:
            if key == d.code.upper() or key in d.aliases:
                return d
        return None
    hindi = _nfc(token)
    if hindi:
        for d in districts:
            names = {_nfc(d.name_hi), *(_nfc(p) for p in (d.name_hi or "").split("-"))} - {""}
            if hindi in names:
                return d
    return None


def all_aliases(districts: list[District]) -> frozenset[str]:
    return frozenset(a for d in districts for a in (*d.aliases, d.code.upper()))


def hindi_names(districts: list[District]) -> frozenset[str]:
    return frozenset(n for d in districts for n in {_nfc(d.name_hi), *(_nfc(p) for p in (d.name_hi or "").split("-"))}
                     if n)


SEED_ALIASES = all_aliases(seed_districts())
SEED_HINDI = hindi_names(seed_districts())


def load(db) -> list[District]:
    """Active districts from the locations table."""
    from sqlalchemy import select

    from app.models import Location

    rows = db.scalars(select(Location).where(Location.active).order_by(Location.code)).all()
    return [District(r.code, r.name, r.name_hi, tuple(a.upper() for a in (r.aliases or ())), r.ftp_folder,
                     dict(r.folder_rules or {})) for r in rows]
