"""
block_builder.py
Coordinate-aware MCQ grouping for images and PDFs (both OCR'd and
digital pages) - the path that replaces "flatten to text, then parse
text" with "group words into lines within each column, then build one
MCQ block at a time from those lines," so a question and its options can
never end up attached to content from a different column or a different
question.

Guarantees this gives, by construction (not just heuristically):

- A column's lines are processed start-to-finish before the next column
  begins (get_columns() + this module's per-column loop), so an option
  from column 2 can never attach to a question from column 1, and a
  two-column page is always read "column 1 top-to-bottom, then column 2
  top-to-bottom" - never interleaved.

- Within a column, an option is only ever appended to whichever question
  block is currently open (the one most recently started in that same
  column), matching "option belongs to the nearest preceding question in
  the same column."

- If an option letter would be set twice inside one block, that block is
  closed and a new (needs_review) block started instead of overwriting
  or silently merging - a real MCQ never repeats a letter, so a repeat
  can only mean a question boundary was missed.

The state machine itself mirrors mcq_parser.parse_mcqs() (same regex
patterns, same QuestionMatcher, same option-splitting/cleanup helpers -
imported, not reimplemented) so the two paths behave consistently; the
difference is only that this one is driven by real per-line coordinates
grouped column-by-column, never a flattened cross-column text stream.
"""

from statistics import median

from backend.layout_detector import get_columns, build_lines
from backend.text_cleaner import clean_text
from backend.mcq_parser import (
    QuestionMatcher,
    line_starts_with_option,
    split_horizontal_options,
    extract_inline_options,
    new_mcq_block,
    validate_mcq,
    missing_parts,
    clean_option_value,
    UNCERTAIN_MARKER_RE,
    is_heading_or_instruction,
    _matches_heading_pattern,
)

_NEXT_LETTER = {"A": "B", "B": "C", "C": "D"}


def _char_width_for(lines):
    heights = [l["y1"] - l["y0"] for l in lines if l["y1"] > l["y0"]]
    return max(4, (median(heights) if heights else 20) * 0.55)


def _expand_bbox(block, line):
    x0, y0, x1, y1 = block["bbox"]
    block["bbox"] = [
        min(x0, line["x0"]), min(y0, line["y0"]),
        max(x1, line["x1"]), max(y1, line["y1"]),
    ]


def _new_block(page_number, line, question_number_guess, needs_review=False, reasons=None):
    return {
        "page_number": page_number,
        "question_number_guess": question_number_guess,
        "question": "",
        "options": {"A": "", "B": "", "C": "", "D": ""},
        "bbox": [line["x0"], line["y0"], line["x1"], line["y1"]],
        "needs_review": needs_review,
        "review_reasons": list(reasons or []),
    }


