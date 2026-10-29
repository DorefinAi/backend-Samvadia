"""Build 1 end to end on the local Postgres and FTP: reporter slugs, districts, kinds, the fetch window, the FTP
footage matcher on a copy of the real newsroom tree, the desk's footage endpoints and browse, reporters with
several emails. Senders and addresses are made up."""
import argparse
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from app.db import session_scope
from app.jobs import enqueue, now
from app.models import KV, Audit, Item, Message, Reporter

HADSA = "161026_AJM_HADSA_AV_SHUBHAM"
HADSA_PATH = "/Ajmer/2026/OCT 2026/151026/161026_AJM_HADSA_AV_SHUBHAM"


def mail(mid, subject, body="खबर का पाठ।", sender="stringer@example.com", name="A Stringer",
         received="2026-10-16T06:00:00Z", attachments=False, conversation=None):
    return {"id": mid, "subject": subject, "conversationId": conversation or f"CONV-{mid}",
            "hasAttachments": attachments, "receivedDateTime": received,
            "from": {"emailAddress": {"name": name, "address": sender}}, "body": {"content": body}}


def poll(drain):
    with session_scope() as db:
        enqueue(db, "fetch_mail", {"manual": True}, dedupe_key="fetch_mail")
    drain()


def scan(drain):
    with session_scope() as db:
        enqueue(db, "footage_scan", {}, dedupe_key="footage_scan")
    drain()


def only_item() -> Item:
    with session_scope() as db:
        row = db.scalars(select(Item)).one()
        _ = row.district, row.reporter
        db.expunge_all()
        return row


def all_kinds(desk, **params):
    return desk.get("/desk/items", params={"kind": "all", **params}).json()


# --- #1 reporter slug, #3 district, #8 kind ------------------------------------------------------------------

def test_unknown_sender_with_a_slug_goes_to_ftp_and_finds_its_footage(stack, drain, desk, newsroom_ftp):
    stack.mail_batch.append(mail("M1", HADSA, attachments=True))
    stack.mail_attachments["M1"] = [{"@odata.type": "#microsoft.graph.fileAttachment", "id": "A1",
                                     "name": "clip.mp4", "contentType": "video/mp4", "size": 3, "isInline": False}]
    poll(drain)
    row = only_item()
    assert row.reporter_slug == HADSA and row.slug_key == "161026ajmhadsaavshubham"
    assert row.parsed["date"] == "2026-10-16" and row.parsed["city_token"] == "AJM"
    assert row.district_code == "AJM" and row.kind == "story"
    # no reporter needed: the folder comes from the reporter's own slug
    assert row.slug == HADSA and row.intake_state == "on_ftp" and stack.ftp_folder(HADSA).is_dir()
    assert row.footage_status == "waiting"

    scan(drain)
    out = desk.get(f"/desk/items/{row.id}").json()["item"]
    assert out["district"] == {"code": "AJM", "name": "Ajmer", "name_hi": "अजमेर"}
    assert out["parsed"] == {"date": "2026-10-16", "city_token": "AJM", "type": "AV", "reporter_name": "SHUBHAM"}
    f = out["footage"]
    assert f["status"] == "found" and f["path"] == HADSA_PATH  # filed in the day-before folder (151026)
    assert f["files"] and f["bytes"] == sum(x["size"] for x in f["files"]) and f["checked_at"]
    assert f["wait_until"] == "2026-10-16T10:00:00Z"
    # the newsroom tree was only read
    assert (stack.ftp_root / HADSA_PATH.strip("/")).is_dir()


def test_resent_slug_gets_a_suffix_and_hindi_never_reaches_the_folder(stack, drain, item):
    stack.mail_batch += [mail("M1", HADSA), mail("M2", HADSA.lower()),
                         mail("M3", "22102026_BRN_1_अंता_पंचायत_AVB_alok_badal")]
    poll(drain)
    with session_scope() as db:
        slugs = sorted(i.slug for i in db.scalars(select(Item)))
    assert slugs == ["161026_AJM_HADSA_AV_SHUBHAM", "161026_ajm_hadsa_av_shubham_2",
                     "22102026_BRN_1_anta_panchayat_AVB_alok_badal"]
    assert all(s.isascii() for s in slugs)


