# Employee Attendance & Analytics API

A FastAPI and MongoDB backend service for managing employee attendance logs, handling punch-in/out business rules, auditing manual regularizations, and performing departmental attendance analytics.

Built for the HROne Engineering Technical Assignment.

---

## Requirements
- Python 3.11+
- MongoDB 6.0+ (Tested with MongoDB 7.0 / Atlas)

---

## Local Setup

1. **Create and activate virtual environment:**
   ```bash
   python -m venv .venv
   source .venv/bin/activate
   ```

2. **Install dependencies:**
   ```bash
   pip install -r requirements.txt
   ```

3. **Configure environment:**
   Create a `.env` file (or set environment variables):
   ```env
   MONGO_URI=mongodb://localhost:27017
   MONGO_DB=attendance_db
   ```

4. **Seed sample data (optional):**
   ```bash
   python sample_seed.py
   ```

5. **Run the API server:**
   ```bash
   uvicorn app.main:app --port 8000
   ```
   Interactive OpenAPI documentation is available at `http://localhost:8000/docs`.

---

## Repository Structure
- `app/main.py`: Complete FastAPI application containing all endpoints, business rules (R1–R10), concurrency protections, and aggregation pipelines.
- `requirements.txt`: Python package dependencies.
- `REVIEW.md`: Comprehensive defect log and review of the starter codebase.
- `DECISIONS.md`: Design and architecture rationales covering indexes, race safety, leaderboard ranking, headcount calculation, and 100x scaling.
- `openapi.yaml`: Formal API specification and contract.
- `DATA_MODEL.md`: MongoDB schema specifications and sample document formats.