def _process_column_lines(lines, matcher, page_number, char_width, baseline_x0):
    """
    Processes ONE column's lines, top to bottom, into MCQ blocks. Returns
    the list of blocks found in this column, in order.
    """
    blocks = []
    current = None
    # state is either "QUESTION" or a letter "A"/"B"/"C"/"D" naming which
    # option is currently being appended to.
    state = None
    # x0 of the first option marker found in the CURRENT block - the left
    # margin the rest of that block's option list should share, used only
    # by the uncertain-marker fallback below.
    option_left_margin = None

    def finalize():
        nonlocal current
        if current is not None:
            blocks.append(current)
        current = None

    for line in lines:
        text = line["text"].strip()
        if not text:
            continue
        # Apply the same OCR-artifact normalization used on the text-only
        # path (Q4->Q.4, Qi->Q.1, (c)->C misreads, brace stripping, etc.)
        # BEFORE classification, not just to the final assembled fields -
        # otherwise a misread question marker never gets a chance to be
        # rescued in time to affect how this line is grouped.
        text = clean_text(text)
        if not text:
            continue

        # Document furniture (titles, mark-allocation lines, question-
        # range lines) must never become a question, including via a
        # coincidental bare-number match. matcher.match() has side
        # effects (advances the numbering-sequence tracker), so this is
        # checked before it runs rather than discarding the result
        # afterward. Pattern-only (not the broader keyword check further
        # below) since this runs on EVERY line, including legitimate
        # question/option continuation text.
        if _matches_heading_pattern(text):
            continue

        indent = int(round((line["x0"] - baseline_x0) / char_width))

        q_text = matcher.match(text, indent)

        if q_text is not None:
            finalize()
            q_text_stripped = q_text.strip()
            current = _new_block(page_number, line, matcher.last_number)
            option_left_margin = None

            if q_text_stripped and line_starts_with_option(q_text_stripped):
                # Question marker and its first option(s) share one
                # physical line with no question text between them.
                for letter, value in split_horizontal_options(q_text_stripped):
                    current["options"][letter] = value
                    state = letter
                    option_left_margin = line["x0"]
            else:
                # Also handle question text followed by inline options
                # further along the SAME line (e.g. "Q7. What is 5x5?
                # (A) 20 (B) 25 (C) 30 (D) 35").
                stem, inline_options = extract_inline_options(q_text_stripped)
                current["question"] = stem
                state = "QUESTION"
                for letter, value in inline_options:
                    current["options"][letter] = value
                    state = letter
                    option_left_margin = line["x0"]

            continue

        if current is None:
            # An option/continuation line with no open question block in
            # this column at all. Document furniture (titles,
            # instructions) never gets a question marker either, so it
            # lands here too - filter it out entirely rather than
            # surfacing it as a fake question.
            if is_heading_or_instruction(text):
                continue
            current = _new_block(
                page_number, line, None,
                needs_review=True,
                reasons=["no question marker detected before this content"],
            )
            state = "QUESTION"
            option_left_margin = None

        if line_starts_with_option(text):
            options_found = split_horizontal_options(text)
            if options_found:
                for letter, value in options_found:
                    if current["options"][letter]:
                        # Option letter repeated inside one block: a real
                        # MCQ never repeats a letter, so this can only
                        # mean a question boundary was missed. Close the
                        # current block and start a new one rather than
                        # overwriting/corrupting it.
                        finalize()
                        current = _new_block(
                            page_number, line, None,
                            needs_review=True,
                            reasons=[f"option {letter} repeated - a question marker was likely missed"],
                        )
                        option_left_margin = None
                    current["options"][letter] = value
                    state = letter
                    if option_left_margin is None:
                        option_left_margin = line["x0"]
                _expand_bbox(current, line)
                continue

        # Positional fallback: OCR sometimes misreads an option's own
        # bracketed marker glyph into something unrecognizable (e.g.
        # "(B)" -> "(8)", a common digit/letter confusion - see the docs
        # on this on this project's option letter detection). If we're
        # mid-way through a vertical option list (at least one option
        # already found normally, not all 4 yet) and this NEW physical
        # line starts with some short bracketed/punctuated token at
        # roughly the SAME left margin as that list's own markers (real
        # option lists line their markers up - this is the "neighboring
        # option markers" / indentation signal), trust that it's the
        # next option in sequence even though its marker didn't literally
        # read as a letter, rather than losing it as continuation text
        # glued onto the previous option.
        if (
            state in _NEXT_LETTER
            and option_left_margin is not None
            and abs(line["x0"] - option_left_margin) <= char_width * 2
        ):
            m = UNCERTAIN_MARKER_RE.match(text)
            if m and len(m.group(0).strip()) <= 5:
                letter = _NEXT_LETTER[state]
                if not current["options"][letter]:
                    value = clean_option_value(text[m.end():])
                    current["options"][letter] = value
                    state = letter
                    _expand_bbox(current, line)
                    continue

        # Continuation line: append to whichever field is currently open.
        if state == "QUESTION":
            current["question"] = (current["question"] + " " + text).strip()
        elif state in ("A", "B", "C", "D"):
            if all(current["options"][letter] for letter in "ABCD"):
                # All 4 options are already filled. An unmarked line
                # arriving now is far more likely to be the start of the
                # NEXT question (whose own marker was also lost to OCR)
                # than a genuine continuation of the last option - an
                # exam naturally moves to a new question right after its
                # fourth option, so gluing this onto option D would
                # silently contaminate it with the next question's stem.
                # Document furniture between questions is discarded
                # outright rather than becoming a fake question.
                if is_heading_or_instruction(text):
                    continue
                finalize()
                current = _new_block(
                    page_number, line, None,
                    needs_review=True,
                    reasons=["no question marker detected before this content"],
                )
                current["question"] = text
                state = "QUESTION"
                option_left_margin = None
            else:
                current["options"][state] = (current["options"][state] + " " + text).strip()
        _expand_bbox(current, line)

    finalize()
    return blocks


