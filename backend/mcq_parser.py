"""
OCR-tolerant multilingual MCQ parser.
Compatible with the existing FastAPI project.

Two entry points share the regex patterns and helpers in this file:

- parse_mcqs(text): a text-only state machine, used when there are no
  real coordinates to work with (TXT/DOCX input). It has no notion of
  columns, so it relies purely on numbering-sequence + a couple of
  defensive checks to avoid mixing questions and options up.

- block_builder.build_mcq_blocks(pages): the coordinate-aware path used
  for images and PDFs (both OCR'd and digital). It processes one
  column's lines at a time, start to finish, before moving to the next
  column - so it is architecturally impossible for it to attach one
  column's options to another column's question. It reuses the same
  regex patterns, the same _QuestionMatcher, and the same option-repeat
  safety check defined here, just driven by real line coordinates
  instead of encoded indentation.

Output shape of parse_mcqs() keeps the original fields (total_questions,
questions, incomplete_count) and adds:
  - incomplete_questions: whatever was extracted for a question that
    failed validation (missing question text or an option).
  - needs_review_count / needs_review_questions: MCQs where the parser
    had to guess a question/option boundary without a confidently
    detected marker (e.g. an option letter repeated, which can only mean
    a question boundary was missed) - kept separate from the trusted
    "questions" list rather than silently included, per spec.
"""

import re
from enum import Enum


class ParseState(Enum):
    WAITING = 0
    QUESTION = 1
    OPT_A = 2
    OPT_B = 3
    OPT_C = 4
    OPT_D = 5


# Unambiguous question markers: these prefixes never appear as internal
# numbering inside a question (sub-statement lists use bare digits or
# roman numerals instead), so a match here is always trusted as a new
# question. Covers Q1, Q.1, Q. 1, Q1), Question 1, प्रश्न १.
QUESTION_RE_STRICT = re.compile(
    r'^\s*(?:Q\s*\.?\s*(\d+)|Question\s+(\d+)|प्रश्न\s*[.\s]*([0-9०-९]+))\s*[.)]?\s*(.*)$',
    re.IGNORECASE
)

# Ambiguous question marker: a bare "12." or "12)" at the start of a line.
# This is also the exact style exam papers use for internal numbered
# sub-statement lists inside a question (e.g. "1." "2." "3." listing
# statements to evaluate). Per spec, those must NOT be treated as a new
# question. A bare-number match is only trusted when it continues the
# running question-number sequence (see _QuestionMatcher below); anything
# else bare-numbered is left as continuation text of the current field.
QUESTION_RE_BARE = re.compile(r'^\s*(\d+)\s*[.)]\s*(.*)$')

# An option marker must be either fully bracketed ((A), [A], {A}) or
# followed by a period/close-paren (A., A)) - a bare letter with nothing
# around it is NOT accepted, since that would match ordinary words/letters
# inside running prose (e.g. the word "a" in "a material").
_LETTERS = r'A|B|C|D|a|b|c|d|अ|ब|क|ड'
OPTION_RE = re.compile(
    r'(?:[\(\[\{]\s*(' + _LETTERS + r')\s*[\)\]\}]'
    r'|(?<![A-Za-z\u0900-\u097F])(' + _LETTERS + r')\s*[.)])'
)

# Generic "this looks like SOME kind of short bracketed/punctuated token"
# pattern - much looser than OPTION_RE (which requires the content to
# actually read as A-D). Used only as a last-resort POSITIONAL fallback
# (see split_horizontal_options below, and the mirrored per-line fallback
# in block_builder.py) for when an option's own marker glyph was
# OCR-misread into something unrecognizable entirely - e.g. "(C)" read as
# "(8)", or "(D)" read as "09)" (both common digit/letter confusions).
# Deliberately shared between the two modules rather than redefined, so
# the same "what counts as an uncertain marker" rule applies everywhere.
UNCERTAIN_MARKER_RE = re.compile(
    r'(?:[\(\[\{]\s*\S{1,3}\s*[\)\]\}]|(?<!\S)\S{1,3}\s*[.)])'
)

