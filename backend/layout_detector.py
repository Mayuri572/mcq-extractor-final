"""
layout_detector.py
Splits a page's OCR/PDF word coordinates into columns and lines, in
correct reading order, WITHOUT flattening to text.

This is the key structural piece for keeping question/option association
correct: callers (block_builder.py) consume columns and lines as
structured objects with real coordinates, and only convert to plain text
one line at a time, inside a bounded block-building state machine. This
guarantees that content from one column can never be interleaved with
another column's content (each column is processed start-to-finish before
the next one begins) - the class of bug where two-column pages produced
"Q1 question + Q4 options" is architecturally impossible with this
approach, rather than merely unlikely.

A plain-text flattening helper (sort_reading_order) is still provided for
callers that only need a best-effort linear transcript (e.g. language
detection, debugging) - it is NOT used for MCQ parsing on the
coordinate-bearing (image/PDF) path.
"""

from statistics import median


def _estimate_y_tolerance(blocks):
    """
    Picks a line-grouping tolerance based on the text's own scale instead
    of a fixed pixel count, so the same logic works whether coordinates
    come from a PDF's point-based layout (pdfplumber) or from OCR pixel
    boxes at any DPI (300 DPI scans have ~4x the pixel height of a PDF's
    point-based coordinates).
    """
    heights = [b["y1"] - b["y0"] for b in blocks if b["y1"] > b["y0"]]
    if not heights:
        return 12
    return max(6, median(heights) * 0.6)


def _group_lines(blocks, y_tolerance=None):
    """
    Groups nearby words into text lines (list of word-lists, each sorted
    left-to-right, lines sorted top-to-bottom).
    """
    if not blocks:
        return []

    if y_tolerance is None:
        y_tolerance = _estimate_y_tolerance(blocks)

    blocks = sorted(blocks, key=lambda b: (b["y0"], b["x0"]))

    lines = []
    current = [blocks[0]]
    # Track the running average y0 of the current line (rather than just
    # the first word's y0) so tolerance doesn't compound/drift across a
    # long line of words with slightly increasing baselines.
    current_y = blocks[0]["y0"]

    for block in blocks[1:]:
        if abs(block["y0"] - current_y) <= y_tolerance:
            current.append(block)
            current_y = sum(b["y0"] for b in current) / len(current)
        else:
            current.sort(key=lambda b: b["x0"])
            lines.append(current)
            current = [block]
            current_y = block["y0"]

    current.sort(key=lambda b: b["x0"])
    lines.append(current)

    return lines


def build_lines(words):
    """
    Groups words (already confined to a single column) into structured
    Line objects, top-to-bottom, each carrying its own bounding box and
    word list alongside its text - callers needing indentation, bounding
    boxes, etc. use these fields directly instead of re-deriving them
    from text.
    """
    lines = []
    for group in _group_lines(words):
        if not group:
            continue
        lines.append({
            "text": " ".join(w["text"] for w in group),
            "x0": min(w["x0"] for w in group),
            "y0": min(w["y0"] for w in group),
            "x1": max(w["x1"] for w in group),
            "y1": max(w["y1"] for w in group),
            "words": group,
        })
    return lines


def _lines_to_text(lines):
    return "\n".join(" ".join(word["text"] for word in line) for line in lines)


def _find_column_gutter(blocks, page_width):
    """
    Finds the x-coordinate of a genuine two-column gutter, if there is
    one - not by guessing from a fixed page-width midpoint (real gutters
    are rarely exactly centered, and a fixed midpoint split corrupts
    ordinary single-column paragraphs whose word x-centers happen to
    fall on both sides of the middle) and not from a single widest empty
    strip (a single OCR-mispositioned token, or a question number with a
    hanging indent that pokes back toward the gutter, can mask or skew
    that).

    Instead: group words into physical rows across the whole page
    (ignoring columns), and for each row that has content on both sides
    find its single widest internal gap. A real two-column layout will
    show that gap in roughly the same place on most rows; the gutter is
    the median of those positions, which is robust to the occasional
    outlier row (like a hanging-indent question number). A single-column
    page won't show a consistent gap at all - most rows either have no
    wide internal gap, or the gap wanders (it's just normal word/sentence
    spacing), so there won't be a stable majority.

    Returns the gutter's center x, or None if the page looks single-column.
    """
    if not blocks:
        return None

    # OCR occasionally emits a garbage "word" with an absurd bounding box
    # (e.g. spanning almost the whole page width). Excluded from the
    # gutter search only - it still appears in the final line/word output.
    usable = [b for b in blocks if (b["x1"] - b["x0"]) < page_width * 0.25]
    if len(usable) < 8:
        return None

    rows = _group_lines(usable)
    low, high = page_width * 0.15, page_width * 0.85

    gap_centers = []
    for row in rows:
        row_sorted = sorted(row, key=lambda b: b["x0"])
        if len(row_sorted) < 2:
            continue

        best_gap, best_center = 0, None
        for a, b in zip(row_sorted, row_sorted[1:]):
            gap = b["x0"] - a["x1"]
            if gap > best_gap:
                best_gap, best_center = gap, (a["x1"] + b["x0"]) / 2

        if best_gap > page_width * 0.02 and best_center is not None and low <= best_center <= high:
            gap_centers.append(best_center)

    # Require a real majority of rows to agree there's a gutter here,
    # not just a handful (which would just be normal sentence spacing
    # that happens to fall near the middle on a few single-column lines).
    if len(gap_centers) < max(4, len(rows) * 0.3):
        return None

    gap_centers.sort()
    return gap_centers[len(gap_centers) // 2]


def get_columns(blocks, page_width):
    """
    Splits page words into columns, in left-to-right reading order.

    Returns a list of {"gutter": x_or_None, "words": [...]}. A
    single-column page returns one entry (gutter=None) holding every
    word. A genuine two-column page returns two entries in left-to-right
    order, each holding only that column's words - words are never
    duplicated or interleaved between entries.
    """
    if not blocks:
        return []

    gutter = _find_column_gutter(blocks, page_width)

    if gutter is not None:
        left = [b for b in blocks if (b["x0"] + b["x1"]) / 2 < gutter]
        right = [b for b in blocks if (b["x0"] + b["x1"]) / 2 >= gutter]

        # Require both sides to actually hold a reasonable amount of text
        # (guards against a page that's mostly a full-width image/table
        # with just a couple of stray words on one side).
        if len(left) > len(blocks) * 0.2 and len(right) > len(blocks) * 0.2:
            return [{"gutter": gutter, "words": left}, {"gutter": gutter, "words": right}]

    return [{"gutter": None, "words": blocks}]


def sort_reading_order(blocks: list, page_width: int) -> str:
    """
    Best-effort plain-text transcript of a page, columns read in order.

    This is a convenience for callers that only need linear text (e.g.
    language detection, a debug transcript) - the MCQ-parsing path uses
    get_columns()/build_lines() directly and never calls this, precisely
    to avoid the failure mode this project was built to fix (options
    from one column/question ending up attached to another).
    """
    if not blocks:
        return ""

    columns = get_columns(blocks, page_width)
    return "\n".join(_lines_to_text(_group_lines(col["words"])) for col in columns)


# Backward compatibility
order_blocks = sort_reading_order
blocks_to_text = lambda blocks: _lines_to_text(_group_lines(blocks))
