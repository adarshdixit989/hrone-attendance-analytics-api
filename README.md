# HROne — Employee Attendance & Analytics API

FastAPI + PyMongo implementation for the HROne Software Engineer Trainee assignment.

## Read first
1. `PROBLEM_STATEMENT.docx`
2. `openapi.yaml` — authoritative HTTP contract and rules R1–R10
3. `DATA_MODEL.md`
4. `app/main.py`
5. `REVIEW.md` and `DECISIONS.md`

## Setup
Requires Python 3.11+ and MongoDB 6.0+.

```bash
python -m venv .venv
# Windows: .venv\Scripts\activate
# macOS/Linux: source .venv/bin/activate
pip install -r requirements.txt
```

Set `MONGO_URI` and `MONGO_DB` as environment variables (or in a local `.env` file that must not be committed). For a local MongoDB container:

```bash
docker run --name hrone-mongo -p 27017:27017 -d mongo:7
```

Seed the provided sample data if desired:

```bash
python sample_seed.py
```

Run from the repository root:

```bash
uvicorn app.main:app --port 8000
```

Readiness: `GET http://localhost:8000/health`. Interactive API docs are at `/docs`.

## Before submission
- Run the app against MongoDB and test the contract in `openapi.yaml`, especially concurrency, overnight shifts, half-up rounding, ties, employees with no logs, and date ranges.
- Read every implementation line and edit `REVIEW.md` / `DECISIONS.md` into your own words so you can explain your decisions live.
- Commit only the required source/docs. Do not commit `.env`, secrets, virtual environments, or a Dockerfile.