# Trailing separator/punctuation to trim off an option's value. Hyphen is
# only trimmed from the *trailing* side (see _clean_option_value) since a
# *leading* hyphen is meaningful content for numeric options like "-1/2".
_EDGE_JUNK = " \t)].:"
_TRAILING_ONLY_JUNK = _EDGE_JUNK + "-\u2013\u2014\u0964"


def _clean_option_value(raw_value):
    value = raw_value.strip()
    value = value.lstrip(_EDGE_JUNK)
    value = value.rstrip(_TRAILING_ONLY_JUNK)
    return value.strip()


# Backward/cross-module-compat alias (used by block_builder.py).
clean_option_value = _clean_option_value


_DEV_DIGITS = {"०": "0", "१": "1", "२": "2", "३": "3", "४": "4",
               "५": "5", "६": "6", "७": "7", "८": "8", "९": "9"}


def _devanagari_to_int(s):
    ascii_digits = "".join(_DEV_DIGITS.get(ch, ch) for ch in s)
    return int(ascii_digits)


def normalize_option_letter(letter):
    mapping = {
        "A": "A", "a": "A", "अ": "A",
        "B": "B", "b": "B", "ब": "B",
        "C": "C", "c": "C", "क": "C",
        "D": "D", "d": "D", "ड": "D",
    }
    return mapping.get(letter, letter.upper())


def _option_match_letter(m):
    return m.group(1) or m.group(2)


def line_starts_with_option(line):
    """
    True only if an option marker sits at (or essentially at) the start of
    the line, e.g. "(A) foo", "A. foo", "(A)0 (B)1 (C)-1 (D)-1/2". This
    stops a stray option-shaped token appearing mid-sentence in a
    continuation line from being mistaken for the start of an option.
    """
    return OPTION_RE.match(line.lstrip()) is not None


def split_horizontal_options(line):
    """
    Converts a line such as:
        (A)0 (B)1 (C)-1 (D)-1/2
    into:
        [("A","0"), ("B","1"), ("C","-1"), ("D","-1/2")]

    Also handles the single-option-per-line case ("(A) Ferromagnetic"),
    which simply yields one pair, and a line that legitimately starts
    mid-sequence, e.g. "(C) Paramagnetic (D) Antiferromagnetic" as the
    second physical line of a two-line horizontal layout whose (A)/(B)
    were on the previous line - the returned letters are whatever was
    actually found, not forced to start at A.

    If 2+ real A-D markers are found but the run doesn't reach D, a
    later option's own marker glyph was likely OCR-misread into
    something unrecognizable (e.g. "(C)" read as "(8)", or "(D)" read as
    "09)" - both common digit/letter confusions): falls back to filling
    the remaining trailing slots POSITIONALLY, trusting any further short
    bracketed/punctuated token after the last confirmed marker to be the
    next option in sequence. This only ever activates once at least 2
    real letters already anchor the sequence (never for an isolated
    single vertical option, where finding just 1 letter is the correct,
    complete result), and only on a line already confirmed to start with
    a genuine option marker - never on arbitrary text.
    """
    matches = list(OPTION_RE.finditer(line))
    if not matches:
        return []

    order = "ABCD"
    # Keep only a strictly-increasing subsequence of real letter matches,
    # by whatever letter they actually are - a stray out-of-order match
    # elsewhere in the line is more likely noise than a genuine marker.
    sequence = []
    last_idx = -1
    for m in matches:
        idx = order.index(normalize_option_letter(_option_match_letter(m)))
        if idx > last_idx:
            sequence.append((idx, m))
            last_idx = idx

    if len(sequence) >= 2 and last_idx < len(order) - 1:
        search_from = sequence[-1][1].end()
        for um in UNCERTAIN_MARKER_RE.finditer(line, search_from):
            if any(um.start() < m.end() and um.end() > m.start() for _, m in sequence):
                continue
            last_idx += 1
            sequence.append((last_idx, um))
            if last_idx >= len(order) - 1:
                break

    parts = []
    for i, (idx, m) in enumerate(sequence):
        start = m.end()
        end = sequence[i + 1][1].start() if i + 1 < len(sequence) else len(line)
        value = _clean_option_value(line[start:end])
        # Position (among the kept real letters, filled forward from
        # there) determines the letter - consistent for both real and
        # recovered markers, since the whole point of recovery is that
        # we can't fully trust a misread marker's literal identity.
        parts.append((order[idx], value))

    return parts


