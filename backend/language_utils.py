"""
language_utils.py
Lightweight, non-ML-heavy language detection used only for the
"Detected Language(s)" summary shown to the user. Extraction/parsing
itself does not depend on this module.

Approach:
- Unicode script ranges tell us whether Latin (English) and/or
  Devanagari (Hindi/Marathi) text is present.
- Devanagari alone doesn't tell Hindi and Marathi apart, so we use the
  lightweight statistical `langdetect` library, if available, as a
  best-effort tie-breaker. If it isn't available or is inconclusive,
  we report both as a combined possibility rather than guessing wrong.
"""

import re

_DEVANAGARI_RE = re.compile(r"[\u0900-\u097F]")
_LATIN_RE = re.compile(r"[A-Za-z]")


def _has_devanagari(text: str) -> bool:
    return bool(_DEVANAGARI_RE.search(text or ""))


def _has_latin(text: str) -> bool:
    return bool(_LATIN_RE.search(text or ""))


def _guess_hindi_or_marathi(text: str):
    """Best-effort split of Devanagari text into Hindi / Marathi / both."""
    try:
        from langdetect import detect_langs, DetectorFactory
        DetectorFactory.seed = 0  # deterministic results
    except ImportError:
        return ["Hindi", "Marathi"]  # can't tell apart without the library

    sample = " ".join(re.findall(r"[\u0900-\u097F]+", text))
    if len(sample) < 8:
        return ["Hindi", "Marathi"]

    try:
        guesses = detect_langs(sample)
    except Exception:
        return ["Hindi", "Marathi"]

    found = set()
    for g in guesses:
        if g.lang == "hi":
            found.add("Hindi")
        elif g.lang == "mr":
            found.add("Marathi")

    if not found:
        return ["Hindi", "Marathi"]
    return sorted(found)


def detect_languages(text: str) -> list:
    """Return a list of display language names detected in the given text."""
    languages = []
    if _has_latin(text):
        languages.append("English")
    if _has_devanagari(text):
        languages.extend(_guess_hindi_or_marathi(text))
    if not languages:
        languages.append("Unknown")
    # de-duplicate while preserving order
    seen = set()
    ordered = []
    for lang in languages:
        if lang not in seen:
            seen.add(lang)
            ordered.append(lang)
    return ordered
