# 📄 Academic OCR — Structured Document Extraction

> Extract structured JSON data from academic marksheets and certificates using **Google Gemini**.

Built for [180 Degrees Consulting](https://180dc.org/) × Lodestar.

---

## ✨ Features

- **Dual document support** — Classifies input as `marksheet` or `certificate` and extracts kind-specific fields.
- **Strict extraction rules** — Only picks final/total marks, never theory or practical sub-scores. Treats `XX`, `AB`, `--` as `null`. Never assumes `maxScore = 100`.
- **Image quality gate** — Rejects blurry uploads before wasting a Gemini API call (Laplacian-variance blur detection).
- **SHA-256 dedup cache** — Identical files return cached results instantly.
- **Retry with back-off** — Transient Gemini failures (quota bursts, 503s) are retried up to 3 times.
- **Automatic cleanup** — Uploaded files are deleted from Google servers after extraction.
- **`needs_review` flag** — Low-confidence or incomplete extractions are flagged for human review.
- **Rate limiting** — Per-key in-memory rate limiter to protect Gemini quota.
- **Dark-theme frontend** — Single-file HTML UI with drag-and-drop, preview, and formatted JSON output.

---

## 📁 Project Structure

```
180DC_Lodestar-OCR/
│
├── academic_ocr/              # Core Python package
│   ├── __init__.py            # Package init, re-exports
│   ├── api.py                 # FastAPI app, routes, CORS, env bootstrap
│   ├── auth.py                # API-key store & validation dependency
│   ├── extractor.py           # AcademicExtractor (Gemini client wrapper)
│   ├── prompt.py              # SYSTEM_PROMPT — extraction rules for Gemini
│   ├── schemas.py             # Pydantic request/response models
│   ├── utils.py               # File validation, blur detection, hashing
│   ├── job_queue.py           # Background worker pool (ThreadPoolExecutor)
│   ├── ratelimit.py           # In-memory per-key rate limiter
│   ├── metrics.py             # Prometheus metrics endpoint (optional)
│   ├── exceptions.py          # Custom exception hierarchy
│   └── main.py                # CLI runner for standalone extraction
│
├── frontend.html              # Single-file dark-theme test UI
├── requirements.txt           # Python dependencies
├── .env.example               # Template for required env vars
├── .gitignore                 # Excludes .env, __pycache__, etc.
└── README.md                  # ← You are here
```

---

## 🚀 Quick Start

### 1. Clone the repo

```bash
git clone https://github.com/Nithin031/180DC_Lodestar-OCR.git
cd 180DC_Lodestar-OCR
```

### 2. Create a virtual environment

```bash
# Using conda
conda create -n academic-ocr python=3.10 -y
conda activate academic-ocr

# Or using venv
python -m venv venv
source venv/bin/activate   # Linux/macOS
venv\Scripts\activate      # Windows
```

### 3. Install dependencies

```bash
pip install -r requirements.txt
```

### 4. Configure environment variables

```bash
cp .env.example .env
```

Edit `.env` with your values:

```env
# Get your key at https://aistudio.google.com/apikey
GOOGLE_API_KEY=your-google-api-key-here

# Shared secret for X-API-Key header authentication
API_KEY=mysecretkey123
```

### 5. Start the server

```bash
uvicorn academic_ocr.api:app --reload
```

The API will be available at `http://127.0.0.1:8000`.

### 6. Open the frontend

Open `frontend.html` in any browser. Upload a marksheet or certificate image/PDF and click **Extract**.

---

## 🔌 API Reference

### `POST /extract`

Extract structured data from an uploaded document.

| Parameter | Location | Required | Description |
|-----------|----------|----------|-------------|
| `X-API-Key` | Header | ✅ | API key (must match `API_KEY` in `.env`) |
| `file` | Body (multipart) | ✅ | Image (JPG, PNG, WEBP, HEIC) or PDF |

**Success Response (200):**

```json
{
  "kind": "marksheet",
  "title": "Senior School Certificate Examination",
  "exam_type": "final",
  "academicRecord": {
    "gradingMode": "percentage",
    "percentage": 72.6,
    "sgpa": null,
    "cgpa": null,
    "subjects": [
      {
        "subject": "English",
        "score": "78",
        "maxScore": "100",
        "grade": null,
        "gradingType": "marks",
        "confidence": 1.0
      }
    ]
  },
  "tags": ["CBSE", "Class 12", "2024"],
  "needs_review": false,
  "processing_ms": 4523,
  "cached": false
}
```

**Error Responses:**

| Status | Meaning |
|--------|---------|
| `401` | Invalid or missing API key |
| `422` | Invalid file (wrong type, too large, too blurry) |
| `502` | Gemini API failure (bad key, quota exceeded) |
| `500` | Internal server error |

### `POST /extract/async`

Same as `/extract` but returns immediately with a `job_id`. Poll `/result/{job_id}` for the result.

### `GET /result/{job_id}`

Retrieve the result of an async extraction job.

### `GET /health`

Health check endpoint (no authentication required).

### `GET /metrics`

Prometheus-compatible metrics (optional).

---

## 🧠 How It Works

```
User uploads document
        │
        ▼
   ┌─────────┐
   │  api.py  │  ← Validates X-API-Key, accepts multipart upload
   └────┬─────┘
        │
        ▼
┌───────────────┐
│ extractor.py  │  ← AcademicExtractor orchestrates the pipeline:
│               │     1. Validate file (type, size)
│               │     2. Blur detection (Laplacian variance)
│               │     3. SHA-256 cache check
│               │     4. Upload to Gemini
│               │     5. Generate with SYSTEM_PROMPT
│               │     6. Parse JSON response
│               │     7. Cleanup uploaded file
│               │     8. Cache result
└───────┬───────┘
        │
        ▼
  ┌────────────┐
  │ prompt.py  │  ← Strict extraction rules:
  │            │     • Only final/total column
  │            │     • XX/AB/-- → null
  │            │     • Never assume maxScore = 100
  │            │     • Never hallucinate values
  └────────────┘
```

---

## ⚙️ Configuration

| Env Variable | Required | Default | Description |
|-------------|----------|---------|-------------|
| `GOOGLE_API_KEY` | ✅ | — | Google Gemini API key |
| `API_KEY` | ✅ | — | Server authentication key |
| `LOG_LEVEL` | ❌ | `INFO` | Logging verbosity (`DEBUG`, `INFO`, `WARNING`) |

**Model selection** is controlled in `extractor.py`:

```python
_DEFAULT_MODEL: str = "gemini-2.5-flash"
```

To change the model, pass it when creating the extractor:

```python
extractor = AcademicExtractor(api_key=key, model="gemini-2.5-pro")
```

---

## 🧪 CLI Usage

You can also run extraction from the command line:

```bash
python -m academic_ocr.main path/to/marksheet.jpg
```

Results are printed to stdout and saved to `academic_ocr/sample_outputs/`.

---

## 🛡️ Security Notes

- **Never commit `.env`** — it's in `.gitignore`. Share `.env.example` instead.
- The `X-API-Key` is a shared secret. For production, replace the static key store in `auth.py` with a database or JWT-based authentication.
- Rotate your `GOOGLE_API_KEY` if it was ever exposed in git history.

---

## 🔧 Extending the Project

| Area | How to extend |
|------|---------------|
| **New document types** | Add classification rules and output schema to `prompt.py` and `schemas.py` |
| **Authentication** | Replace `_KEY_STORE` dict in `auth.py` with a database lookup |
| **Rate limiting** | Tune `MAX_REQUESTS_PER_MINUTE` in `ratelimit.py` or switch to Redis |
| **Background jobs** | `job_queue.py` uses threads; swap for Celery/RQ for heavy load |
| **Monitoring** | Enable `/metrics` endpoint and point Prometheus at it |
| **Testing** | Mock `google.genai` in unit tests; use FastAPI `TestClient` for integration |

---

## 📋 Requirements

- Python 3.10+
- A valid [Google Gemini API key](https://aistudio.google.com/apikey)
- Dependencies listed in `requirements.txt`:
  - `google-genai>=1.0.0`
  - `fastapi>=0.115.0`
  - `uvicorn[standard]>=0.30.0`
  - `python-dotenv>=1.0.0`
  - `Pillow>=10.0.0`
  - `python-multipart>=0.0.9`

---

## 👥 Team

Built by the **180DC × Lodestar** team.

## 📄 License

Internal use — 180 Degrees Consulting.
