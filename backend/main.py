"""
main.py
FastAPI application: serves the frontend, runs the extraction pipeline on
upload, and exposes download endpoints for the generated JSON/DOCX files.

Extraction pipeline summary:
- IMAGE / PDF (has real coordinates): OCR'd or digital-PDF words are
  grouped into columns and lines (layout_detector), then into MCQ blocks
  column-by-column (block_builder) - never flattened into a single
  cross-column text stream before parsing, which is what keeps a
  question and its options from ever being attached to the wrong MCQ.
- TXT / DOCX (no coordinates at all): falls back to the text-only state
  machine (mcq_parser.parse_mcqs).
"""

import os
import traceback

from fastapi import FastAPI, UploadFile, File, Form, HTTPException
from fastapi.responses import JSONResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from PIL import ImageDraw

from backend.file_handler import save_upload, UnsupportedFileError
from backend.text_cleaner import clean_text
from backend.pdf_extractor import extract_pdf_text, render_page_to_image
from backend.docx_extractor import extract_docx_text
from backend.ocr_engine import (
    ocr_image_blocks,
    tesseract_lang_for,
    OcrNotAvailableError,
    LANGUAGE_OPTIONS,
    DEFAULT_LANGUAGE_KEY,
)
from backend.mcq_parser import parse_mcqs
from backend.block_builder import build_mcq_blocks, format_blocks
from backend.language_utils import detect_languages
from backend.json_generator import build_result_dict, write_json_file
from backend.docx_generator import write_docx_file

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FRONTEND_DIR = os.path.join(BASE_DIR, "frontend")
UPLOAD_DIR = os.path.join(BASE_DIR, "uploads")
OUTPUT_DIR = os.path.join(BASE_DIR, "outputs")

os.makedirs(UPLOAD_DIR, exist_ok=True)
os.makedirs(OUTPUT_DIR, exist_ok=True)

app = FastAPI(title="Multilingual MCQ Extractor")

FILE_TYPE_LABELS = {"IMAGE": "Image", "PDF": "PDF", "TXT": "TXT", "DOCX": "DOCX"}

# How many pages (with a rendered image available) to draw debug
# annotations for, when debug mode is on. Bounded so a large scanned PDF
# doesn't generate dozens of debug images per request.
MAX_DEBUG_ANNOTATED_PAGES = 3

_STATUS_COLORS = {"complete": "#2e7d32", "incomplete": "#e08a00", "needs_review": "#c62828"}


def _extract_pages_for_upload(stored_path: str, file_type: str, tesseract_lang: str):
    """
    Returns (pages, raw_text):
      - For IMAGE/PDF: pages is a list of {"page_number","words",
        "page_width","pil_image"} (pil_image is None for digital PDF
        pages that didn't need OCR), raw_text is None.
      - For TXT/DOCX: pages is None, raw_text is the extracted string
        (no coordinates exist for these formats).
    """
    if file_type == "TXT":
        with open(stored_path, "rb") as f:
            raw = f.read()
        try:
            return None, raw.decode("utf-8")
        except UnicodeDecodeError:
            return None, raw.decode("utf-8", errors="ignore")

    if file_type == "DOCX":
        return None, extract_docx_text(stored_path)

    if file_type == "IMAGE":
        try:
            words, width, height = ocr_image_blocks(stored_path, tesseract_lang)
        except OcrNotAvailableError as exc:
            raise HTTPException(status_code=500, detail=str(exc))
        from PIL import Image
        pil_image = Image.open(stored_path).convert("RGB")
        return [{
            "page_number": 0,
            "words": words,
            "page_width": width,
            "pil_image": pil_image,
        }], None

    if file_type == "PDF":
        pdf_result = extract_pdf_text(stored_path)
        pages = []
        for page in pdf_result["pages"]:
            if page["needs_ocr"]:
                try:
                    image = render_page_to_image(stored_path, page["page_number"])
                    words, width, height = ocr_image_blocks(image, tesseract_lang)
                except OcrNotAvailableError as exc:
                    raise HTTPException(status_code=500, detail=str(exc))
                pages.append({
                    "page_number": page["page_number"],
                    "words": words,
                    "page_width": width,
                    "pil_image": image,
                })
            else:
                pages.append({
                    "page_number": page["page_number"],
                    "words": page["words"],
                    "page_width": page["page_width"],
                    "pil_image": None,
                })
        return pages, None

    raise HTTPException(status_code=400, detail=f"Unsupported file type: {file_type}")


def _sample_text_for_language_detection(parsed: dict) -> str:
    """Builds a plain-text sample from already-parsed MCQs (questions +
    options, across the trusted/incomplete/needs-review buckets) for
    language auto-detection - avoids a second, separate text-flattening
    pass over the source."""
    parts = []
    for bucket in ("questions", "incomplete_questions", "needs_review_questions"):
        for q in parsed.get(bucket, []):
            parts.append(q.get("question", ""))
            parts.extend(q.get("options", {}).values())
    return "\n".join(p for p in parts if p)


def _languages_for_report(language_key: str, sample_text: str) -> list:
    if language_key == DEFAULT_LANGUAGE_KEY or language_key not in LANGUAGE_OPTIONS:
        return detect_languages(sample_text)
    label = LANGUAGE_OPTIONS[language_key]["label"]
    return [part.strip() for part in label.split("+")]


def _apply_text_cleanup_to_blocks(blocks):
    """Runs the same OCR-artifact/text normalization used on the
    text-only path (Q4->Q.4, (c)->C misreads, etc.) over each block's
    question/option text, field by field - blocks already have their
    boundaries decided by coordinates, so cleanup here only touches the
    text content, never re-derives structure."""
    for b in blocks:
        b["question"] = clean_text(b["question"])
        for letter, value in b["options"].items():
            b["options"][letter] = clean_text(value)
    return blocks


