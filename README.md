# MCQ Extractor

A web app that takes an uploaded question paper (image, PDF, TXT or DOCX —
in English, Hindi, Marathi, or a mix) and extracts every question with its
four options, in the document's natural reading order, then offers the
result as a downloadable JSON or DOCX file.

## How it works

```
Upload → identify file type → extract text / OCR → detect reading order
       → parse questions & options (rule-based) → validate
       → generate JSON + DOCX → display results
```

- **PDF**: text is extracted directly with PyMuPDF. If a page yields
  little or no usable text (e.g. a scanned page), that page is rasterized
  and run through Tesseract OCR instead.
- **Images (JPG/JPEG/PNG)**: OCR'd directly with Tesseract.
- **TXT**: read as-is.
- **DOCX**: text extracted in document order with `python-docx`.
- **Layout**: a simple coordinate-based check (`backend/layout_detector.py`)
  looks at text block bounding boxes to tell one-column pages from
  two-column pages, and reorders the text (left column top-to-bottom, then
  right column top-to-bottom) before parsing. No ML is used for this.
- **Parsing**: `backend/mcq_parser.py` is a regex + state-machine parser.
  It recognizes several question-numbering styles (`1.`, `Q1)`, `Question 1`,
  `प्रश्न १.`, ...) and several option styles (`A.`, `(a)`, `अ.`, `(अ)`, ...),
  and stitches multi-line questions/options back together. A question is
  only kept if it has non-empty text and exactly 4 options; anything else
  is reported as "incomplete" rather than shown or crashing the app.

## Project structure

```
mcq-extractor/
├── backend/
│   ├── main.py            FastAPI app + endpoints, serves the frontend
│   ├── file_handler.py    upload saving, file-type detection
│   ├── pdf_extractor.py   PyMuPDF text extraction + page rasterization
│   ├── ocr_engine.py      pytesseract OCR (layout-aware)
│   ├── docx_extractor.py  python-docx text extraction
│   ├── text_cleaner.py    text normalization helpers
│   ├── layout_detector.py one/two column detection & reading order
│   ├── mcq_parser.py      rule-based question/option parser
│   ├── language_utils.py  lightweight language detection for reporting
│   ├── json_generator.py  builds the result dict + writes JSON
│   └── docx_generator.py  writes the result DOCX
├── frontend/
│   ├── index.html
│   ├── style.css
│   └── script.js
├── uploads/                temporary uploaded files
├── outputs/                generated JSON/DOCX files
└── requirements.txt
```

## API

| Method | Path                          | Description                          |
|--------|-------------------------------|---------------------------------------|
| POST   | `/upload`                     | Upload a document (`file`, `language`), returns extracted MCQs + download URLs |
| GET    | `/download/json/{job_id}`     | Download the generated JSON           |
| GET    | `/download/docx/{job_id}`     | Download the generated DOCX           |
| GET    | `/languages`                  | List supported language-selector keys |

## Setup

### 1. System dependency: Tesseract OCR

Install Tesseract with the English, Hindi and Marathi language packs.

**Windows**: install from https://github.com/UB-Mannheim/tesseract/wiki,
selecting Hindi and Marathi under "Additional language data" during setup.

**macOS**:
```
brew install tesseract tesseract-lang
```

**Ubuntu/Debian**:
```
sudo apt-get install tesseract-ocr tesseract-ocr-hin tesseract-ocr-mar
```

Verify it's on PATH:
```
tesseract --version
```

### 2. Python packages

```
pip install -r requirements.txt
```

### 3. Run the app

```
uvicorn backend.main:app --reload --app-dir .
```

Then open **http://127.0.0.1:8000** in your browser.

## Notes / limitations

- This is a rule-based parser (regex + state machine), not an AI model —
  it works reliably on typically-formatted question papers but may miss
  unusual numbering/option styles.
- Column detection is coordinate-based and tuned for the common one- or
  two-column exam-paper layout; highly irregular layouts may not reorder
  perfectly.
- Hindi vs. Marathi (both written in Devanagari) are distinguished for the
  "Detected Language(s)" summary using the lightweight `langdetect`
  library; this is a best-effort label and does not affect parsing itself.
