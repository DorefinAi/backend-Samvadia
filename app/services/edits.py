"""How far the desk moved a story from the reporter's copy (decisions.headline_changed, body_edit_distance).
Pure functions, no dependency: a plain Levenshtein distance on characters.

Cost: the common start and end are cut off first (an edit is usually one place in the copy), and the full
table runs only on what is left. When that is more than MAX_CELLS (old length x new length, about half a
second of Python), the distance is not worked out and None is stored: a 20,000-character body rewritten all through is
"a lot", and a number from part of it would mislead the training more than a null does.
"""

MAX_CELLS = 4_000_000


def headline_changed(original: str | None, final: str | None) -> bool:
    """Leading and trailing spaces do not count as a change."""
    return (original or "").strip() != (final or "").strip()


def edit_distance(original: str | None, final: str | None, max_cells: int = MAX_CELLS) -> int | None:
    """Characters inserted, deleted or replaced to turn original into final (both stripped).
    None when the changed middle part is too big to compare (see MAX_CELLS)."""
    a, b = (original or "").strip(), (final or "").strip()
    start = 0
    for x, y in zip(a, b):
        if x != y:
            break
        start += 1
    a, b = a[start:], b[start:]
    end = 0
    for x, y in zip(reversed(a), reversed(b)):
        if x != y:
            break
        end += 1
    if end:
        a, b = a[:-end], b[:-end]
    if not a or not b:
        return len(a) + len(b)
    if len(a) < len(b):
        a, b = b, a  # the row is the shorter one
    if len(a) * len(b) > max_cells:
        return None
    previous = list(range(len(b) + 1))
    for i, x in enumerate(a, 1):
        current = [i]
        for j, y in enumerate(b, 1):
            current.append(min(previous[j] + 1, current[j - 1] + 1, previous[j - 1] + (x != y)))
        previous = current
    return previous[-1]
