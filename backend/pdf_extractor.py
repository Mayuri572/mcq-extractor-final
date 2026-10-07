"""
pdf_extractor.py
Compatible PDF extractor for the existing FastAPI project.
Supports:
- Digital PDFs (pdfplumber with coordinates)
- Scanned PDFs (Poppler + OCR fallback)
- Automatic OCR fallback for poor text extraction
"""

import os
import pdfplumber
from pdf2image import convert_from_path

# Poppler location: cross-platform. pdf2image finds `pdftoppm`/`pdftocairo`
# on PATH by default (correct on Linux/macOS). Only override via the
# POPPLER_PATH env var when Poppler isn't on PATH (common on Windows).
POPPLER_PATH = os.environ.get("POPPLER_PATH") or None


def extract_pdf_text(file_path: str):
    """
    Returns per-page WORD COORDINATES (not flattened text) so the caller
    (main.py -> block_builder.build_mcq_blocks) can group questions and
    options from real positions, the same way it does for OCR'd pages -
    a digital PDF page and a scanned page end up in the identical shape,
    so there's one grouping code path for both instead of two.

    {
        "pages": [
            {
                "page_number": 0,
                "words": [{"text","x0","y0","x1","y1"}, ...],
                "page_width": 612.0,
                "needs_ocr": False,
            }
        ]
    }

    A page with too little extractable text (needs_ocr=True, words=[])
    is presumed scanned; the caller renders it to an image and OCRs it
    instead (see render_page_to_image below).
    """

    pages = []

    with pdfplumber.open(file_path) as pdf:

        for page_num, page in enumerate(pdf.pages):

            words = page.extract_words()

            # Very little text -> probably scanned
            if not words or len(words) < 15:

                pages.append({
                    "page_number": page_num,
                    "words": [],
                    "page_width": page.width,
                    "needs_ocr": True,
                })

                continue

            blocks = [
                {
                    "text": w["text"],
                    "x0": w["x0"],
                    "y0": w["top"],
                    "x1": w["x1"],
                    "y1": w["bottom"],
                }
                for w in words
            ]

            pages.append({
                "page_number": page_num,
                "words": blocks,
                "page_width": page.width,
                "needs_ocr": False,
            })

    return {"pages": pages}


def render_page_to_image(file_path: str, page_number: int):
    """
    Converts one PDF page into a PIL image.
    Compatible with main.py.
    """

    images = convert_from_path(
        file_path,
        first_page=page_number + 1,
        last_page=page_number + 1,
        dpi=300,
        poppler_path=POPPLER_PATH,
    )

    return images[0]