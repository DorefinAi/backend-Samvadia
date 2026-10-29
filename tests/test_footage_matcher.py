"""The FTP footage matcher (build 1, #5): folder names as the districts really spell them, against an in-memory
tree. The integration tests run the same matcher on a real local FTP server."""
import posixpath
from datetime import datetime, timezone

import pytest

from app.services import footage
from app.services.districts import seed_districts
from app.services.footage import Role, folder_roles
from app.services.ftp import Entry
from app.services.reporter_slug import parse

DISTRICTS = {d.code: d for d in seed_districts()}


@pytest.mark.parametrize("name,roles", [
    ("2026", [Role("year", 2026)]),
    ("OCT 2026", [Role("month", 2026, 10)]), ("OCTOBER", [Role("month", None, 10)]),
    ("oct2026", [Role("month", 2026, 10)]), ("OCT_2026", [Role("month", 2026, 10)]),
    ("OCTober", [Role("month", None, 10)]), ("October_2026", [Role("month", 2026, 10)]),
    ("161026", [Role("day", 2026, 10, 16)]), ("16102026", [Role("day", 2026, 10, 16)]),
    ("161026+", [Role("day", 2026, 10, 16)]), ("22-10-26", [Role("day", 2026, 10, 22)]),
    ("19.10.2026", [Role("day", 2026, 10, 19)]), ("22_OCT", [Role("day", None, 10, 22)]),
    ("Oct _17", [Role("day", None, 10, 17), Role("month", 2017, 10)]), ("16 Oct", [Role("day", None, 10, 16)]),
    ("20-OCT", [Role("day", None, 10, 20)]), ("22", [Role("day", None, None, 22)]),
    # read both ways: the month of 2026, or 26 October
    ("OCTOBER-26", [Role("day", None, 10, 26), Role("month", 2026, 10)]),
    ("CHOMU( JAIPUR)", []), ("RENWAL", []), ("161026_AJM_HADSA_AV_SHUBHAM", []), ("octopus", []), ("45", []),
])
def test_folder_roles(name, roles):
    assert folder_roles(name) == roles


class Tree:
    """An in-memory remote: paths -> files. Directories are implied."""

    def __init__(self, paths: list[str]):
        self.dirs: dict[str, dict[str, Entry]] = {"/": {}}
        for path in paths:
            parts = path.strip("/").split("/")
            cur = ""
            for i, part in enumerate(parts):
                parent = cur or "/"
                cur = f"{cur}/{part}"
                is_file = i == len(parts) - 1 and "." in part[-5:]
                self.dirs.setdefault(parent, {})[part] = Entry(part, not is_file, 5 if is_file else 0,
                                                                datetime(2026, 10, 16, 6, 30, tzinfo=timezone.utc))
                if not is_file:
                    self.dirs.setdefault(cur, {})
        self.calls = 0

    def scan(self, path):
        self.calls += 1
        return list(self.dirs.get(posixpath.normpath(path), {}).values())


def find(tree: Tree, code: str, typed: str, received: datetime):
    p = parse(typed, None, received=received.date())
    return footage.find(footage.Lister(tree, ttl=0), DISTRICTS[code], p.slug_key, received=received,
                        parsed=p.stored(), subject=p.subject)


AT = datetime(2026, 10, 16, 6, 0, tzinfo=timezone.utc)  # 11:30 IST


def test_story_folder_in_the_day_before():
    """Ajmer filed a 16 Oct story in the 151026 folder."""
    tree = Tree(["/Ajmer/2026/OCT 2026/151026/161026_AJM_HADSA_AV_SHUBHAM/clip.mp4",
                 "/Ajmer/2026/OCT 2026/151026/161026_AJM_OTHER_AV_SHUBHAM/x.mp4",
                 "/Ajmer/2026/SEP 2026/151026/161026_AJM_HADSA_AV_SHUBHAM/wrong-month.mp4"])
    m = find(tree, "AJM", "161026_AJM_HADSA_AV_SHUBHAM", AT)
    assert m.path == "/Ajmer/2026/OCT 2026/151026/161026_AJM_HADSA_AV_SHUBHAM" and m.how == "exact"
    assert m.files == [{"name": "clip.mp4", "size": 5, "modified": "2026-10-16T06:30:00Z"}] and m.bytes == 5


