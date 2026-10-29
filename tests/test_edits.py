import time

from app.services.edits import MAX_CELLS, edit_distance, headline_changed


def test_unchanged_copy_is_zero():
    assert edit_distance("झीलों के शहर में भीड़।", "झीलों के शहर में भीड़।") == 0
    assert edit_distance("", "") == 0 and edit_distance(None, None) == 0
    assert edit_distance("  same text\n", "same text") == 0  # outer spaces do not count


def test_known_distances():
    assert edit_distance("kitten", "sitting") == 3
    assert edit_distance("flaw", "lawn") == 2
    assert edit_distance("abc", "") == 3 and edit_distance("", "abcd") == 4
    assert edit_distance("उदयपुर में बारिश", "उदयपुर में भारी बारिश") == 5  # "भारी " inserted
    assert edit_distance("one two three", "one 2 three") == 3


def test_common_start_and_end_are_cut_before_comparing():
    body = "क" * 30000
    edited = body[:15000] + "ख" + body[15000:]
    started = time.perf_counter()
    assert edit_distance(body, edited) == 1
    assert time.perf_counter() - started < 1


def test_too_big_a_rewrite_is_none():
    a, b = "a" * 3000, "b" * 3000  # 9M cells, nothing in common
    assert 3000 * 3000 > MAX_CELLS and edit_distance(a, b) is None
    assert edit_distance("ab" * 10, "ba" * 10, max_cells=10) is None
    assert edit_distance("ab" * 10, "ba" * 10) == 2


def test_headline_changed():
    assert headline_changed("जयपुर में बारिश", "जयपुर में बारिश ") is False
    assert headline_changed("जयपुर में बारिश", "जयपुर में भारी बारिश") is True
    assert headline_changed(None, "") is False
