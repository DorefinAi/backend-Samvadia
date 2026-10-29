"""build 1: districts (locations get aliases, the newsroom FTP folder and matcher overrides, and grow from 14 to
48 rows), the slug the reporter typed, item kind, FTP footage status, several emails per reporter

Runs on existing data, nothing is dropped on upgrade:
- the 14 existing locations keep their codes and names; aliases, ftp_folder and a Hindi name (where missing) are set
- every new item column is NULL or has a default: kind 'story' (so nothing already on the desk disappears from the
  default view, decided items included), footage_status 'not_checked'
- reporters.city_code becomes nullable; reporters.district_code is set from city_code where that is a location
- reporters.email is copied into the new reporter_emails table (the column stays, as the first address)
Older items get reporter_slug / district / kind with `python -m app.cli backfill-items` (optional).

Downgrade drops the new columns and table (their values are lost: reporter_slug, parsed, kind, footage, extra
emails) and the 34 added locations unless something still points at them.

Revision ID: 0004
Revises: 0003
Create Date: 2026-10-27
"""

import sqlalchemy as sa
from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None

ORIGINAL = ("JPR", "JDH", "KOT", "UDR", "AJM", "BKN", "ALW", "BHL", "SKR", "BRT", "SGN", "SWM", "BMR", "DEL")

