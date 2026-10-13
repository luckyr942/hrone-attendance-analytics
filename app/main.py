import calendar
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
import os
from typing import Any, Dict, List, Optional

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query, Path, status
from pydantic import BaseModel, Field, field_validator, model_validator
from pymongo import MongoClient, ReturnDocument
from pymongo.errors import DuplicateKeyError

load_dotenv()

MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017")
MONGO_DB = os.getenv("MONGO_DB", "attendance_db")

client = MongoClient(MONGO_URI, tz_aware=True)
db = client[MONGO_DB]

IST = timezone(timedelta(hours=5, minutes=30))


@asynccontextmanager
async def lifespan(app: FastAPI):
    # setup database indexes when the app starts
    try:
        db.employees.create_index([("emp_code", 1)], unique=True)
        db.employees.create_index([("department", 1), ("emp_code", 1)])
        db.employees.create_index([("department", 1), ("joined_on", 1)])
        db.employees.create_index([("joined_on", 1)])

        db.attendance_logs.create_index([("emp_code", 1), ("date", 1)], unique=True)
        db.attendance_logs.create_index([("date", -1), ("emp_code", 1)])
        db.attendance_logs.create_index([("emp_code", 1), ("date", -1)])
        db.attendance_logs.create_index([("status", 1), ("date", -1)])
        db.attendance_logs.create_index([("date", 1), ("emp_code", 1)])
        db.attendance_logs.create_index([("date", 1), ("late_minutes", 1)])
    except Exception:
        pass
    yield


app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0", lifespan=lifespan)


# simple time conversion helpers
def datetime_to_epoch_ms(time_value: Optional[datetime]) -> Optional[int]:
    """Converts a datetime into milliseconds since 1970."""
    if time_value is None:
        return None
    if time_value.tzinfo is None:
        time_value = time_value.replace(tzinfo=timezone.utc)
    return int(time_value.timestamp() * 1000)


def epoch_ms_to_datetime(epoch_ms: Optional[int]) -> Optional[datetime]:
    """Converts milliseconds into a UTC datetime, dropping fractional seconds."""
    if epoch_ms is None:
        return None
    seconds = epoch_ms // 1000
    return datetime.fromtimestamp(seconds, tz=timezone.utc)


def get_current_utc_time() -> datetime:
    """Returns current UTC time rounded down to whole seconds."""
    seconds = int(datetime.now(timezone.utc).timestamp())
    return datetime.fromtimestamp(seconds, tz=timezone.utc)


def round_half_up(number: Optional[float], decimal_places: int = 2) -> Optional[float]:
    """Rounds numbers using standard math half-up rounding."""
    if number is None:
        return None
    decimal_number = Decimal(str(number))
    multiplier = Decimal("10") ** -decimal_places
    return float(decimal_number.quantize(multiplier, rounding=ROUND_HALF_UP))


# attendance business rule helpers
def get_attendance_date(punch_in_time: datetime, shift_start: str, shift_end: str) -> str:
    """
    Finds the attendance date for a punch-in.
    For night shifts (ending next morning), punches before shift end belong to the previous day.
    """
    punch_in_ist = punch_in_time.astimezone(IST)
    start_hour, start_minute = map(int, shift_start.split(":"))
    end_hour, end_minute = map(int, shift_end.split(":"))

    is_overnight = (end_hour, end_minute) <= (start_hour, start_minute)
    if is_overnight:
        if (punch_in_ist.hour, punch_in_ist.minute, punch_in_ist.second) < (end_hour, end_minute, 0):
            return (punch_in_ist.date() - timedelta(days=1)).isoformat()

    return punch_in_ist.date().isoformat()