def test_kinds_counts_and_filters(stack, drain, desk, make_reporter):
    make_reporter(email="known@example.com", phone=None, slug_code="RAM", city_code="AJM")
    stack.mail_batch += [
        mail("S1", HADSA, received="2026-10-16T06:00:00Z"),
        mail("S2", "चंडीगढ़", sender="known@example.com", received="2026-10-16T07:00:00Z"),  # known, no slug
        mail("P1", "Press Release | Store opens", sender="pr@agency.example", received="2026-10-16T08:00:00Z"),
        mail("D1", "221026_DAY PLAN", sender="known@example.com", received="2026-10-22T03:00:00Z"),
        mail("N1", "Undeliverable: 221026_DAY PLAN", sender="postmaster@example.com", name="Microsoft Outlook",
             received="2026-10-22T03:01:00Z"),
        mail("N2", "[IMD-NewDelhi] SPECIAL MESSAGE NO. 1", sender="cwd@example.gov.in",
             received="2026-10-22T03:02:00Z"),
        mail("N3", "Weekly listings", sender="news@brand.example", received="2026-10-22T03:03:00Z"),
    ]
    poll(drain)
    body = desk.get("/desk/items").json()  # default: stories only
    assert sorted(i["kind"] for i in body["items"]) == ["story", "story"]
    assert body["kind_counts"] == {"story": 2, "press_release": 1, "day_plan": 1, "not_a_story": 3}
    assert body["counts"] == {"pending": 2, "approved": 0, "rejected": 0, "held": 0, "sent_back": 0, "duplicate": 0,
                              "all": 2}
    assert len(all_kinds(desk)["items"]) == 7
    assert len(desk.get("/desk/items", params={"kind": "press_release,day_plan"}).json()["items"]) == 2
    # from/to with an India offset: [16 Oct 00:00 IST, 17 Oct 00:00 IST)
    day = desk.get("/desk/items", params={"kind": "all", "from": "2026-10-16T00:00:00+05:30",
                                          "to": "2026-10-17T00:00:00+05:30"}).json()
    assert len(day["items"]) == 3
    assert day["kind_counts"] == {"story": 2, "press_release": 1, "day_plan": 0, "not_a_story": 0}
    ajm = desk.get("/desk/items", params={"kind": "story", "district": "AJM"}).json()
    assert len(ajm["items"]) == 2 and ajm["counts"]["all"] == 2  # S2 through the reporter's city
    assert desk.get("/desk/items", params={"footage": "waiting"}).json()["items"][0]["slug"] == HADSA
    assert desk.get("/desk/items", params={"kind": "nope"}).status_code == 422
    assert desk.get("/desk/items", params={"from": "2026-10-16T00:00:00"}).status_code == 422  # needs an offset


def test_picking_a_reporter_turns_not_a_story_into_a_story(stack, drain, desk, make_reporter):
    reporter_id = make_reporter(phone=None, slug_code="NEW", city_code="KOT")
    stack.mail_batch.append(mail("N1", "एक खबर", sender="new.stringer@example.com"))
    poll(drain)
    row = all_kinds(desk)["items"][0]
    assert row["kind"] == "not_a_story" and row["district"] is None
    r = desk.patch(f"/desk/items/{row['id']}", json={"version": row["version"], "reporter_id": str(reporter_id)})
    assert r.status_code == 200, r.text
    assert r.json()["item"]["kind"] == "story" and r.json()["item"]["district"]["code"] == "KOT"


# --- #11 fetch window ----------------------------------------------------------------------------------------

