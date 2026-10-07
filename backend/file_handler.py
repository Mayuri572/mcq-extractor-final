"""
file_handler.py
Handles saving uploads to disk and identifying file type.
"""

import os
import uuid

ALLOWED_EXTENSIONS = {
    ".jpg": "IMAGE",
    ".jpeg": "IMAGE",
    ".png": "IMAGE",
    ".pdf": "PDF",
    ".txt": "TXT",
    ".docx": "DOCX",
}


class UnsupportedFileError(Exception):
    pass


def get_file_type(filename: str) -> str:
    """Return a canonical type string for a filename, or raise UnsupportedFileError."""
    ext = os.path.splitext(filename or "")[1].lower()
    if ext not in ALLOWED_EXTENSIONS:
        raise UnsupportedFileError(
            f"Unsupported file type '{ext or 'unknown'}'. "
            f"Supported formats: JPG, JPEG, PNG, PDF, TXT, DOCX."
        )
    return ALLOWED_EXTENSIONS[ext]


def save_upload(file_bytes: bytes, original_filename: str, upload_dir: str) -> dict:
    """
    Save uploaded bytes to disk under a unique name while keeping the
    original extension. Returns metadata about the saved file.
    """
    os.makedirs(upload_dir, exist_ok=True)

    ext = os.path.splitext(original_filename or "")[1].lower()
    file_type = get_file_type(original_filename)

    job_id = uuid.uuid4().hex[:12]
    stored_name = f"{job_id}{ext}"
    stored_path = os.path.join(upload_dir, stored_name)

    with open(stored_path, "wb") as f:
        f.write(file_bytes)

    return {
        "job_id": job_id,
        "original_filename": original_filename,
        "stored_path": stored_path,
        "file_type": file_type,
        "size_bytes": len(file_bytes),
    }