# A frozen copy of app/services/districts.py SEED: code, name, name_hi, ftp_folder, aliases.
DISTRICTS = [
    ("JPR", "Jaipur", "जयपुर", "Jaipur", ["JP", "JPR", "JAIPUR", "SANGANER"]),
    ("JDH", "Jodhpur", "जोधपुर", "JODHPUR", ["JDP", "JDR", "JDH", "JODHPUR"]),
    ("KOT", "Kota", "कोटा", "Kota", ["KTA", "KOT", "KOTA", "KTR", "RAMGANJMANDI", "RAWATBHATA"]),
    ("UDR", "Udaipur", "उदयपुर", "Udaipur", ["UDR", "UDP", "UDI", "UDAIPUR"]),
    ("AJM", "Ajmer", "अजमेर", "Ajmer", ["AJM", "AJMER", "PUSHKAR", "KISHANGARH"]),
    ("BKN", "Bikaner", "बीकानेर", "BIKANER", ["BKN", "BIKANER", "FST"]),
    ("ALW", "Alwar", "अलवर", "ALWER", ["ALW", "ALWAR", "ALWER", "THANAGAZI"]),
    ("BHL", "Bhilwara", "भीलवाड़ा", "Bhilwara", ["BHL", "BHW", "BHILWARA", "GULABPURA"]),
    ("SKR", "Sikar", "सीकर", "Sikar", ["SKR", "SIKAR", "KHATU"]),
    ("BRT", "Bharatpur", "भरतपुर", "BHARATPUR", ["BRT", "BTP", "BHARATPUR"]),
    ("SGN", "Sri Ganganagar", "श्रीगंगानगर", "SRIGANGANAGAR",
     ["SGN", "SGNR", "GGN", "SRIGANGANAGAR", "GANGANAGAR", "SURATGARH", "RAISINGHNAGAR", "SRIVIJAYNAGAR"]),
    ("SWM", "Sawai Madhopur", "सवाई माधोपुर", "Sawaimadhopur", ["SWM", "SAWAIMADHOPUR"]),
    ("BMR", "Barmer", "बाड़मेर", "Barmer", ["BMR", "BAR", "BARMER"]),
    ("DEL", "Delhi / National", "दिल्ली", "Delhi", ["DEL", "DELHI"]),
    ("DAU", "Dausa", "दौसा", "Dausa", ["DAU", "DUA", "DAUSA", "SIKRAI"]),
    ("BUN", "Bundi", "बूंदी", "Bundi", ["BUN", "BND", "BUNDI", "HINDOLI", "NAINWAN"]),
    ("TNK", "Tonk", "टोंक", "TONK", ["TNK", "TONK"]),
    ("PTG", "Pratapgarh", "प्रतापगढ़", "Pratapgarh", ["PTG", "PRATAPGARH"]),
    ("DID", "Didwana-Kuchaman", "डीडवाना-कुचामन", "DIDWANA-KUCHAMAN", ["DID", "DIDWANA", "KUCHAMAN", "LADNUN", "MAKRANA"]),
    ("NGR", "Nagaur", "नागौर", "NAGOUR", ["NGR", "NAGAUR", "NAGOUR", "MERTA", "JAYAL", "PARBATSAR"]),
    ("PAL", "Pali", "पाली", "pali", ["PAL", "PALI", "SOJAT", "ROHAT", "SUMERPUR"]),
    ("CHR", "Churu", "चूरू", "CHURU", ["CHR", "CHURU", "SUJANGARH", "SARDARSHAHAR"]),
    ("JLO", "Jalore", "जालौर", "JALORE", ["JLO", "JALORE", "SAYLA"]),
    ("JSL", "Jaisalmer", "जैसलमेर", "jaisalmer", ["JSL", "JSM", "JAISALMER"]),
    ("JLW", "Jhalawar", "झालावाड़", "Jhalawad", ["JLW", "JHL", "JHALAWAR", "JHALAWAD", "PIRAWA", "BAKANI", "AKLERA", "KHANPUR"]),
    ("BRN", "Baran", "बारां", "BARAN", ["BRN", "BARAN", "KELWARA", "SHAHBAD"]),
    ("BSW", "Banswara", "बांसवाड़ा", "Banswara", ["BSW", "BNS", "BANSWARA", "GHATOL", "BAGIDORA"]),
    ("DNP", "Dungarpur", "डूंगरपुर", "DUNGARPUR", ["DNP", "DUG", "DUNG", "DUNGARPUR", "SAGWARA"]),
    ("CHI", "Chittorgarh", "चित्तौड़गढ़", "CHITORGARH", ["CHI", "CHITORGARH", "CHITTORGARH", "BEGUN", "NIMBAHERA"]),
    ("RJS", "Rajsamand", "राजसमंद", "Rajsamand", ["RJS", "RSM", "RAJ", "RAJSAMAND"]),
    ("HMH", "Hanumangarh", "हनुमानगढ़", "HAUMANGARH", ["HMH", "HUN", "HANUMANGARH"]),
    ("JJN", "Jhunjhunu", "झुंझुनूं", "Jhunjhunu", ["JJN", "JHUNJHUNU", "CHIRAWA", "SINGHANA", "UDAIPURWATI"]),
    ("KRL", "Karauli", "करौली", "KARAULI", ["KRL", "KRO", "KARAULI"]),
    ("DHO", "Dholpur", "धौलपुर", "dholpur", ["DHO", "DHL", "DHOLPUR"]),
    ("DEG", "Deeg", "डीग", "DEEG", ["DEG", "DEEG"]),
    ("BEA", "Beawar", "ब्यावर", "Beawar", ["BEA", "BEAWAR"]),
    ("KPB", "Kotputli-Behror", "कोटपूतली-बहरोड़", "KOTPUTLI - BEHROR", ["KPB", "KOTPUTLI", "BEHROR"]),
    ("KTJ", "Khairthal-Tijara", "खैरथल-तिजारा", "Khairthal-Tijara", ["KTJ", "KHAIRTHAL", "TIJARA", "KOTKASIM"]),
    ("NMK", "Neem Ka Thana", "नीम का थाना", "Neemkathana", ["NMK", "NEEMKATHANA"]),
    ("SLB", "Salumbar", "सलूंबर", "Salumbar", ["SLB", "SALUMBAR"]),
    ("SIR", "Sirohi", "सिरोही", "SIROHI", ["SIR", "SIROHI", "ABURAJ", "ABUROAD"]),
    ("BLT", "Balotra", "बालोतरा", "balotra", ["BLT", "BAL", "BALOTRA", "SAMDARI"]),
    ("DUD", "Dudu", "दूदू", "Dudu(Jaipur Rural)", ["DUD", "DUDU"]),
    ("JRL", "Jaipur Rural", "जयपुर ग्रामीण", "JAIPUR RULAR", ["JRL", "CHOMU", "RENWAL", "KALADERA", "PAOTA", "SHAHPURA"]),
    ("HR", "Haryana", "हरियाणा", "HARYANA", ["HR", "HARYANA", "CHD"]),
    ("MP", "Madhya Pradesh", "मध्य प्रदेश", "MADHYA PRADESH", ["MP"]),
    ("UP", "Uttar Pradesh", "उत्तर प्रदेश", "UP", ["UP"]),
    ("UK", "Uttarakhand", "उत्तराखंड", "UTTARAKHAND", ["UK"]),
]