def validate_mcq(mcq):
    if not mcq["question"].strip():
        return False
    return all(mcq["options"][x].strip() for x in ["A", "B", "C", "D"])


def missing_parts(mcq):
    """Human-readable list of what's missing, for incomplete-question reports."""
    missing = []
    if not mcq["question"].strip():
        missing.append("question text")
    for letter in ["A", "B", "C", "D"]:
        if not mcq["options"][letter].strip():
            missing.append(f"option {letter}")
    return missing


# Document furniture that must never be treated as a question, however it
# ends up unmarked in the line stream (a missing question-number glyph
# would otherwise make this indistinguishable from a genuinely marker-
# less question). Matched case-insensitively, keyword-or-pattern based
# rather than "contains Q" or "contains a number", which is far too broad
# (a real question can easily contain either).
_HEADING_KEYWORDS = (
    "instructions", "general instructions", "note:", "answer the following",
    "carries marks", "marks each", "time allowed", "maximum marks",
    "total marks", "do not open", "question paper", "roll number",
    "duration:", "read the following instructions", "question booklet",
    "test booklet",
)

_HEADING_RE_PATTERNS = (
    # "GATE 2018", "CSP 2026" - a short run of capital letters/initials
    # immediately followed by a 4-digit year, with nothing else on the
    # line (a title/masthead line, not a sentence).
    re.compile(r'^[A-Z][A-Z.\-() ]{0,20}\b(19|20)\d{2}\b[A-Za-z()\- ]{0,25}$'),
    # "carry one mark each", "carries two marks" - the standard exam
    # mark-allocation instruction line.
    re.compile(r'\bcarr(?:y|ies)\s+(?:one|two|three|four|five|\d+)\s+marks?\b', re.IGNORECASE),
    # "Q.1-Q.9", "Q. 10 - Q. 22" - a question-number RANGE (as opposed to
    # a single question marker), which only ever appears in instructions
    # describing how a paper is organized, never as a question itself.
    re.compile(r'\bQ\.?\s*\d+\s*[-\u2013\u2014]\s*Q\.?\s*\d+\b', re.IGNORECASE),
    # "Section A", "Part II", "PART B" as a short standalone line.
    re.compile(r'^(?:section|part)\s+[A-Za-z0-9]{1,4}\s*[:.\-]?\s*$', re.IGNORECASE),
)


def _matches_heading_pattern(text):
    """Structural-only heading check (title+year lines, mark-allocation
    lines, question-range lines, standalone Section/Part labels) - safe
    to apply to ANY line, including one already mid-flow as question or
    option continuation text, because these shapes essentially never
    occur as a substring of genuine question/option content."""
    t = text.strip()
    return bool(t) and any(pat.search(t) for pat in _HEADING_RE_PATTERNS)