def test_first_sync_never_imports_the_old_inbox(stack, drain, monkeypatch):
    from app.config import get_settings
    monkeypatch.setenv("MAIL_FETCH_SINCE", "")
    get_settings.cache_clear()
    try:
        fresh = (now() + timedelta(minutes=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
        stack.mail_batch += [mail("OLD", HADSA, received="2020-01-01T00:00:00Z"), mail("NEW", HADSA, received=fresh)]
        poll(drain)
        with session_scope() as db:
            assert [m.external_id for m in db.scalars(select(Message))] == ["NEW"]
            first_run = db.get(KV, "mail_fetch_since").value["at"]
        poll(drain)  # the stored time is kept
        with session_scope() as db:
            assert db.get(KV, "mail_fetch_since").value["at"] == first_run
    finally:
        get_settings.cache_clear()


def test_mail_fetch_since_setting_wins(stack, drain, monkeypatch):
    from app.config import get_settings
    monkeypatch.setenv("MAIL_FETCH_SINCE", "2026-10-16T12:00:00+05:30")
    get_settings.cache_clear()
    try:
        stack.mail_batch += [mail("A", HADSA, received="2026-10-16T06:29:59Z"),
                             mail("B", HADSA, received="2026-10-16T06:30:00Z")]
        poll(drain)
        with session_scope() as db:
            assert [m.external_id for m in db.scalars(select(Message))] == ["B"]
    finally:
        get_settings.cache_clear()


# --- #5 / #6 footage ---------------------------------------------------------------------------------------------

def test_loose_files_in_the_day_folder(stack, drain, desk, newsroom_ftp):
    stack.mail_batch.append(mail("A1", "161026_alw17_baris_av1_tapukada"))
    poll(drain)
    scan(drain)
    f = all_kinds(desk)["items"][0]["footage"]
    assert f["status"] == "found" and f["path"] == "/ALWER/OCTOBER 2026/161026"
    assert [x["name"] for x in f["files"]] == ["161026_alw17_baris_av1_tapukada.mp4"]


def test_waiting_then_not_found_then_recheck_reopens(stack, drain, desk, newsroom_ftp):
    stack.mail_batch.append(mail("W1", "161026_AJM_NOWHERE_AV_SHUBHAM"))
    poll(drain)
    scan(drain)  # received 16 Oct: the 4 hours are long over
    item = all_kinds(desk)["items"][0]
    assert item["footage"]["status"] == "not_found" and item["footage"]["path"] is None
    # the reporter uploads late; the desk rechecks
    newsroom_ftp("/Ajmer/2026/OCT 2026/161026/161026_AJM_NOWHERE_AV_SHUBHAM", ["late.mp4"])
    r = desk.post(f"/desk/items/{item['id']}/footage/recheck")
    assert r.status_code == 200, r.text
    f = r.json()["item"]["footage"]
    assert f["status"] == "found" and f["files"][0]["name"] == "late.mp4"
    assert datetime.fromisoformat(f["wait_until"].replace("Z", "+00:00")) > now() + timedelta(hours=3)


def test_found_folder_is_relisted_until_wait_until(stack, drain, desk, newsroom_ftp):
    received = (now() - timedelta(minutes=30)).strftime("%Y-%m-%dT%H:%M:%SZ")
    today = now().astimezone(timezone(timedelta(hours=5, minutes=30)))
    slug = f"{today:%d%m%y}_AJM_LIVE_AV_SHUBHAM"
    folder = f"/Ajmer/{today:%Y}/{today:%b %Y}/{today:%d%m%y}/{slug}"
    newsroom_ftp(folder, ["one.mp4"])
    stack.mail_batch.append(mail("L1", slug, received=received))
    poll(drain)
    scan(drain)
    item = all_kinds(desk)["items"][0]
    assert item["footage"]["status"] == "found" and len(item["footage"]["files"]) == 1
    newsroom_ftp(folder, ["two.mp4"])
    with session_scope() as db:  # pretend the last check was a while ago
        db.get(Item, item["id"]).footage_checked_at = now() - timedelta(minutes=10)
    scan(drain)
    assert len(all_kinds(desk)["items"][0]["footage"]["files"]) == 2


def test_recheck_needs_something_to_match_on(stack, drain, desk):
    stack.mail_batch.append(mail("P1", "Press Release | x", sender="pr@agency.example"))
    poll(drain)
    item = all_kinds(desk)["items"][0]
    assert item["footage"]["status"] == "not_checked"
    assert desk.post(f"/desk/items/{item['id']}/footage/recheck").status_code == 409


def test_desk_sets_footage_by_hand(stack, drain, desk, newsroom_ftp):
    stack.mail_batch.append(mail("H1", "161026_AJM_ELSEWHERE_AV_SHUBHAM"))
    poll(drain)
    item = all_kinds(desk)["items"][0]
    url = f"/desk/items/{item['id']}/footage"
    assert desk.put(url, json={"path": HADSA_PATH, "version": item["version"] + 5}).status_code == 409
    assert desk.put(url, json={"path": "/Vartamaan_DND/x", "version": item["version"]}).status_code == 422
    assert desk.put(url, json={"path": "/Ajmer/2026/NOPE", "version": item["version"]}).status_code == 422
    assert desk.put(url, json={"path": "/Ajmer/../Jaipur", "version": item["version"]}).status_code == 422
    r = desk.put(url, json={"path": HADSA_PATH, "version": item["version"]})
    assert r.status_code == 200, r.text
    out = r.json()["item"]
    assert out["footage"]["status"] == "manual" and out["footage"]["path"] == HADSA_PATH and out["footage"]["files"]
    with session_scope() as db:
        audit = db.scalars(select(Audit).where(Audit.action == "item.footage")).one()
        assert audit.after["footage"]["path"] == HADSA_PATH and audit.before["footage"]["status"] == "waiting"
    scan(drain)  # the periodic scan leaves a manual pick alone
    assert all_kinds(desk)["items"][0]["footage"]["status"] == "manual"
    r = desk.put(url, json={"path": None, "version": out["version"]})
    assert r.status_code == 200 and r.json()["item"]["footage"]["status"] == "waiting"
    assert r.json()["item"]["footage"]["path"] is None


def test_browse_is_read_only_and_fenced(desk, newsroom_ftp, make_user, login):
    root = desk.get("/desk/ftp/browse", params={"path": "/"}).json()
    names = [e["name"] for e in root["entries"]]
    assert "Ajmer" in names and "ALWER" in names and "Vartamaan_DND" in names
    assert "SP and Collector Phone_O  list" not in names and all(e["dir"] for e in root["entries"])
    ajm = desk.get("/desk/ftp/browse", params={"path": "/Ajmer/2026/OCT 2026/151026"}).json()
    assert ajm["path"] == "/Ajmer/2026/OCT 2026/151026"
    assert {"name", "dir", "size", "modified"} <= set(ajm["entries"][0])
    files = desk.get("/desk/ftp/browse", params={"path": HADSA_PATH}).json()["entries"]
    assert files and not files[0]["dir"] and files[0]["size"] == 5 and files[0]["modified"].endswith("Z")
    assert desk.get("/desk/ftp/browse", params={"path": "/SP and Collector Phone_O  list"}).status_code == 403
    assert desk.get("/desk/ftp/browse", params={"path": "/Ajmer/../SP and Collector Phone_O  list"}).status_code == 422
    assert desk.get("/desk/ftp/browse", params={"path": "/Vartamaan_DND"}).status_code == 200
    make_user("editor", "chief_editor")
    assert login("editor").get("/desk/ftp/browse").status_code == 403  # desk.review only


def test_districts_endpoint(desk):
    rows = desk.get("/desk/districts").json()["districts"]
    by_code = {d["code"]: d for d in rows}
    assert len(rows) == 48
    assert by_code["AJM"] == {"code": "AJM", "name": "Ajmer", "name_hi": "अजमेर", "ftp_folder": "Ajmer"}
    assert by_code["JRL"]["ftp_folder"] == "JAIPUR RULAR" and by_code["ALW"]["ftp_folder"] == "ALWER"


# --- #10 reporters ---------------------------------------------------------------------------------------------

def test_reporter_with_several_emails(admin, desk, stack, drain):
    r = admin.post("/desk/reporters", json={"name": "Ram Kumar", "slug_code": "ram", "district_code": "ajm",
                                            "emails": ["Ram.One@example.com", "ram.two@example.com"],
                                            "phone": "+91 90000 00001", "unknown_field": "ignored"})
    assert r.status_code == 200, r.text
    rep = r.json()["reporter"]
    assert rep["emails"] == ["ram.one@example.com", "ram.two@example.com"] and rep["email"] == "ram.one@example.com"
    assert rep["district_code"] == "AJM" and rep["city_code"] == "AJM"
    # a mail from the second address is the reporter's: a story even without a slug
    stack.mail_batch.append(mail("R1", "बिना स्लग की खबर", sender="ram.two@example.com"))
    poll(drain)
    item = all_kinds(desk)["items"][0]
    assert item["reporter"]["id"] == rep["id"] and item["kind"] == "story" and item["district"]["code"] == "AJM"

    # PUT the way the desk saves: new and old fields together; emails[] wins
    r = admin.put(f"/desk/reporters/{rep['id']}", json={
        "name": "Ram Kumar", "slug_code": "RAM", "district_code": "AJM", "city_code": "AJM", "district": "Ajmer",
        "email": "ram.one@example.com", "emails": ["ram.one@example.com", "ram.three@example.com"], "active": True})
    assert r.status_code == 200 and r.json()["reporter"]["emails"] == ["ram.one@example.com", "ram.three@example.com"]
    # an older client sends one email: it goes first, the others stay
    r = admin.put(f"/desk/reporters/{rep['id']}", json={"name": "Ram Kumar", "slug_code": "RAM",
                                                         "email": "ram.four@example.com"})
    assert r.json()["reporter"]["emails"] == ["ram.four@example.com", "ram.one@example.com", "ram.three@example.com"]
    r = admin.patch(f"/desk/reporters/{rep['id']}", json={"active": False})
    assert r.status_code == 200 and r.json()["reporter"]["active"] is False
    assert r.json()["reporter"]["phone"] == "919000000001"  # untouched by the PATCH

    assert desk.get("/desk/reporters").json()["reporters"] == []
    listed = desk.get("/desk/reporters", params={"all": "true", "limit": 1000}).json()["reporters"]
    assert [x["id"] for x in listed] == [rep["id"]]
    assert desk.get("/desk/reporters", params={"all": "true", "q": "ram.three"}).json()["reporters"]


def test_reporter_email_conflicts_and_bad_district(admin):
    admin.post("/desk/reporters", json={"name": "A", "slug_code": "AA", "emails": ["a@example.com"]})
    r = admin.post("/desk/reporters", json={"name": "B", "slug_code": "BB", "emails": ["A@example.com"]})
    assert r.status_code == 409
    assert admin.post("/desk/reporters", json={"name": "C", "slug_code": "CC", "district_code": "XXX"}).status_code == 422
    assert admin.post("/desk/reporters", json={"name": "D", "slug_code": "DD", "emails": ["not-an-email"]}).status_code == 422


def test_import_reporters_cli(tmp_path, capsys):
    from app.cli import import_reporters
    csv_path = tmp_path / "reporters.csv"
    csv_path.write_text(
        "name,slug_code,district_code,city_code,district,phone,emails,active\n"
        "Ram Kumar,RAM,AJM,,Ajmer,919000000001,ram.one@example.com; ram.two@example.com,yes\n"
        "Sita Devi,SITA,XXX,,,,sita@example.com,no\n"
        ",NONAME,AJM,,,,,\n", encoding="utf-8")
    import_reporters(argparse.Namespace(csv=str(csv_path), dry_run=False))
    assert "added: 2, updated: 0, skipped: 1" in capsys.readouterr().out
    csv_path.write_text("name,slug_code,district_code,emails\nRam K,RAM,AJM,ram.two@example.com;ram.new@example.com\n",
                        encoding="utf-8")
    import_reporters(argparse.Namespace(csv=str(csv_path), dry_run=False))
    with session_scope() as db:
        ram = db.scalars(select(Reporter).where(Reporter.slug_code == "RAM")).one()
        assert ram.name == "Ram K" and ram.city_code == "AJM" and ram.district_code == "AJM"
        assert ram.email_list == ["ram.one@example.com", "ram.two@example.com", "ram.new@example.com"]
        sita = db.scalars(select(Reporter).where(Reporter.slug_code == "SITA")).one()
        assert sita.active is False and sita.district_code is None


# --- backfill and the migration ---------------------------------------------------------------------------

def test_backfill_items_fills_old_rows_but_never_hides_decided_ones(capsys):
    from app.cli import backfill_items
    from app.config import get_settings
    received = datetime(2026, 10, 16, 6, tzinfo=timezone.utc)
    with session_scope() as db:
        for n, (subject, state) in enumerate([(HADSA, "pending"), ("Weekly listings", "approved"),
                                               ("Weekly listings", "pending")]):
            item = Item(org_id=get_settings().org_id, source="outlook", sender_contact="x@brand.example",
                        headline=subject, body="text", received_at=received, last_message_at=received,
                        intake_state="needs_desk", review_state=state)
            db.add(item)
            db.flush()
            db.add(Message(item_id=item.id, source="outlook", external_id=f"OLD{n}", text="text",
                           raw={"subject": subject}, received_at=received))
    backfill_items(argparse.Namespace(dry_run=False))
    with session_scope() as db:
        rows = {(i.headline, i.review_state): i for i in db.scalars(select(Item))}
        assert rows[(HADSA, "pending")].reporter_slug == HADSA and rows[(HADSA, "pending")].district_code == "AJM"
        assert rows[(HADSA, "pending")].footage_status == "waiting"
        assert rows[("Weekly listings", "approved")].kind == "story"  # decided: stays on the desk
        assert rows[("Weekly listings", "pending")].kind == "not_a_story"


def test_migration_0004_downgrades_and_upgrades_with_data(filed_item):
    """0004 runs on a database that has items and reporters in it, both ways."""
    from pathlib import Path

    from alembic import command
    from alembic.config import Config

    import app.db
    filed_item()
    root = Path(__file__).resolve().parents[2]
    cfg = Config(str(root / "alembic.ini"))
    cfg.set_main_option("script_location", str(root / "alembic"))
    app.db.engine.dispose()
    command.downgrade(cfg, "0003")
    command.upgrade(cfg, "head")
    app.db.engine.dispose()
    with session_scope() as db:
        item = db.scalars(select(Item)).one()
        assert item.kind == "story" and item.footage_status == "not_checked" and item.slug
        reporter = db.scalars(select(Reporter)).one()
        assert reporter.district_code == "UDR"