UPGRADE = [
    # districts
    "ALTER TABLE locations ADD COLUMN aliases TEXT[] DEFAULT '{}' NOT NULL",
    "ALTER TABLE locations ADD COLUMN ftp_folder TEXT",
    "ALTER TABLE locations ADD COLUMN folder_rules JSONB",
    # reporters: several emails, a district
    "ALTER TABLE reporters ALTER COLUMN city_code DROP NOT NULL",
    "ALTER TABLE reporters ADD COLUMN district_code VARCHAR(8)",
    """CREATE TABLE reporter_emails (
        id BIGSERIAL NOT NULL,
        reporter_id UUID NOT NULL,
        email TEXT NOT NULL,
        position INTEGER DEFAULT 0 NOT NULL,
        created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
        PRIMARY KEY (id),
        UNIQUE (email),
        FOREIGN KEY(reporter_id) REFERENCES reporters (id) ON DELETE CASCADE
    )""",
    "CREATE INDEX ix_reporter_emails_reporter_id ON reporter_emails (reporter_id)",
    "INSERT INTO reporter_emails (reporter_id, email, position) "
    "SELECT id, lower(email), 0 FROM reporters WHERE email IS NOT NULL ON CONFLICT (email) DO NOTHING",
    # items: the reporter's slug, kind, district, footage
    "ALTER TABLE items ADD COLUMN reporter_slug TEXT",
    "ALTER TABLE items ADD COLUMN slug_key TEXT",
    "ALTER TABLE items ADD COLUMN parsed JSONB",
    "ALTER TABLE items ADD COLUMN kind VARCHAR(16) DEFAULT 'story' NOT NULL",
    "ALTER TABLE items ADD COLUMN district_code VARCHAR(8)",
    "ALTER TABLE items ADD COLUMN footage_status VARCHAR(16) DEFAULT 'not_checked' NOT NULL",
    "ALTER TABLE items ADD COLUMN footage_path TEXT",
    "ALTER TABLE items ADD COLUMN footage_files JSONB DEFAULT '[]'::jsonb NOT NULL",
    "ALTER TABLE items ADD COLUMN footage_bytes BIGINT DEFAULT 0 NOT NULL",
    "ALTER TABLE items ADD COLUMN footage_checked_at TIMESTAMP WITH TIME ZONE",
    "ALTER TABLE items ADD COLUMN footage_wait_until TIMESTAMP WITH TIME ZONE",
    "ALTER TABLE items ADD CONSTRAINT items_kind_check "
    "CHECK (kind IN ('story', 'press_release', 'day_plan', 'not_a_story'))",
    "ALTER TABLE items ADD CONSTRAINT items_footage_status_check "
    "CHECK (footage_status IN ('not_checked', 'waiting', 'found', 'not_found', 'manual'))",
    "CREATE INDEX items_kind_received_idx ON items (kind, received_at)",
    "CREATE INDEX items_slug_key_idx ON items (slug_key)",
    "CREATE INDEX items_footage_due_idx ON items (footage_checked_at) "
    "WHERE footage_status IN ('waiting', 'found')",
]