def is_heading_or_instruction(text):
    """
    True for document furniture (titles, section/part labels, exam
    instructions) that must never be treated as a question or surfaced
    as a needs-review MCQ, even when it has no question marker of its
    own - which is exactly when it would otherwise be indistinguishable
    from a genuinely marker-less question. Deliberately keyword/pattern
    based rather than "contains a number" or "contains Q", both of which
    a real question can easily do.

    Broader than _matches_heading_pattern (adds keyword phrases like
    "instructions", "roll number"): only safe to apply where there is no
    legitimate in-progress question/option content that could be
    silently swallowed by a false positive - i.e. when a line has no
    open question to attach to, not to arbitrary continuation text (a
    genuine option could legitimately mention "the instructions on the
    answer sheet" as content, for instance).
    """
    t = text.strip()
    if not t:
        return False

    if any(kw in t.lower() for kw in _HEADING_KEYWORDS):
        return True

    return _matches_heading_pattern(t)


# Two-column exam papers often print a thin vertical rule between the
# columns. On the text-only fallback path (no real coordinates - TXT/DOCX
# input has no notion of columns at all, so this never applies there
# either; it's a defensive normalization only), a lone "|" immediately
# followed by "<number>." / "<number>)" is not legitimate running text in
# any of the supported languages, so splitting the line there is safe.
_MIDLINE_COLUMN_BLEED_RE = re.compile(r'^(.*\S)\s*\|\s*(\d+\s*[.)]\s*.*)$')


def _split_midline_column_bleed(lines):
    out = []
    for line in lines:
        m = _MIDLINE_COLUMN_BLEED_RE.match(line)
        if m and len(m.group(1).strip()) > 3:
            out.append(m.group(1))
            out.append(m.group(2))
        else:
            out.append(line)
    return out


def extract_inline_options(text):
    """
    If `text` contains question-stem text followed by a run of 2+ option
    markers on the SAME line (e.g. "What is 5x5? (A) 20 (B) 25 (C) 30
    (D) 35", or just "(A) 20 (B) 25 (C) 30 (D) 35" with no stem at all),
    returns (stem, [(letter, value), ...]).

    Returns (text, []) if no such run is found - ordinary prose that
    happens to contain a single incidental option-shaped token (e.g. a
    stray "(a)" aside) is left alone: the run must start at letter A and
    each subsequent match must be a distinct, strictly increasing letter
    (A, then B, then C, ...), which is what distinguishes a genuine
    embedded options block from an unrelated coincidental match.
    """
    matches = list(OPTION_RE.finditer(text))
    if len(matches) < 2:
        return text, []

    order = "ABCD"
    letters = [normalize_option_letter(_option_match_letter(m)) for m in matches]
    if letters[0] != "A" or len(set(letters)) != len(letters):
        return text, []
    if letters != sorted(letters, key=order.index):
        return text, []

    stem = text[:matches[0].start()].strip()
    options = split_horizontal_options(text[matches[0].start():])
    return stem, options


def new_mcq_block(question_text=""):
    return {
        "question": question_text.strip(),
        "options": {"A": "", "B": "", "C": "", "D": ""},
    }


# Backward-compat alias (previously private/internal name).
_new_mcq = new_mcq_block