def build_mcq_blocks(pages):
    """
    pages: list of {
        "page_number": int,
        "words": [{"text","x0","y0","x1","y1"}, ...],
        "page_width": number,
    }
    (as produced by ocr_engine.ocr_image_blocks / pdf_extractor page
    words - both OCR and digital-PDF sources use this same shape).

    Returns (blocks, debug_info):
      blocks: flat list of MCQ blocks, in overall reading order (page,
        then column left-to-right, then top-to-bottom within a column).
      debug_info: {"pages": [{"page_number", "columns": [...], "blocks": [...]}]}
        - lightweight, JSON-safe summary for the debug endpoint (no raw
        per-word coordinate dumps, just column/line counts and per-block
        bounding boxes/status).
    """
    matcher = QuestionMatcher()
    all_blocks = []
    debug_pages = []

    for page in pages:
        words = page.get("words") or []
        page_number = page.get("page_number")
        page_width = page.get("page_width") or 0

        if not words:
            debug_pages.append({"page_number": page_number, "columns": [], "blocks": []})
            continue

        columns = get_columns(words, page_width)
        debug_columns = []
        page_blocks = []

        for col in columns:
            lines = build_lines(col["words"])
            if not lines:
                continue

            char_width = _char_width_for(lines)
            baseline_x0 = min(l["x0"] for l in lines)

            col_blocks = _process_column_lines(lines, matcher, page_number, char_width, baseline_x0)
            page_blocks.extend(col_blocks)

            debug_columns.append({
                "gutter": col.get("gutter"),
                "num_lines": len(lines),
                "num_words": len(col["words"]),
            })

        all_blocks.extend(page_blocks)
        debug_pages.append({
            "page_number": page_number,
            "columns": debug_columns,
            "blocks": [
                {
                    "question_number_guess": b["question_number_guess"],
                    "bbox": b["bbox"],
                    "needs_review": b["needs_review"],
                    "review_reasons": b["review_reasons"],
                    "question_preview": b["question"][:60],
                    "options_filled": [l for l in "ABCD" if b["options"][l].strip()],
                }
                for b in page_blocks
            ],
        })

    return all_blocks, {"pages": debug_pages}


def format_blocks(blocks):
    """
    Validates and formats blocks into the same output shape used
    throughout the project (total_questions/questions/incomplete_*), plus
    a needs_review_* bucket for MCQs the builder couldn't confidently
    place (kept separate from "questions" rather than silently included,
    per spec).
    """
    formatted = []
    incomplete = []
    needs_review = []
    q_num = 1

    for b in blocks:
        entry = {
            "question": b["question"].strip(),
            "options": {k: v.strip() for k, v in b["options"].items()},
        }

        if b["needs_review"]:
            needs_review.append({
                **entry,
                "reasons": b["review_reasons"],
                "page_number": b["page_number"],
            })
            continue

        if validate_mcq(entry):
            formatted.append({"question_number": q_num, **entry})
            q_num += 1
        else:
            incomplete.append({
                **entry,
                "missing": missing_parts(entry),
            })

    return {
        "total_questions": len(formatted),
        "questions": formatted,
        "incomplete_count": len(incomplete),
        "incomplete_questions": incomplete,
        "needs_review_count": len(needs_review),
        "needs_review_questions": needs_review,
    }