@pytest.mark.parametrize("path,code,typed", [
    ("/BARAN/oct2026/161026/161026_BRN 3_HADSA_AV_SANJAY/a.mp4", "BRN", "161026_BRN 3_HADSA_AV_SANJAY"),
    ("/Barmer/OCT_2026/161026/161026_BMR_AAG_AV_ASHOK/a.mp4", "BMR", "161026_BMR_AAG_AV_ASHOK"),
    ("/Beawar/OCTOBER-26/161026/161026_BEA_MELA_AV_RAM/a.mp4", "BEA", "161026_BEA_MELA_AV_RAM"),
    ("/BIKANER/2026/OCTOBER/161026/161026_BKN_ARREST_AV-VINOD/a.mp4", "BKN", "161026_BKN_ARREST_AV-VINOD"),
    ("/CHURU/OCT/161026/161026_CHR_ACCIDENT_AV-TANUJ/a.mp4", "CHR", "161026_CHR_ACCIDENT_AV-TANUJ"),
    ("/Banswara/2026/october/161026/16102026_BNS4_Bagidora_AV_Aag_Dharmendra/a.mp4", "BSW",
     "16102026_BNS4_Bagidora_AV_Aag_Dharmendra"),
    ("/Jhalawad/OCTOBER-2026/16-10-26/161026_jhalawar_aato_av_arif/a.mp4", "JLW", "161026_jhalawar_aato_av_arif"),
    ("/NAGOUR/2026/October_2026/Oct _16/161026_Nagaur_dussehra _Av_Younus/a.mp4", "NGR",
     "161026_Nagaur_dussehra _Av_Younus दशहरा महोत्सव"),
    ("/Pratapgarh/October/16/161026_pratapgarh_collector_av_sanjay/a.mp4", "PTG", "161026_pratapgarh_collector_av_sanjay"),
    ("/DIDWANA-KUCHAMAN/OCT_2026/16_OCT/161026_DIDWANA_BARISH_AV_NARPAT/a.mp4", "DID", "161026_DIDWANA_BARISH_AV_NARPAT"),
    ("/TONK/OCTOBER 2026/161026+/161026_TONK_AAG_AV_WAJAHT/a.mp4", "TNK", "161026_TONK_AAG_AV_WAJAHT"),
    ("/JAIPUR RULAR/OCTOBER 2026/CHOMU( JAIPUR)/161026_CHOMU_PROGRAM_AV_AMIT/a.mp4", "JRL", "161026_CHOMU_PROGRAM_AV_AMIT"),
    ("/Jhunjhunu/CHIRAWA/161026/161026_JJN_MELA_AVB_HARSH/a.mp4", "JJN", "161026_JJN_MELA_AVB_HARSH"),
    ("/Kota/OCT2026/161026_KTA_STORY_IN_MONTH_AV_GUL/a.mp4", "KOT", "161026_KTA_STORY_IN_MONTH_AV_GUL"),
])
def test_each_district_layout(path, code, typed):
    m = find(Tree([path]), code, typed, AT)
    assert m is not None and m.path == posixpath.dirname(path)


def test_loose_files_named_with_the_slug():
    tree = Tree(["/ALWER/OCTOBER 2026/161026/161026_alw17_baris_av1_tapukada.mp4",
                 "/ALWER/OCTOBER 2026/161026/161026_alw17_baris_av2_tapukada.mp4",
                 "/ALWER/OCTOBER 2026/161026/161026_alw17_other_av1_tapukada.mp4"])
    m = find(tree, "ALW", "161026_alw17_baris_av1_tapukada", AT)
    assert m.how == "files" and m.path == "/ALWER/OCTOBER 2026/161026"
    assert [f["name"] for f in m.files] == ["161026_alw17_baris_av1_tapukada.mp4", "161026_alw17_baris_av2_tapukada.mp4"]


def test_prefix_both_ways():
    tree = Tree(["/Kota/OCT2026/191026/191026-kta-Shootrs-Case-overall-avbb-Gulmohd KAILASH/a.mp4"])
    m = find(tree, "KOT", "191026-kta-Shootrs-Case-overall-avbb-Gulmohd", datetime(2026, 10, 19, 6, tzinfo=timezone.utc))
    assert m.how == "prefix"


def test_fuzzy_only_when_date_reporter_and_type_agree():
    at = datetime(2026, 10, 22, 6, tzinfo=timezone.utc)
    tree = Tree(["/pali/October/221026/221026 pali, chunav bahiskaar, AV vokthru subhash/a.mp4"])
    assert find(tree, "PAL", "221026 pali, chunav bahishkar, AV vokthru subhash", at).how == "fuzzy"
    # same day, same reporter, same type, a different subject: not a match (the survey's wrong guesses)
    tree = Tree(["/Jaipur/OCTOBER/221026/221026-JPR3-TISTRE KI RAVN KI BETHK-SANGANER-AVB-DINESH/a.mp4"])
    assert find(tree, "JPR", "JPR3-22102026-JPR3-POORNIMA-SANGANER-AVB-DINESH", at) is None
    # another reporter or another type: never
    tree = Tree(["/pali/October/221026/221026 pali, chunav bahiskaar, AV vokthru ramesh/a.mp4",
                 "/pali/October/221026/221026 pali, chunav bahiskaar, PKG subhash/a.mp4"])
    assert find(tree, "PAL", "221026 pali, chunav bahishkar, AV vokthru subhash", at) is None