def _draw_debug_annotations(pil_image, page_blocks_debug):
    annotated = pil_image.convert("RGB").copy()
    draw = ImageDraw.Draw(annotated)
    for b in page_blocks_debug:
        if b["needs_review"]:
            status = "needs_review"
        elif len(b["options_filled"]) == 4 and b["question_preview"].strip():
            status = "complete"
        else:
            status = "incomplete"
        color = _STATUS_COLORS[status]
        x0, y0, x1, y1 = b["bbox"]
        draw.rectangle([x0, y0, x1, y1], outline=color, width=3)
        label = f"Q{b['question_number_guess']}" if b["question_number_guess"] else "?"
        draw.text((x0 + 2, max(0, y0 - 14)), label, fill=color)
    return annotated


@app.post("/upload")
async def upload(
    file: UploadFile = File(...),
    language: str = Form(DEFAULT_LANGUAGE_KEY),
    debug: bool = Form(False),
):
    try:
        file_bytes = await file.read()
        if not file_bytes:
            raise HTTPException(status_code=400, detail="The uploaded file is empty.")

        try:
            saved = save_upload(file_bytes, file.filename, UPLOAD_DIR)
        except UnsupportedFileError as exc:
            raise HTTPException(status_code=400, detail=str(exc))

        tesseract_lang = tesseract_lang_for(language)
        job_id = saved["job_id"]

        pages, raw_text = _extract_pages_for_upload(saved["stored_path"], saved["file_type"], tesseract_lang)

        debug_payload = None

        if pages is not None:
            # Coordinate-aware path: images and PDFs. Column/line/block
            # grouping happens from real coordinates - see block_builder.py.
            has_any_words = any(p["words"] for p in pages)
            if not has_any_words:
                raise HTTPException(
                    status_code=422,
                    detail="No readable text could be extracted from this document."
                )

            blocks, debug_info = build_mcq_blocks(pages)
            blocks = _apply_text_cleanup_to_blocks(blocks)
            parsed = format_blocks(blocks)

            if debug:
                debug_payload = debug_info
                image_urls = []
                annotated_count = 0
                for page in pages:
                    if page["pil_image"] is None or annotated_count >= MAX_DEBUG_ANNOTATED_PAGES:
                        continue
                    page_debug = next(
                        (p for p in debug_info["pages"] if p["page_number"] == page["page_number"]),
                        None,
                    )
                    if not page_debug or not page_debug["blocks"]:
                        continue
                    annotated = _draw_debug_annotations(page["pil_image"], page_debug["blocks"])
                    out_name = f"{job_id}_debug_p{page['page_number']}.png"
                    annotated.save(os.path.join(OUTPUT_DIR, out_name))
                    image_urls.append(f"/download/debug-image/{job_id}/{page['page_number']}")
                    annotated_count += 1
                debug_payload["annotated_image_urls"] = image_urls

        else:
            # Text-only fallback: TXT/DOCX, no coordinates available.
            cleaned = clean_text(raw_text)
            if not cleaned:
                raise HTTPException(
                    status_code=422,
                    detail="No readable text could be extracted from this document."
                )
            parsed = parse_mcqs(cleaned)

        sample_text = _sample_text_for_language_detection(parsed)
        languages = _languages_for_report(language, sample_text)

        result = build_result_dict(
            document_name=saved["original_filename"],
            file_format=FILE_TYPE_LABELS.get(saved["file_type"], saved["file_type"]),
            languages=languages,
            parsed=parsed,
        )

        write_json_file(result, OUTPUT_DIR, job_id)
        write_docx_file(result, OUTPUT_DIR, job_id)

        result["job_id"] = job_id
        result["json_download_url"] = f"/download/json/{job_id}"
        result["docx_download_url"] = f"/download/docx/{job_id}"
        if debug_payload is not None:
            result["debug"] = debug_payload

        return JSONResponse(content=result)

    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001 - never let the app crash on bad input
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=f"Processing failed: {exc}")


@app.get("/download/json/{filename}")
def download_json(filename: str):
    safe_name = os.path.basename(filename)
    path = os.path.join(OUTPUT_DIR, f"{safe_name}.json")
    if not os.path.isfile(path):
        raise HTTPException(status_code=404, detail="JSON file not found.")
    return FileResponse(
        path, media_type="application/json",
        filename=f"mcqs_{safe_name}.json",
    )


@app.get("/download/docx/{filename}")
def download_docx(filename: str):
    safe_name = os.path.basename(filename)
    path = os.path.join(OUTPUT_DIR, f"{safe_name}.docx")
    if not os.path.isfile(path):
        raise HTTPException(status_code=404, detail="DOCX file not found.")
    return FileResponse(
        path,
        media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        filename=f"mcqs_{safe_name}.docx",
    )


@app.get("/download/debug-image/{filename}/{page_number}")
def download_debug_image(filename: str, page_number: int):
    """Debug mode (requirement I): an annotated page image showing the
    detected question/option block boundaries, color-coded by status
    (green=complete, orange=incomplete, red=needs review), so a person
    can visually verify why a document was parsed a certain way."""
    safe_name = os.path.basename(filename)
    path = os.path.join(OUTPUT_DIR, f"{safe_name}_debug_p{page_number}.png")
    if not os.path.isfile(path):
        raise HTTPException(status_code=404, detail="Debug image not found.")
    return FileResponse(path, media_type="image/png", filename=f"debug_{safe_name}_p{page_number}.png")


@app.get("/languages")
def get_languages():
    return {key: val["label"] for key, val in LANGUAGE_OPTIONS.items()}


# Serve the frontend last so /upload and /download keep priority.
app.mount("/", StaticFiles(directory=FRONTEND_DIR, html=True), name="frontend")
