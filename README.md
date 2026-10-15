# Employee Attendance and Analytics API

A FastAPI service backed by MongoDB for employee records, attendance punches, manual corrections, and attendance analytics.

## Requirements

- Python 3.11 or newer
- MongoDB 6.0 or newer

## Run locally

From this directory, install the dependencies and set the MongoDB connection values in PowerShell:

```powershell
python -m pip install -r requirements.txt
$env:MONGO_URI = "mongodb://localhost:27017"
$env:MONGO_DB = "attendance_db"
uvicorn app.main:app --port 8000
```

The API creates its indexes on startup. Check readiness at `http://127.0.0.1:8000/health`; interactive API documentation is available at `http://127.0.0.1:8000/docs`.

## Run the regression checks

With MongoDB running locally, open a second PowerShell window in this directory and run:

```powershell
python tests/test_api.py
```

The test runner uses a randomly named database and drops that database when it finishes. Point `MONGO_URI` only at a local or disposable MongoDB instance when running these checks.

## Submission contents

The required files are `app/main.py`, `requirements.txt`, `REVIEW.md`, `DECISIONS.md`, and this README. `tests/` and `TEST_REPORT.md` are included as supporting verification materials. Do not commit credentials, `.env` files, virtual environments, or data dumps.
