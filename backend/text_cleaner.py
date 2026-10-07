"""
text_cleaner.py
Normalizes OCR/PDF extracted text before it reaches the MCQ parser.
Compatible with the current FastAPI project.
"""

import re
import unicodedata

# Regex helpers
_MULTI_SPACE_RE = re.compile(r"[ \t]+")
_MULTI_BLANK_LINE_RE = re.compile(r"\n{3,}")
_TRAILING_WS_RE = re.compile(r"[ \t]+\n")


def normalize_unicode(text: str) -> str:
    """Normalize Unicode (important for Hindi/Marathi)."""
    if not text:
        return ""
    return unicodedata.normalize("NFC", text)


def clean_text(text: str) -> str:
    """
    Cleans OCR text while preserving question structure.
    """

    if not text:
        return ""

    text = normalize_unicode(text)

    # Normalize line endings
    text = text.replace("\r\n", "\n").replace("\r", "\n")

    # Remove OCR artifacts
    text = text.replace("\u200b", "")   # Zero-width space
    text = text.replace("\ufeff", "")   # BOM
    text = text.replace("\x0c", "\n")   # Form feed
    text = text.replace("{", "")
    text = text.replace("}", "")

    # Fix common OCR patterns
    # "Q1" is very often misread as "Qi"/"Ql"/"QI" (the digit 1 mistaken
    # for a similar-looking letter), which otherwise silently drops the
    # entire first question since nothing matches the question pattern.
    # Only applied at the very start of a line, right after "Q", so it
    # can't corrupt ordinary words.
    text = re.sub(r"(?m)^(\s*)Q\s*\.?\s*[iIlL](?=[\s.):]|$)", r"\1Q.1", text)

    # "Q4", "Q 4", "Q.  4" -> "Q.4" (also handles the no-space case OCR
    # frequently produces, which the old patterns below missed).
    text = re.sub(r"\bQ\s*\.?\s*(\d+)", r"Q.\1", text)

    # OCR frequently misreads "(C)" as "(©" (copyright glyph) or "(c" with
    # a stray curly/round confusable. Normalize the copyright glyph to C
    # wherever it appears so it reads as a normal option marker.
    text = text.replace("©", "C")

    # Normalize Devanagari digits
    text = devanagari_digits_to_ascii(text)

    # Clean whitespace
    text = _TRAILING_WS_RE.sub("\n", text)
    text = _MULTI_SPACE_RE.sub(" ", text)
    text = _MULTI_BLANK_LINE_RE.sub("\n\n", text)

    # Strip each line but preserve line breaks
    lines = [line.strip() for line in text.split("\n")]
    text = "\n".join(lines)

    return text.strip()


def split_lines(text: str) -> list:
    """Return non-empty cleaned lines."""
    if not text:
        return []

    return [line.strip() for line in text.split("\n") if line.strip()]


# Devanagari digit mapping
DEVANAGARI_DIGITS = {
    "०": "0",
    "१": "1",
    "२": "2",
    "३": "3",
    "४": "4",
    "५": "5",
    "६": "6",
    "७": "7",
    "८": "8",
    "९": "9",
}


def devanagari_digits_to_ascii(text: str) -> str:
    """Convert Devanagari digits to ASCII digits."""
    return "".join(DEVANAGARI_DIGITS.get(ch, ch) for ch in text)