def upgrade() -> None:
    for statement in UPGRADE:
        op.execute(statement)
    bind = op.get_bind()
    # Existing rows keep their code and name; aliases, folder and a missing Hindi name are filled in.
    upsert = sa.text(
        "INSERT INTO locations (code, name, name_hi, ftp_folder, aliases) "
        "VALUES (:code, :name, :name_hi, :ftp_folder, :aliases) "
        "ON CONFLICT (code) DO UPDATE SET name_hi = COALESCE(locations.name_hi, EXCLUDED.name_hi), "
        "ftp_folder = EXCLUDED.ftp_folder, aliases = EXCLUDED.aliases"
    ).bindparams(sa.bindparam("aliases", type_=sa.ARRAY(sa.Text)))
    for code, name, name_hi, folder, aliases in DISTRICTS:
        bind.execute(upsert, {"code": code, "name": name, "name_hi": name_hi, "ftp_folder": folder, "aliases": aliases})
    op.execute("UPDATE reporters SET district_code = city_code WHERE city_code IN (SELECT code FROM locations)")
    # After the rows exist: both point at a location
    op.execute("ALTER TABLE items ADD CONSTRAINT items_district_code_fkey "
               "FOREIGN KEY (district_code) REFERENCES locations (code)")
    op.execute("ALTER TABLE reporters ADD CONSTRAINT reporters_district_code_fkey "
               "FOREIGN KEY (district_code) REFERENCES locations (code)")


def downgrade() -> None:
    for statement in [
        "ALTER TABLE items DROP CONSTRAINT items_district_code_fkey",
        "ALTER TABLE reporters DROP CONSTRAINT reporters_district_code_fkey",
        "DROP INDEX items_footage_due_idx", "DROP INDEX items_slug_key_idx", "DROP INDEX items_kind_received_idx",
        "ALTER TABLE items DROP CONSTRAINT items_footage_status_check",
        "ALTER TABLE items DROP CONSTRAINT items_kind_check",
        *(f"ALTER TABLE items DROP COLUMN {c}" for c in (
            "footage_wait_until", "footage_checked_at", "footage_bytes", "footage_files", "footage_path",
            "footage_status", "district_code", "kind", "parsed", "slug_key", "reporter_slug")),
        # the first address back into reporters.email where it was empty
        "UPDATE reporters r SET email = e.email FROM (SELECT DISTINCT ON (reporter_id) reporter_id, email "
        "FROM reporter_emails ORDER BY reporter_id, position, id) e "
        "WHERE r.id = e.reporter_id AND r.email IS NULL "
        "AND NOT EXISTS (SELECT 1 FROM reporters o WHERE o.email = e.email)",
        "DROP TABLE reporter_emails",
        "UPDATE reporters SET city_code = COALESCE(city_code, district_code, '')",
        "ALTER TABLE reporters DROP COLUMN district_code",
        "ALTER TABLE reporters ALTER COLUMN city_code SET NOT NULL",
    ]:
        op.execute(statement)
    op.get_bind().execute(sa.text(
        "DELETE FROM locations WHERE code <> ALL(:keep) "
        "AND code NOT IN (SELECT city_code FROM reporters) "
        "AND code NOT IN (SELECT city_code FROM items WHERE city_code IS NOT NULL)"
    ).bindparams(sa.bindparam("keep", type_=sa.ARRAY(sa.Text))), {"keep": list(ORIGINAL)})
    for statement in ["ALTER TABLE locations DROP COLUMN folder_rules", "ALTER TABLE locations DROP COLUMN ftp_folder",
                      "ALTER TABLE locations DROP COLUMN aliases"]:
        op.execute(statement)