class QuestionMatcher:
    """
    Decides whether a line starts a new question, using two independent
    signals:

    1. Numbering sequence - a bare number is only trusted as a new
       question if it continues the running question-number sequence
       (see the QUESTION_RE_BARE comment above for why this matters).

    2. Indentation - internal numbered sub-statement lists are typically
       indented further right than the question's own number in the
       source layout. Once a reliable left-margin baseline has been
       established from at least one confidently identified question
       start, a bare number indented well past that baseline is rejected
       as a sub-item even if the numbering sequence lost its anchor
       earlier (e.g. because OCR failed to read one page's
       question-number glyph) - this is what actually recovers the
       document once the numbering heuristic alone would otherwise
       cascade into treating every sub-item as its own question.

    On the text-only path, indentation is encoded as a leading-space
    count (an approximate character-width unit). On the coordinate path
    (block_builder.py), it's derived directly from each line's real x0
    relative to its column's left margin, in the same units - both
    callers share this one class so the decision logic never diverges.

    Text sources with no real coordinates (TXT/DOCX input) carry no
    indentation, so every line is indent 0 and this degrades cleanly to
    numbering-sequence-only behavior.
    """

    INDENT_MARGIN = 3

    def __init__(self):
        self.expected_next = None  # None until the first question is seen
        self.baseline_indent = None  # None until a reliable anchor is seen
        self.last_number = None  # most recently matched question number, for debug/reporting

    def match(self, line, indent=0):
        m = QUESTION_RE_STRICT.match(line)
        if m:
            num_str = m.group(1) or m.group(2) or m.group(3)
            text = m.group(4) or ""
            self._advance(num_str)
            self._update_baseline(indent)
            return text

        m = QUESTION_RE_BARE.match(line)
        if m:
            num_str, text = m.group(1), m.group(2)
            try:
                num = int(num_str)
            except ValueError:
                return None

            if self.baseline_indent is not None and indent > self.baseline_indent + self.INDENT_MARGIN:
                return None  # indented well past the question-start margin: a sub-item

            if self.expected_next is None or num in (self.expected_next, self.expected_next + 1):
                self.expected_next = num + 1
                self._update_baseline(indent)
                self.last_number = num
                return text

            return None  # looks like internal numbering, not a new question

        return None

    def _update_baseline(self, indent):
        if self.baseline_indent is None or indent < self.baseline_indent:
            self.baseline_indent = indent

    def _advance(self, num_str):
        try:
            num = _devanagari_to_int(num_str)
        except (TypeError, ValueError):
            return
        self.expected_next = num + 1
        self.last_number = num


# Backward-compat alias (previously private/internal name).
_QuestionMatcher = QuestionMatcher


