(() => {
  "use strict";

  const screenUpload = document.getElementById("screen-upload");
  const screenProcessing = document.getElementById("screen-processing");
  const screenResults = document.getElementById("screen-results");

  const dropzone = document.getElementById("dropzone");
  const fileInput = document.getElementById("file-input");
  const dzEmpty = document.getElementById("dropzone-empty");
  const dzFile = document.getElementById("dropzone-file");
  const fileNameEl = document.getElementById("file-name");
  const fileSizeEl = document.getElementById("file-size");
  const removeFileBtn = document.getElementById("remove-file");

  const uploadForm = document.getElementById("upload-form");
  const extractBtn = document.getElementById("extract-btn");
  const languageSelect = document.getElementById("language-select");
  const uploadError = document.getElementById("upload-error");

  const processingStepEl = document.getElementById("processing-step");

  const summaryTotal = document.getElementById("summary-total");
  const summaryLanguages = document.getElementById("summary-languages");
  const summaryFormat = document.getElementById("summary-format");
  const summaryIncomplete = document.getElementById("summary-incomplete");
  const questionsList = document.getElementById("questions-list");
  const incompleteSection = document.getElementById("incomplete-section");
  const incompleteList = document.getElementById("incomplete-list");
  const needsReviewSection = document.getElementById("needs-review-section");
  const needsReviewList = document.getElementById("needs-review-list");
  const downloadJsonBtn = document.getElementById("download-json");
  const downloadDocxBtn = document.getElementById("download-docx");
  const restartBtn = document.getElementById("restart-btn");

  let selectedFile = null;
  let lastResult = null;

  const PROCESSING_STEPS = [
    "Processing document...",
    "Extracting text...",
    "Analyzing document layout...",
    "Extracting questions and options...",
    "Preparing results...",
  ];

  // ---------- helpers ----------

  function formatSize(bytes) {
    if (bytes < 1024) return `${bytes} B`;
    if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
    return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
  }

  function showScreen(screen) {
    [screenUpload, screenProcessing, screenResults].forEach((s) => {
      s.hidden = s !== screen;
    });
  }

  function setFile(file) {
    selectedFile = file;
    hideError();
    if (!file) {
      dzEmpty.hidden = false;
      dzFile.hidden = true;
      extractBtn.disabled = true;
      return;
    }
    dzEmpty.hidden = true;
    dzFile.hidden = false;
    fileNameEl.textContent = file.name;
    fileSizeEl.textContent = formatSize(file.size);
    extractBtn.disabled = false;
  }

  function showError(message) {
    uploadError.textContent = message;
    uploadError.hidden = false;
  }

  function hideError() {
    uploadError.hidden = true;
    uploadError.textContent = "";
  }

  // ---------- dropzone interactions ----------

  dropzone.addEventListener("click", () => fileInput.click());

  dropzone.addEventListener("keydown", (e) => {
    if (e.key === "Enter" || e.key === " ") {
      e.preventDefault();
      fileInput.click();
    }
  });
  dropzone.setAttribute("tabindex", "0");
  dropzone.setAttribute("role", "button");
  dropzone.setAttribute("aria-label", "Upload a document");

  fileInput.addEventListener("change", () => {
    if (fileInput.files && fileInput.files[0]) {
      setFile(fileInput.files[0]);
    }
  });

  ["dragenter", "dragover"].forEach((evt) => {
    dropzone.addEventListener(evt, (e) => {
      e.preventDefault();
      e.stopPropagation();
      dropzone.classList.add("drag-over");
    });
  });

  ["dragleave", "drop"].forEach((evt) => {
    dropzone.addEventListener(evt, (e) => {
      e.preventDefault();
      e.stopPropagation();
      dropzone.classList.remove("drag-over");
    });
  });

  dropzone.addEventListener("drop", (e) => {
    const dt = e.dataTransfer;
    if (dt && dt.files && dt.files[0]) {
      setFile(dt.files[0]);
    }
  });

  removeFileBtn.addEventListener("click", (e) => {
    e.stopPropagation();
    fileInput.value = "";
    setFile(null);
  });

  // ---------- processing animation ----------

  let processingTimer = null;

  function startProcessingAnimation() {
    let i = 0;
    processingStepEl.textContent = PROCESSING_STEPS[0];
    processingTimer = setInterval(() => {
      i = Math.min(i + 1, PROCESSING_STEPS.length - 1);
      processingStepEl.textContent = PROCESSING_STEPS[i];
    }, 1100);
  }

  function stopProcessingAnimation() {
    if (processingTimer) {
      clearInterval(processingTimer);
      processingTimer = null;
    }
  }

  // ---------- submit ----------

  uploadForm.addEventListener("submit", async (e) => {
    e.preventDefault();
    if (!selectedFile) return;

    hideError();
    showScreen(screenProcessing);
    startProcessingAnimation();

    const formData = new FormData();
    formData.append("file", selectedFile);
    formData.append("language", languageSelect.value);

    try {
      const res = await fetch("/upload", { method: "POST", body: formData });
      const data = await res.json().catch(() => ({}));

      if (!res.ok) {
        throw new Error(data.detail || "Something went wrong while processing the document.");
      }

      lastResult = data;
      renderResults(data);
      stopProcessingAnimation();
      showScreen(screenResults);
    } catch (err) {
      stopProcessingAnimation();
      showScreen(screenUpload);
      showError(err.message || "Something went wrong. Please try again.");
    }
  });

  // ---------- results rendering ----------

  function renderResults(data) {
    summaryTotal.textContent = data.total_questions ?? 0;
    summaryLanguages.textContent = (data.languages || []).join(", ") || "—";
    summaryFormat.textContent = data.format || "—";

    const incomplete = data.incomplete_count || 0;
    if (incomplete > 0) {
      summaryIncomplete.hidden = false;
      summaryIncomplete.textContent =
        `${incomplete} question${incomplete === 1 ? "" : "s"} could not be completely extracted and ${incomplete === 1 ? "was" : "were"} left out of the results below. See the "Incomplete questions" section for details.`;
    } else {
      summaryIncomplete.hidden = true;
    }

    questionsList.innerHTML = "";
    const questions = data.questions || [];

    if (questions.length === 0) {
      const empty = document.createElement("p");
      empty.className = "summary-note";
      empty.style.color = "var(--error)";
      empty.textContent = "No complete MCQs could be extracted from this document.";
      questionsList.appendChild(empty);
    }

    questions.forEach((q) => {
      questionsList.appendChild(
        buildQuestionCard(`QUESTION ${q.question_number}`, q.question, q.options)
      );
    });

    const incompleteQuestions = data.incomplete_questions || [];
    incompleteList.innerHTML = "";
    if (incompleteQuestions.length > 0) {
      incompleteSection.hidden = false;
      incompleteQuestions.forEach((q, idx) => {
        const missingNote = (q.missing || []).join(", ");
        const card = buildQuestionCard(
          `INCOMPLETE ${idx + 1}${missingNote ? ` — missing: ${missingNote}` : ""}`,
          q.question || "(no question text found)",
          q.options
        );
        card.classList.add("question-card--incomplete");
        incompleteList.appendChild(card);
      });
    } else {
      incompleteSection.hidden = true;
    }

    const needsReviewQuestions = data.needs_review_questions || [];
    needsReviewList.innerHTML = "";
    if (needsReviewQuestions.length > 0) {
      needsReviewSection.hidden = false;
      needsReviewQuestions.forEach((q, idx) => {
        const reasonNote = (q.reasons || []).join("; ");
        const card = buildQuestionCard(
          `NEEDS REVIEW ${idx + 1}${reasonNote ? ` — ${reasonNote}` : ""}`,
          q.question || "(no question text found)",
          q.options
        );
        card.classList.add("question-card--incomplete");
        needsReviewList.appendChild(card);
      });
    } else {
      needsReviewSection.hidden = true;
    }
  }

  function buildQuestionCard(label, questionText, options) {
    const card = document.createElement("div");
    card.className = "question-card";

    const numEl = document.createElement("div");
    numEl.className = "question-number";
    numEl.textContent = label;

    const textEl = document.createElement("p");
    textEl.className = "question-text";
    textEl.textContent = questionText;

    const optsEl = document.createElement("div");
    optsEl.className = "options-list";

    const letters = ["A", "B", "C", "D"];
    letters.forEach((letter) => {
      if (options && options[letter] !== undefined) {
        const row = document.createElement("div");
        row.className = "option-row";
        const letterSpan = document.createElement("span");
        letterSpan.className = "option-letter";
        letterSpan.textContent = `${letter}.`;
        const textSpan = document.createElement("span");
        textSpan.textContent = options[letter] || "—";
        row.appendChild(letterSpan);
        row.appendChild(textSpan);
        optsEl.appendChild(row);
      }
    });

    card.appendChild(numEl);
    card.appendChild(textEl);
    card.appendChild(optsEl);
    return card;
  }

  downloadJsonBtn.addEventListener("click", () => {
    if (lastResult && lastResult.json_download_url) {
      window.location.href = lastResult.json_download_url;
    }
  });

  downloadDocxBtn.addEventListener("click", () => {
    if (lastResult && lastResult.docx_download_url) {
      window.location.href = lastResult.docx_download_url;
    }
  });

  restartBtn.addEventListener("click", () => {
    fileInput.value = "";
    setFile(null);
    lastResult = null;
    incompleteSection.hidden = true;
    incompleteList.innerHTML = "";
    needsReviewSection.hidden = true;
    needsReviewList.innerHTML = "";
    showScreen(screenUpload);
  });
})();