def calculate_late_minutes(punch_in_time: datetime, attendance_date: str, shift_start: str) -> int:
    """
    Calculates late minutes.
    Employees have a 10-minute grace window. If later than 10 minutes,
    we count all whole minutes since shift start.
    """
    punch_in_ist = punch_in_time.astimezone(IST)
    calendar_day = date.fromisoformat(attendance_date)
    start_hour, start_minute = map(int, shift_start.split(":"))
    shift_start_ist = datetime(calendar_day.year, calendar_day.month, calendar_day.day, start_hour, start_minute, 0, tzinfo=IST)

    seconds_late = int((punch_in_ist - shift_start_ist).total_seconds())
    if seconds_late > 600:
        return int(seconds_late // 60)
    return 0


def calculate_overtime_minutes(punch_out_time: datetime, attendance_date: str, shift_start: str, shift_end: str) -> int:
    """
    Calculates overtime minutes.
    Only counts if overtime is at least 30 minutes.
    """
    punch_out_ist = punch_out_time.astimezone(IST)
    calendar_day = date.fromisoformat(attendance_date)
    start_hour, start_minute = map(int, shift_start.split(":"))
    end_hour, end_minute = map(int, shift_end.split(":"))

    if (end_hour, end_minute) <= (start_hour, start_minute):
        shift_end_date = calendar_day + timedelta(days=1)
    else:
        shift_end_date = calendar_day

    shift_end_ist = datetime(shift_end_date.year, shift_end_date.month, shift_end_date.day, end_hour, end_minute, 0, tzinfo=IST)
    seconds_extra = int((punch_out_ist - shift_end_ist).total_seconds())
    minutes_extra = int(seconds_extra // 60)

    if minutes_extra >= 30:
        return minutes_extra
    return 0


def calculate_work_hours(punch_in_time: datetime, punch_out_time: datetime) -> float:
    """Calculates total hours between punch in and punch out rounded to 2 decimals."""
    total_seconds = int((punch_out_time - punch_in_time).total_seconds())
    hours = Decimal(total_seconds) / Decimal(3600)
    return float(hours.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def count_working_days(month_str: str, joined_date_str: Optional[str] = None) -> int:
    """Counts weekdays (Mon-Fri) in the month, starting from the employee join date."""
    year, month = map(int, month_str.split("-"))
    days_in_month = calendar.monthrange(year, month)[1]
    start_date = date(year, month, 1)
    end_date = date(year, month, days_in_month)

    if joined_date_str:
        joined_date = date.fromisoformat(joined_date_str)
        if joined_date > end_date:
            return 0
        if joined_date > start_date:
            start_date = joined_date

    weekday_count = 0
    current_day = start_date
    while current_day <= end_date:
        if current_day.weekday() < 5:  # Monday to Friday
            weekday_count += 1
        current_day += timedelta(days=1)
    return weekday_count


def format_attendance_response(attendance_doc: Dict[str, Any]) -> Dict[str, Any]:
    """Formats a database document into the clean attendance JSON response."""
    formatted_history = []
    for entry in attendance_doc.get("history") or []:
        formatted_changes = {}
        for field_name, change in (entry.get("changes") or {}).items():
            from_val = change.get("from")
            to_val = change.get("to")
            if field_name in ("punch_in", "punch_out"):
                from_val = datetime_to_epoch_ms(from_val) if isinstance(from_val, datetime) else from_val
                to_val = datetime_to_epoch_ms(to_val) if isinstance(to_val, datetime) else to_val
            formatted_changes[field_name] = {"from": from_val, "to": to_val}

        formatted_history.append({
            "at": datetime_to_epoch_ms(entry.get("at")),
            "by": entry.get("by"),
            "reason": entry.get("reason"),
            "changes": formatted_changes,
        })

    return {
        "emp_code": attendance_doc["emp_code"],
        "date": attendance_doc["date"],
        "status": attendance_doc["status"],
        "punch_in": datetime_to_epoch_ms(attendance_doc.get("punch_in")),
        "punch_out": datetime_to_epoch_ms(attendance_doc.get("punch_out")),
        "work_hours": attendance_doc.get("work_hours"),
        "late_minutes": attendance_doc.get("late_minutes", 0),
        "overtime_minutes": attendance_doc.get("overtime_minutes", 0),
        "half_day": attendance_doc.get("half_day", False),
        "history": formatted_history,
    }


# request models
class EmployeeCreate(BaseModel):
    emp_code: str = Field(pattern=r"^EMP\d{4,6}$")
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$", max_length=120)
    department: str = Field(min_length=1, max_length=50)
    shift_start: str = Field(default="09:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    shift_end: str = Field(default="18:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    joined_on: str = Field(pattern=r"^\d{4}-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])$")

    @model_validator(mode="after")
    def validate_employee(self):
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start must differ from shift_end")
        try:
            date.fromisoformat(self.joined_on)
        except ValueError:
            raise ValueError("joined_on must be a valid date")
        return self


class PunchInRequest(BaseModel):
    emp_code: str
    punched_at: Optional[int] = Field(default=None, ge=100000000000, le=4102444800000)
    status: str = Field(default="PRESENT", pattern=r"^(PRESENT|WFH|ON_DUTY)$")

    @field_validator("punched_at", mode="before")
    @classmethod
    def check_epoch(cls, v):
        if v is not None and (isinstance(v, bool) or not isinstance(v, int)):
            raise ValueError("punched_at must be an integer epoch milliseconds")
        return v


class PunchOutRequest(BaseModel):
    emp_code: str
    punched_at: Optional[int] = Field(default=None, ge=100000000000, le=4102444800000)

    @field_validator("punched_at", mode="before")
    @classmethod
    def check_epoch(cls, v):
        if v is not None and (isinstance(v, bool) or not isinstance(v, int)):
            raise ValueError("punched_at must be an integer epoch milliseconds")
        return v


class RegularizeRequest(BaseModel):
    status: Optional[str] = Field(default=None, pattern=r"^(PRESENT|ABSENT|LEAVE|WFH|ON_DUTY)$")
    punch_in: Optional[int] = Field(default=None, ge=100000000000, le=4102444800000)
    punch_out: Optional[int] = Field(default=None, ge=100000000000, le=4102444800000)
    reason: str = Field(min_length=5, max_length=200)
    regularized_by: str = Field(min_length=1, max_length=50)

    @field_validator("punch_in", "punch_out", mode="before")
    @classmethod
    def check_epoch(cls, v):
        if v is not None and (isinstance(v, bool) or not isinstance(v, int)):
            raise ValueError("Punch instant must be an integer epoch milliseconds")
        return v


# core endpoints
@app.get("/health")
def health():
    try:
        client.admin.command("ping")
        return {"status": "ok"}
    except Exception:
        raise HTTPException(status_code=503, detail="Database unavailable")


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeCreate):
    if db.employees.find_one({"emp_code": body.emp_code}):
        raise HTTPException(409, "emp_code already exists")

    doc = body.model_dump()
    doc["created_at"] = get_current_utc_time()

    try:
        db.employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "emp_code already exists")

    doc["created_at"] = datetime_to_epoch_ms(doc["created_at"])
    doc.pop("_id", None)
    return doc


@app.get("/employees")
def list_employees(
    department: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    q = {}
    if department:
        q["department"] = department

    total = db.employees.count_documents(q)
    skip = (page - 1) * page_size
    cursor = db.employees.find(q, {"_id": 0}).sort("emp_code", 1).skip(skip).limit(page_size)

    items = []
    for emp in cursor:
        emp["created_at"] = datetime_to_epoch_ms(emp.get("created_at"))
        items.append(emp)

    return {"items": items, "total": total, "page": page, "page_size": page_size}


@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInRequest):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(404, "Employee not found")

    punch_in_time = epoch_ms_to_datetime(body.punched_at) if body.punched_at else get_current_utc_time()
    attendance_date = get_attendance_date(punch_in_time, emp["shift_start"], emp["shift_end"])

    if db.attendance_logs.find_one({"emp_code": body.emp_code, "date": attendance_date}):
        raise HTTPException(409, "Already punched in for this date")

    late_mins = calculate_late_minutes(punch_in_time, attendance_date, emp["shift_start"])

    doc = {
        "emp_code": body.emp_code,
        "date": attendance_date,
        "status": body.status,
        "punch_in": punch_in_time,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": late_mins,
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }

    try:
        db.attendance_logs.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "Already punched in for this date")

    return format_attendance_response(doc)


@app.post("/attendance/punch-out")
def punch_out(body: PunchOutRequest):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(404, "Employee not found")

    punch_out_time = epoch_ms_to_datetime(body.punched_at) if body.punched_at else get_current_utc_time()

    record = db.attendance_logs.find_one(
        {"emp_code": body.emp_code, "punch_in": {"$lte": punch_out_time}},
        sort=[("punch_in", -1)],
    )

    if not record:
        raise HTTPException(404, "No matching punch-in found")

    if record.get("punch_out") is not None:
        raise HTTPException(409, "Record is already punched out")

    punch_in_time = record["punch_in"]
    if punch_out_time <= punch_in_time:
        raise HTTPException(422, "punched_at must be strictly greater than punch_in")

    if punch_out_time > punch_in_time + timedelta(hours=24):
        raise HTTPException(422, "punched_at cannot be more than 24 hours after punch_in")

    work_hours = calculate_work_hours(punch_in_time, punch_out_time)
    overtime_mins = calculate_overtime_minutes(punch_out_time, record["date"], emp["shift_start"], emp["shift_end"])
    half_day = work_hours < 4.50

    updated = db.attendance_logs.find_one_and_update(
        {"_id": record["_id"], "punch_out": None},
        {
            "$set": {
                "punch_out": punch_out_time,
                "work_hours": work_hours,
                "overtime_minutes": overtime_mins,
                "half_day": half_day,
            }
        },
        return_document=ReturnDocument.AFTER,
    )

    if not updated:
        raise HTTPException(409, "Record was already punched out concurrently")

    return format_attendance_response(updated)


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[str] = Query(None, pattern=r"^(PRESENT|ABSENT|LEAVE|WFH|ON_DUTY)$"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    if date_from and date_to and date_from > date_to:
        raise HTTPException(422, "date_from cannot be greater than date_to")

    q = {}
    if emp_code:
        q["emp_code"] = emp_code
    if date_from or date_to:
        q["date"] = {}
        if date_from:
            q["date"]["$gte"] = date_from
        if date_to:
            q["date"]["$lte"] = date_to
    if status:
        q["status"] = status

    total = db.attendance_logs.count_documents(q)
    skip = (page - 1) * page_size
    cursor = db.attendance_logs.find(q).sort([("date", -1), ("emp_code", 1)]).skip(skip).limit(page_size)

    items = [format_attendance_response(doc) for doc in cursor]
    return {"items": items, "total": total, "page": page, "page_size": page_size}


@app.patch("/attendance/{emp_code}/{date}")
def regularize_attendance(
    emp_code: str = Path(...),
    date_param: str = Path(..., alias="date", pattern=r"^\d{4}-\d{2}-\d{2}$"),
    body: RegularizeRequest = ...,
):
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(404, "Employee not found")

    record = db.attendance_logs.find_one({"emp_code": emp_code, "date": date_param})
    if not record:
        raise HTTPException(404, "Attendance record not found for this date")

    if body.status in ("ABSENT", "LEAVE") and (body.punch_in is not None or body.punch_out is not None):
        raise HTTPException(422, "Cannot provide punch times when regularizing to ABSENT or LEAVE")

    final_status = body.status if body.status is not None else record["status"]

    if final_status in ("ABSENT", "LEAVE"):
        new_punch_in = None
        new_punch_out = None
        new_work_hours = None
        new_late_minutes = 0
        new_overtime_minutes = 0
        new_half_day = False
    else:
        new_punch_in = epoch_ms_to_datetime(body.punch_in) if body.punch_in is not None else record.get("punch_in")
        if new_punch_in is None:
            raise HTTPException(422, "A presence status requires a valid punch_in")

        derived_date = get_attendance_date(new_punch_in, emp["shift_start"], emp["shift_end"])
        if derived_date != date_param:
            raise HTTPException(422, f"punch_in belongs to date {derived_date}, not record date {date_param}")

        new_late_minutes = calculate_late_minutes(new_punch_in, date_param, emp["shift_start"])

        new_punch_out = epoch_ms_to_datetime(body.punch_out) if body.punch_out is not None else record.get("punch_out")
        if new_punch_out is not None:
            if new_punch_out <= new_punch_in:
                raise HTTPException(422, "punch_out must be after punch_in")
            if new_punch_out > new_punch_in + timedelta(hours=24):
                raise HTTPException(422, "punch_out must be within 24 hours of punch_in")
            new_work_hours = calculate_work_hours(new_punch_in, new_punch_out)
            new_overtime_minutes = calculate_overtime_minutes(new_punch_out, date_param, emp["shift_start"], emp["shift_end"])
            new_half_day = new_work_hours < 4.50
        else:
            new_work_hours = None
            new_overtime_minutes = 0
            new_half_day = False

    old_punch_in = record.get("punch_in")
    old_punch_out = record.get("punch_out")
    old_work_hours = record.get("work_hours")
    old_late_minutes = record.get("late_minutes", 0)
    old_overtime_minutes = record.get("overtime_minutes", 0)
    old_half_day = record.get("half_day", False)
    old_status = record.get("status")

    changes_mongo = {}
    if final_status != old_status:
        changes_mongo["status"] = {"from": old_status, "to": final_status}
    if datetime_to_epoch_ms(new_punch_in) != datetime_to_epoch_ms(old_punch_in):
        changes_mongo["punch_in"] = {"from": old_punch_in, "to": new_punch_in}
    if datetime_to_epoch_ms(new_punch_out) != datetime_to_epoch_ms(old_punch_out):
        changes_mongo["punch_out"] = {"from": old_punch_out, "to": new_punch_out}
    if new_work_hours != old_work_hours:
        changes_mongo["work_hours"] = {"from": old_work_hours, "to": new_work_hours}
    if new_late_minutes != old_late_minutes:
        changes_mongo["late_minutes"] = {"from": old_late_minutes, "to": new_late_minutes}
    if new_overtime_minutes != old_overtime_minutes:
        changes_mongo["overtime_minutes"] = {"from": old_overtime_minutes, "to": new_overtime_minutes}
    if new_half_day != old_half_day:
        changes_mongo["half_day"] = {"from": old_half_day, "to": new_half_day}

    if not changes_mongo:
        raise HTTPException(422, "Request changes nothing")

    history_len = len(record.get("history") or [])
    audit_entry = {
        "at": get_current_utc_time(),
        "by": body.regularized_by,
        "reason": body.reason,
        "changes": changes_mongo,
    }

    updated = db.attendance_logs.find_one_and_update(
        {"_id": record["_id"], f"history.{history_len}": {"$exists": False}},
        {
            "$set": {
                "status": final_status,
                "punch_in": new_punch_in,
                "punch_out": new_punch_out,
                "work_hours": new_work_hours,
                "late_minutes": new_late_minutes,
                "overtime_minutes": new_overtime_minutes,
                "half_day": new_half_day,
            },
            "$push": {"history": audit_entry},
        },
        return_document=ReturnDocument.AFTER,
    )

    if not updated:
        raise HTTPException(409, "Concurrent modification conflict")

    return format_attendance_response(updated)


# analytics pipelines
def build_employee_monthly_pipeline(emp_code: str, month: str) -> List[Dict[str, Any]]:
    return [
        {
            "$match": {
                "emp_code": emp_code,
                "date": {"$gte": f"{month}-01", "$lte": f"{month}-31"},
            }
        },
        {
            "$project": {
                "status": 1,
                "late_minutes": {"$ifNull": ["$late_minutes", 0]},
                "overtime_minutes": {"$ifNull": ["$overtime_minutes", 0]},
                "half_day": {"$ifNull": ["$half_day", False]},
                "dayOfWeek": {
                    "$dayOfWeek": {
                        "$dateFromString": {"dateString": "$date"}
                    }
                },
            }
        },
        {
            "$group": {
                "_id": None,
                "leave_days": {
                    "$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}
                },
                "late_count": {
                    "$sum": {"$cond": [{"$gt": ["$late_minutes", 0]}, 1, 0]}
                },
                "total_late_minutes": {"$sum": "$late_minutes"},
                "total_overtime_minutes": {"$sum": "$overtime_minutes"},
                "present_days": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$in": ["$dayOfWeek", [2, 3, 4, 5, 6]]},
                                    {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                ]
                            },
                            {"$cond": ["$half_day", 0.5, 1.0]},
                            0.0,
                        ]
                    }
                },
            }
        },
    ]


