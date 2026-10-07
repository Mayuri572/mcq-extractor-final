"""
json_generator.py
Builds the final MCQ result structure and writes it as a UTF-8 JSON file.
"""

import json
import os

OPTION_LETTERS = ["A", "B", "C", "D"]


def build_result_dict(document_name: str, file_format: str, languages: list,
                      parsed: dict) -> dict:
    questions = []

    for q in parsed["questions"]:
        options = q["options"]

        # Supports both list and dictionary option formats
        if isinstance(options, dict):
            formatted_options = {
                "A": options.get("A", ""),
                "B": options.get("B", ""),
                "C": options.get("C", ""),
                "D": options.get("D", ""),
            }
        else:
            formatted_options = {
                OPTION_LETTERS[i]: options[i]
                for i in range(min(len(options), 4))
            }

        questions.append({
            "question_number": q["question_number"],
            "question": q["question"],
            "options": formatted_options,
        })

    return {
        "document_name": document_name,
        "format": file_format,
        "languages": languages,
        "total_questions": len(questions),
        "incomplete_count": parsed.get("incomplete_count", 0),
        "incomplete_questions": parsed.get("incomplete_questions", []),
        "needs_review_count": parsed.get("needs_review_count", 0),
        "needs_review_questions": parsed.get("needs_review_questions", []),
        "questions": questions,
    }


def write_json_file(result: dict, output_dir: str, job_id: str) -> str:
    os.makedirs(output_dir, exist_ok=True)

    out_path = os.path.join(output_dir, f"{job_id}.json")

    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)

    return out_path