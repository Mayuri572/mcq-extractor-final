"""
ocr_engine.py
OCR engine for multilingual MCQ extraction.
Compatible with the existing FastAPI project.
"""

import os

import cv2
import numpy as np
import pytesseract
from pytesseract import Output
from PIL import Image

from backend.layout_detector import sort_reading_order

# Tesseract binary location: cross-platform. Only override pytesseract's
# default (which relies on `tesseract` being on PATH, correct on Linux/macOS)
# when the user explicitly points to a non-PATH install via TESSERACT_CMD
# (common on Windows, e.g. C:\Program Files\Tesseract-OCR\tesseract.exe).
_tesseract_cmd = os.environ.get("TESSERACT_CMD")
if _tesseract_cmd:
    pytesseract.pytesseract.tesseract_cmd = _tesseract_cmd

LANGUAGE_OPTIONS = {
    "auto": {"tesseract": "eng+hin+mar", "label": "Auto Detect"},
    "english": {"tesseract": "eng", "label": "English"},
    "hindi": {"tesseract": "hin", "label": "Hindi"},
    "marathi": {"tesseract": "mar", "label": "Marathi"},
    "english_hindi": {"tesseract": "eng+hin", "label": "English + Hindi"},
    "english_marathi": {"tesseract": "eng+mar", "label": "English + Marathi"},
    "hindi_marathi": {"tesseract": "hin+mar", "label": "Hindi + Marathi"},
    "english_hindi_marathi": {
        "tesseract": "eng+hin+mar",
        "label": "English + Hindi + Marathi",
    },
}

DEFAULT_LANGUAGE_KEY = "auto"


class OcrNotAvailableError(Exception):
    pass


def tesseract_lang_for(language_key: str) -> str:
    return LANGUAGE_OPTIONS.get(
        language_key, LANGUAGE_OPTIONS[DEFAULT_LANGUAGE_KEY]
    )["tesseract"]


def preprocess_image(image):
    """
    Improves OCR accuracy:
    - Grayscale
    - Upscale small/low-DPI scans (Tesseract does poorly below ~300 DPI
      equivalent; exam-paper scans/photos are often smaller than that)
    - Denoise
    - Adaptive threshold
    """

    if isinstance(image, str):
        img = cv2.imread(image)
    else:
        img = cv2.cvtColor(np.array(image), cv2.COLOR_RGB2BGR)

    if img is None:
        raise ValueError("Could not read image.")

    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)

    # Upscale small images so character strokes are thick enough for
    # Tesseract, and so word-box coordinates remain usable for layout
    # detection. Targets a working width of ~1800px.
    h, w = gray.shape[:2]
    if max(h, w) < 1800:
        scale = 1800 / max(h, w)
        gray = cv2.resize(gray, None, fx=scale, fy=scale, interpolation=cv2.INTER_CUBIC)

    gray = cv2.fastNlMeansDenoising(gray, h=20)

    processed = cv2.adaptiveThreshold(
        gray,
        255,
        cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
        cv2.THRESH_BINARY,
        31,
        15,
    )

    return processed


def _run_tesseract_data(processed, langs, psm):
    return pytesseract.image_to_data(
        processed,
        lang=langs,
        config=f"--oem 3 --psm {psm}",
        output_type=Output.DICT,
    )


def _data_to_blocks(data):
    blocks = []

    for i in range(len(data["text"])):

        word = data["text"][i].strip()

        if not word:
            continue

        try:
            conf = float(data["conf"][i])
        except Exception:
            conf = 0

        if conf < 0:
            continue

        blocks.append(
            {
                "text": word,
                "x0": data["left"][i],
                "y0": data["top"][i],
                "x1": data["left"][i] + data["width"][i],
                "y1": data["top"][i] + data["height"][i],
            }
        )

    return blocks


def extract_text_with_coordinates(image, langs="eng+hin+mar"):
    """
    Runs OCR and returns word-level coordinates.

    Tries PSM 3 (fully automatic page segmentation) first, which is the
    most robust default for real exam-paper scans (mixed single/two-column
    layouts, headers, tables). If that yields too little text (a page that
    is mostly a single dense column can sometimes confuse automatic
    segmentation), falls back to PSM 6 (single uniform block).
    """

    processed = preprocess_image(image)

    data = _run_tesseract_data(processed, langs, psm=3)
    blocks = _data_to_blocks(data)

    if len(blocks) < 8:
        data = _run_tesseract_data(processed, langs, psm=6)
        fallback_blocks = _data_to_blocks(data)
        if len(fallback_blocks) > len(blocks):
            blocks = fallback_blocks

    return blocks


def ocr_image_with_layout(image, lang):
    """
    OCR + reading-order reconstruction.
    Returns final extracted text (best-effort flattened transcript - not
    used for MCQ parsing on the coordinate path, see ocr_image_blocks).
    """

    try:
        blocks, width, _height = ocr_image_blocks(image, lang)
        if not blocks:
            return ""
        return sort_reading_order(blocks, width)

    except pytesseract.TesseractNotFoundError:
        raise OcrNotAvailableError("Tesseract OCR not found.")


def ocr_image_blocks(image, lang):
    """
    OCR an image (path or already-open PIL Image) and return its raw
    word-level coordinates, WITHOUT flattening to text - this is what the
    MCQ pipeline (block_builder.py) actually consumes, so that column and
    question/option grouping happens from real coordinates rather than
    from a pre-linearized text stream.

    Returns (blocks, width, height).
    """
    try:
        if isinstance(image, str):
            pil_image = Image.open(image).convert("RGB")
        else:
            pil_image = image

        blocks = extract_text_with_coordinates(pil_image, lang)
        return blocks, pil_image.width, pil_image.height

    except pytesseract.TesseractNotFoundError:
        raise OcrNotAvailableError("Tesseract OCR not found.")


def ocr_image_file(path, lang):
    """Convenience: OCR then flatten to text. The main pipeline uses
    ocr_image_blocks() directly to preserve coordinates; this is kept for
    callers that only want a plain-text transcript."""
    image = Image.open(path).convert("RGB")
    return ocr_image_with_layout(image, lang)