@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(
    emp_code: str = Path(...),
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
):
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(404, "Employee not found")

    working_days = count_working_days(month, emp.get("joined_on"))
    pipeline = build_employee_monthly_pipeline(emp_code, month)
    results = list(db.attendance_logs.aggregate(pipeline))

    if results:
        agg = results[0]
        leave_days = agg.get("leave_days", 0)
        late_count = agg.get("late_count", 0)
        total_late = agg.get("total_late_minutes", 0)
        total_ot = agg.get("total_overtime_minutes", 0)
        present_days = round_half_up(agg.get("present_days", 0.0), 2)
    else:
        leave_days = 0
        late_count = 0
        total_late = 0
        total_ot = 0
        present_days = 0.0

    attendance_pct = None
    if working_days > 0:
        attendance_pct = round_half_up((present_days / working_days) * 100, 2)

    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working_days,
        "present_days": present_days,
        "leave_days": leave_days,
        "late_count": late_count,
        "total_late_minutes": total_late,
        "total_overtime_minutes": total_ot,
        "attendance_pct": attendance_pct,
    }


def build_department_summary_pipeline(month: str, department: Optional[str] = None) -> List[Dict[str, Any]]:
    year, m = map(int, month.split("-"))
    num_days = calendar.monthrange(year, m)[1]
    month_start = f"{month}-01"
    month_end = f"{month}-{num_days:02d}"

    match_stage = {"joined_on": {"$lte": month_end}}
    if department:
        match_stage["department"] = department

    return [
        {"$match": match_stage},
        {
            "$lookup": {
                "from": "attendance_logs",
                "let": {"code": "$emp_code"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$and": [
                                    {"$eq": ["$emp_code", "$$code"]},
                                    {"$gte": ["$date", month_start]},
                                    {"$lte": ["$date", month_end]},
                                ]
                            }
                        }
                    },
                    {
                        "$project": {
                            "status": 1,
                            "work_hours": 1,
                            "late_minutes": {"$ifNull": ["$late_minutes", 0]},
                            "overtime_minutes": {"$ifNull": ["$overtime_minutes", 0]},
                            "half_day": {"$ifNull": ["$half_day", False]},
                            "dayOfWeek": {
                                "$dayOfWeek": {
                                    "$dateFromString": {"dateString": "$date"}
                                }
                            },
                        }
                    },
                ],
                "as": "logs",
            }
        },
        {
            "$project": {
                "department": 1,
                "emp_present_days": {
                    "$sum": {
                        "$map": {
                            "input": "$logs",
                            "as": "l",
                            "in": {
                                "$cond": [
                                    {
                                        "$and": [
                                            {"$in": ["$$l.dayOfWeek", [2, 3, 4, 5, 6]]},
                                            {"$in": ["$$l.status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                        ]
                                    },
                                    {"$cond": ["$$l.half_day", 0.5, 1.0]},
                                    0.0,
                                ]
                            },
                        }
                    }
                },
                "emp_late_count": {
                    "$size": {
                        "$filter": {
                            "input": "$logs",
                            "as": "l",
                            "cond": {"$gt": ["$$l.late_minutes", 0]},
                        }
                    }
                },
                "emp_total_late_minutes": {"$sum": "$logs.late_minutes"},
                "emp_leave_count": {
                    "$size": {
                        "$filter": {
                            "input": "$logs",
                            "as": "l",
                            "cond": {"$eq": ["$$l.status", "LEAVE"]},
                        }
                    }
                },
                "emp_on_duty_count": {
                    "$size": {
                        "$filter": {
                            "input": "$logs",
                            "as": "l",
                            "cond": {"$eq": ["$$l.status", "ON_DUTY"]},
                        }
                    }
                },
                "emp_valid_work_hours": {
                    "$filter": {
                        "input": "$logs",
                        "as": "l",
                        "cond": {
                            "$and": [
                                {"$in": ["$$l.status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                {"$ne": ["$$l.work_hours", None]},
                            ]
                        },
                    }
                },
            }
        },
        {
            "$group": {
                "_id": "$department",
                "headcount": {"$sum": 1},
                "present_days": {"$sum": "$emp_present_days"},
                "late_count": {"$sum": "$emp_late_count"},
                "total_late_minutes": {"$sum": "$emp_total_late_minutes"},
                "leave_count": {"$sum": "$emp_leave_count"},
                "on_duty_count": {"$sum": "$emp_on_duty_count"},
                "work_hours_sum": {"$sum": {"$sum": "$emp_valid_work_hours.work_hours"}},
                "work_hours_count": {"$sum": {"$size": "$emp_valid_work_hours"}},
            }
        },
        {"$sort": {"_id": 1}},
        {
            "$project": {
                "_id": 0,
                "department": "$_id",
                "headcount": "$headcount",
                "present_days": {"$round": ["$present_days", 2]},
                "late_count": "$late_count",
                "total_late_minutes": "$total_late_minutes",
                "leave_count": "$leave_count",
                "on_duty_count": "$on_duty_count",
                "avg_work_hours": {
                    "$cond": [
                        {"$gt": ["$work_hours_count", 0]},
                        {"$round": [{"$divide": ["$work_hours_sum", "$work_hours_count"]}, 2]},
                        None,
                    ]
                },
            }
        },
    ]


@app.get("/analytics/departments/summary")
def department_summary(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: Optional[str] = None,
):
    pipeline = build_department_summary_pipeline(month, department)
    items = list(db.employees.aggregate(pipeline))
    return {"month": month, "items": items}


def build_late_leaderboard_pipeline(month: str, limit: int = 10, department: Optional[str] = None) -> List[Dict[str, Any]]:
    pipeline = [
        {
            "$match": {
                "date": {"$gte": f"{month}-01", "$lte": f"{month}-31"},
                "late_minutes": {"$gt": 0},
            }
        },
        {
            "$group": {
                "_id": "$emp_code",
                "total_late_minutes": {"$sum": "$late_minutes"},
                "late_count": {"$sum": 1},
            }
        },
        {"$match": {"total_late_minutes": {"$gt": 0}}},
        {
            "$lookup": {
                "from": "employees",
                "localField": "_id",
                "foreignField": "emp_code",
                "as": "emp",
            }
        },
        {"$unwind": "$emp"},
    ]

    if department:
        pipeline.append({"$match": {"emp.department": department}})

    pipeline.extend([
        {
            "$setWindowFields": {
                "sortBy": {"total_late_minutes": -1},
                "output": {
                    "rank": {
                        "$rank": {}
                    }
                },
            }
        },
        {"$match": {"rank": {"$lte": limit}}},
        {"$sort": {"total_late_minutes": -1, "_id": 1}},
        {
            "$project": {
                "_id": 0,
                "rank": "$rank",
                "emp_code": "$_id",
                "name": "$emp.name",
                "department": "$emp.department",
                "total_late_minutes": "$total_late_minutes",
                "late_count": "$late_count",
            }
        },
    ])
    return pipeline


@app.get("/analytics/leaderboard/late")
def late_leaderboard(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    limit: int = Query(10, ge=1, le=50),
    department: Optional[str] = None,
):
    pipeline = build_late_leaderboard_pipeline(month, limit, department)
    items = list(db.attendance_logs.aggregate(pipeline))
    return {"month": month, "items": items}


def build_department_trend_pipeline(department: str, from_date: str, to_date: str) -> List[Dict[str, Any]]:
    from_d = date.fromisoformat(from_date)
    to_d = date.fromisoformat(to_date)
    to_d_plus_1 = to_d + timedelta(days=1)

    from_dt = datetime(from_d.year, from_d.month, from_d.day, 0, 0, 0, tzinfo=timezone.utc)
    to_dt = datetime(to_d_plus_1.year, to_d_plus_1.month, to_d_plus_1.day, 0, 0, 0, tzinfo=timezone.utc)

    return [
        {
            "$match": {
                "date": {"$gte": from_date, "$lte": to_date},
            }
        },
        {
            "$lookup": {
                "from": "employees",
                "localField": "emp_code",
                "foreignField": "emp_code",
                "as": "emp",
            }
        },
        {"$unwind": "$emp"},
        {"$match": {"emp.department": department}},
        {
            "$group": {
                "_id": "$date",
                "present_count": {
                    "$sum": {
                        "$cond": [
                            {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                            {"$cond": ["$half_day", 0.5, 1.0]},
                            0.0,
                        ]
                    }
                },
                "late_count": {
                    "$sum": {"$cond": [{"$gt": ["$late_minutes", 0]}, 1, 0]}
                },
            }
        },
        {
            "$project": {
                "_id": 0,
                "date_str": "$_id",
                "date_val": {"$dateFromString": {"dateString": "$_id"}},
                "present_count": "$present_count",
                "late_count": "$late_count",
            }
        },
        {
            "$unionWith": {
                "pipeline": [
                    {
                        "$documents": [
                            {
                                "date_str": from_date,
                                "date_val": from_dt,
                                "present_count": 0.0,
                                "late_count": 0,
                                "is_anchor": True,
                            }
                        ]
                    }
                ]
            }
        },
        {
            "$group": {
                "_id": "$date_val",
                "date_str": {"$first": "$date_str"},
                "present_count": {"$sum": "$present_count"},
                "late_count": {"$sum": "$late_count"},
            }
        },
        {
            "$densify": {
                "field": "_id",
                "range": {
                    "step": 1,
                    "unit": "day",
                    "bounds": [from_dt, to_dt],
                },
            }
        },
        {
            "$project": {
                "_id": 0,
                "date_val": "$_id",
                "date": {"$dateToString": {"format": "%Y-%m-%d", "date": "$_id"}},
                "present_count": {"$ifNull": ["$present_count", 0.0]},
                "late_count": {"$ifNull": ["$late_count", 0]},
                "dayOfWeek": {"$dayOfWeek": "$_id"},
            }
        },
        {
            "$lookup": {
                "from": "employees",
                "let": {"d_str": "$date"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$and": [
                                    {"$eq": ["$department", department]},
                                    {"$lte": ["$joined_on", "$$d_str"]},
                                ]
                            }
                        }
                    },
                    {"$count": "hc"},
                ],
                "as": "hc_docs",
            }
        },
        {
            "$project": {
                "date": 1,
                "date_val": 1,
                "present_count": 1,
                "late_count": 1,
                "is_working_day": {"$in": ["$dayOfWeek", [2, 3, 4, 5, 6]]},
                "headcount": {"$ifNull": [{"$first": "$hc_docs.hc"}, 0]},
            }
        },
        {
            "$project": {
                "date": 1,
                "date_val": 1,
                "is_working_day": 1,
                "headcount": 1,
                "present_count": 1,
                "late_count": 1,
                "attendance_rate": {
                    "$cond": [
                        {
                            "$and": [
                                {"$eq": ["$is_working_day", True]},
                                {"$gt": ["$headcount", 0]},
                            ]
                        },
                        {"$round": [{"$divide": ["$present_count", "$headcount"]}, 4]},
                        None,
                    ]
                },
            }
        },
        {
            "$setWindowFields": {
                "sortBy": {"date_val": 1},
                "output": {
                    "moving_avg_7d": {
                        "$avg": "$attendance_rate",
                        "window": {
                            "documents": [-6, "current"],
                        },
                    }
                },
            }
        },
        {"$sort": {"date_val": 1}},
        {
            "$project": {
                "date": 1,
                "is_working_day": 1,
                "headcount": 1,
                "present_count": {"$round": ["$present_count", 2]},
                "late_count": 1,
                "attendance_rate": 1,
                "moving_avg_7d": {"$round": ["$moving_avg_7d", 4]},
            }
        },
    ]


@app.get("/analytics/departments/{department}/trend")
def department_trend(
    department: str = Path(...),
    from_date: str = Query(..., alias="from", pattern=r"^\d{4}-\d{2}-\d{2}$"),
    to_date: str = Query(..., alias="to", pattern=r"^\d{4}-\d{2}-\d{2}$"),
):
    if to_date < from_date:
        raise HTTPException(422, "'to' date must be on or after 'from' date")

    start_d = date.fromisoformat(from_date)
    end_d = date.fromisoformat(to_date)
    days_span = (end_d - start_d).days + 1
    if days_span > 92:
        raise HTTPException(422, "Date range cannot exceed 92 days")

    dept_count = db.employees.count_documents({"department": department})
    if dept_count == 0:
        raise HTTPException(404, "Department not found")

    pipeline = build_department_trend_pipeline(department, from_date, to_date)
    items = list(db.attendance_logs.aggregate(pipeline))
    return {"department": department, "items": items}


# explain endpoint for query profiling
@app.get("/admin/explain/{endpoint}")
def explain_endpoint(
    endpoint: str = Path(..., pattern=r"^(attendance_list|employee_monthly|department_summary|late_leaderboard|department_trend)$"),
    emp_code: Optional[str] = None,
    month: Optional[str] = Query(None, pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: Optional[str] = None,
    limit: int = Query(10, ge=1, le=50),
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[str] = Query(None, pattern=r"^(PRESENT|ABSENT|LEAVE|WFH|ON_DUTY)$"),
    from_date: Optional[str] = Query(None, alias="from"),
    to_date: Optional[str] = Query(None, alias="to"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    if endpoint == "attendance_list":
        if date_from and date_to and date_from > date_to:
            raise HTTPException(422, "date_from cannot be greater than date_to")
        q = {}
        if emp_code:
            q["emp_code"] = emp_code
        if date_from or date_to:
            q["date"] = {}
            if date_from:
                q["date"]["$gte"] = date_from
            if date_to:
                q["date"]["$lte"] = date_to
        if status:
            q["status"] = status

        skip = (page - 1) * page_size
        command = {
            "explain": {
                "find": "attendance_logs",
                "filter": q,
                "sort": {"date": -1, "emp_code": 1},
                "skip": skip,
                "limit": page_size,
            },
            "verbosity": "executionStats",
        }
        res = db.command(command)
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": res}

    elif endpoint == "employee_monthly":
        if not emp_code or not month:
            raise HTTPException(422, "emp_code and month are required for employee_monthly")
        pipeline = build_employee_monthly_pipeline(emp_code, month)
        command = {
            "explain": {
                "aggregate": "attendance_logs",
                "pipeline": pipeline,
                "cursor": {},
            },
            "verbosity": "executionStats",
        }
        res = db.command(command)
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": res}

    elif endpoint == "department_summary":
        if not month:
            raise HTTPException(422, "month is required for department_summary")
        pipeline = build_department_summary_pipeline(month, department)
        command = {
            "explain": {
                "aggregate": "employees",
                "pipeline": pipeline,
                "cursor": {},
            },
            "verbosity": "executionStats",
        }
        res = db.command(command)
        return {"endpoint": endpoint, "collection": "employees", "explain": res}

    elif endpoint == "late_leaderboard":
        if not month:
            raise HTTPException(422, "month is required for late_leaderboard")
        pipeline = build_late_leaderboard_pipeline(month, limit, department)
        command = {
            "explain": {
                "aggregate": "attendance_logs",
                "pipeline": pipeline,
                "cursor": {},
            },
            "verbosity": "executionStats",
        }
        res = db.command(command)
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": res}

    elif endpoint == "department_trend":
        if not department or not from_date or not to_date:
            raise HTTPException(422, "department, from, and to are required for department_trend")
        if to_date < from_date:
            raise HTTPException(422, "'to' date must be on or after 'from' date")
        pipeline = build_department_trend_pipeline(department, from_date, to_date)
        command = {
            "explain": {
                "aggregate": "attendance_logs",
                "pipeline": pipeline,
                "cursor": {},
            },
            "verbosity": "executionStats",
        }
        res = db.command(command)
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": res}

    raise HTTPException(404, "Unknown explain endpoint")