def parse_mcqs(text):
    """
    Text-only parser (no coordinates available): used for TXT/DOCX input.
    See block_builder.build_mcq_blocks() for the coordinate-aware path
    used for images and PDFs, which is what actually guarantees options
    can't be attached to the wrong question on multi-column pages.
    """

    lines = _split_midline_column_bleed(text.splitlines())

    mcqs = []
    current = None
    state = ParseState.WAITING
    matcher = QuestionMatcher()

    OPT_STATE_FOR_LETTER = {
        "A": ParseState.OPT_A, "B": ParseState.OPT_B,
        "C": ParseState.OPT_C, "D": ParseState.OPT_D,
    }
    LETTER_FOR_OPT_STATE = {v: k for k, v in OPT_STATE_FOR_LETTER.items()}

    def finalize(mcq, needs_review=False, reasons=None):
        if mcq is None:
            return
        status = "complete" if validate_mcq(mcq) else "incomplete"
        mcqs.append({
            "status": status,
            "needs_review": needs_review,
            "reasons": reasons or [],
            **mcq,
        })

    review_reasons = []

    for raw in lines:

        line = raw.strip()
        if not line:
            continue

        # Leading-space count encodes this line's relative indentation -
        # captured before the strip() above discards it.
        indent = len(raw) - len(raw.lstrip(" "))

        # Remove OCR artifacts (stray braces around option letters, e.g.
        # OCR'd "{A)" for "(A)").
        line = line.replace("{", "").replace("}", "")

        # Document furniture (titles, mark-allocation lines, question-
        # range lines) must never become a question, including via a
        # coincidental bare-number match (e.g. a "1." that's really part
        # of a "Q.1-Q.9 carry one mark each" instruction line).
        # matcher.match() has side effects (it advances the numbering-
        # sequence tracker), so this is checked before it runs rather
        # than discarding the result afterward, to keep that internal
        # state from being corrupted by a heading that happens to look
        # number-like. Pattern-only (not the broader keyword check further
        # below) since this runs on EVERY line, including legitimate
        # question/option continuation text.
        if _matches_heading_pattern(line):
            continue

        q_text = matcher.match(line, indent)

        if q_text is not None:
            finalize(current, needs_review=bool(review_reasons), reasons=review_reasons)
            review_reasons = []
            q_text_stripped = q_text.strip()

            if q_text_stripped and line_starts_with_option(q_text_stripped):
                # The question marker and its first option(s) sit on the
                # same physical line with no question text between them
                # (e.g. "Q2. (A) 5 (B) 10 ..."). Route q_text through the
                # option parser instead of storing it as question text.
                current = new_mcq_block("")
                for letter, value in split_horizontal_options(q_text_stripped):
                    current["options"][letter] = value
                    state = OPT_STATE_FOR_LETTER[letter]
            else:
                # Also handle question text followed by inline options
                # further along the SAME line (e.g. "Q7. What is 5x5?
                # (A) 20 (B) 25 (C) 30 (D) 35").
                stem, inline_options = extract_inline_options(q_text_stripped)
                current = new_mcq_block(stem)
                state = ParseState.QUESTION
                for letter, value in inline_options:
                    current["options"][letter] = value
                    state = OPT_STATE_FOR_LETTER[letter]

            continue

        if current is None:
            # An option/continuation line with no open question at all.
            # No legitimate content to lose here, so the broader
            # keyword+pattern heading check applies (document furniture
            # never gets a question marker either, so it lands here too).
            if is_heading_or_instruction(line):
                continue
            current = new_mcq_block("")
            review_reasons = ["no question marker detected before this content"]
            state = ParseState.QUESTION

        if line_starts_with_option(line):
            options_found = split_horizontal_options(line)
            if options_found:
                for letter, value in options_found:
                    if current["options"][letter]:
                        # Same option letter appearing twice inside one
                        # block can only mean a question boundary was
                        # missed (a real MCQ never repeats a letter).
                        # Close the current block instead of overwriting/
                        # corrupting it, and start a new one.
                        finalize(current, needs_review=True,
                                 reasons=review_reasons + [f"option {letter} repeated - a question marker was likely missed"])
                        current = new_mcq_block("")
                        review_reasons = [f"question marker not detected (inferred from option {letter} restarting)"]
                        state = ParseState.QUESTION
                    current["options"][letter] = value
                    state = OPT_STATE_FOR_LETTER[letter]
                continue

        # Multiline handling: append to whichever field is currently open.
        if state == ParseState.QUESTION:
            current["question"] += " " + line
        elif state in LETTER_FOR_OPT_STATE:
            letter = LETTER_FOR_OPT_STATE[state]
            if all(current["options"][l] for l in "ABCD"):
                # All 4 options are already filled - this unmarked line
                # is either the start of the next question (marker also
                # lost) or document furniture between questions; neither
                # belongs glued onto option D, and there's no legitimate
                # in-progress content here to lose, so the broader
                # keyword+pattern heading check applies.
                if is_heading_or_instruction(line):
                    continue
                finalize(current, needs_review=True,
                         reasons=["no question marker detected before this content"])
                current = new_mcq_block(line)
                review_reasons = []
                state = ParseState.QUESTION
            else:
                current["options"][letter] += " " + line

    finalize(current, needs_review=bool(review_reasons), reasons=review_reasons)

    formatted = []
    incomplete = []
    needs_review = []
    q_num = 1

    for mcq in mcqs:
        entry = {
            "question": mcq["question"].strip(),
            "options": {k: v.strip() for k, v in mcq["options"].items()},
        }

        if mcq["needs_review"]:
            needs_review.append({**entry, "reasons": mcq["reasons"]})
        elif mcq["status"] == "complete":
            formatted.append({"question_number": q_num, **entry})
            q_num += 1
        else:
            incomplete.append({
                "question": entry["question"],
                "options": entry["options"],
                "missing": missing_parts(mcq),
            })

    return {
        "total_questions": len(formatted),
        "questions": formatted,
        "incomplete_count": len(incomplete),
        "incomplete_questions": incomplete,
        "needs_review_count": len(needs_review),
        "needs_review_questions": needs_review,
    }