def test_nothing_outside_the_dates_is_opened():
    tree = Tree([f"/Jaipur/OCTOBER/{d}1026/{d}1026_JPR_X_AV_RAM/a.mp4" for d in range(10, 29)])
    find(tree, "JPR", "161026_JPR_Y_AV_RAM", AT)
    # root, month, and the day folders 15, 16, 17: not the other 16 day folders
    assert tree.calls <= 6


def test_listing_cache_is_shared_between_listers():
    tree = Tree(["/Ajmer/2026/OCT 2026/161026/161026_AJM_A_AV_X/a.mp4"])
    footage.clear_cache()
    footage.Lister(tree, ttl=60).entries("/Ajmer")
    footage.Lister(tree, ttl=60).entries("/Ajmer")
    assert tree.calls == 1
    footage.Lister(tree, ttl=0).entries("/Ajmer")  # the desk's recheck: always fresh
    assert tree.calls == 2
    footage.clear_cache()


class Item:
    """Just the fields footage.apply / start read."""

    def __init__(self, **kw):
        self.kind, self.slug_key, self.footage_status = "story", "161026ajmhadsaavshubham", "not_checked"
        self.footage_path, self.footage_files, self.footage_bytes = None, [], 0
        self.footage_checked_at = self.footage_wait_until = None
        self.received_at = AT
        self.district = type("Loc", (), {"ftp_folder": "Ajmer"})()
        self.__dict__.update(kw)


def test_status_waiting_found_and_not_found():
    item = Item()
    footage.start(item)
    assert item.footage_status == "waiting" and item.footage_wait_until == AT + footage.wait_hours()
    footage.apply(item, None, AT)
    assert item.footage_status == "waiting"
    match = footage.Match("/Ajmer/x", "exact", [{"name": "a.mp4", "size": 5, "modified": None}], 5)
    assert footage.apply(item, match, AT) and item.footage_status == "found" and item.footage_bytes == 5
    late = Item()
    footage.start(late)
    footage.apply(late, None, late.footage_wait_until)
    assert late.footage_status == "not_found"


def test_apply_drops_a_result_computed_for_another_state():
    """Review M3: a check computed before the desk picked (or changed, or cleared) a manual path must not land."""
    clip = [{"name": "a.mp4", "size": 5, "modified": None}]
    picked = Item(footage_status="manual", footage_path="/Ajmer/B", footage_files=[{"name": "b.mp4"}], footage_bytes=9)
    for stale in (footage.Match("/Ajmer/A", "exact", clip, 5),  # automatic result, desk picked B meanwhile
                  footage.Match("/Ajmer/A", "manual", clip, 5)):  # listing of the old pick A
        assert footage.apply(picked, stale, AT) is False
        assert (picked.footage_status, picked.footage_path, picked.footage_bytes) == ("manual", "/Ajmer/B", 9)
        assert picked.footage_files == [{"name": "b.mp4"}] and picked.footage_checked_at is None
    # the listing of the desk's own pick still updates its files
    assert footage.apply(picked, footage.Match("/Ajmer/B", "manual", clip, 5), AT)
    assert picked.footage_files == clip and picked.footage_bytes == 5
    # the desk cleared its pick meanwhile: the old manual listing must not become "found"
    cleared = Item()
    footage.start(cleared)
    assert footage.apply(cleared, footage.Match("/Ajmer/B", "manual", clip, 5), AT) is False
    assert cleared.footage_status == "waiting" and cleared.footage_path is None


def test_nothing_to_match_on_stays_not_checked():
    for item in (Item(kind="press_release"), Item(slug_key=None), Item(district=None)):
        footage.start(item)
        assert item.footage_status == "not_checked" and item.footage_wait_until is None


def test_dates_searched_include_neighbours():
    tree = Tree(["/Ajmer/2026/OCT 2026/171026/161026_AJM_HADSA_AV_SHUBHAM/a.mp4"])
    assert find(tree, "AJM", "161026_AJM_HADSA_AV_SHUBHAM", AT) is not None
    tree = Tree(["/Ajmer/2026/OCT 2026/181026/161026_AJM_HADSA_AV_SHUBHAM/a.mp4"])
    assert find(tree, "AJM", "161026_AJM_HADSA_AV_SHUBHAM", AT) is None
