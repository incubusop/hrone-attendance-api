"""Employee attendance API for the HROne assignment."""
from __future__ import annotations

import os
import re
from calendar import monthrange
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Annotated, Any, Literal

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field, StrictInt, field_validator, model_validator
from pymongo import ASCENDING, DESCENDING, MongoClient
from pymongo.errors import DuplicateKeyError

load_dotenv()
UTC = timezone.utc
IST = timezone(timedelta(hours=5, minutes=30))
PRESENT_STATUSES = ("PRESENT", "WFH", "ON_DUTY")
ALL_STATUSES = PRESENT_STATUSES + ("ABSENT", "LEAVE")
MIN_EPOCH_MS = 100_000_000_000
MAX_EPOCH_MS = 253_402_300_799_000

client = MongoClient(os.getenv("MONGO_URI", "mongodb://localhost:27017"), tz_aware=True, serverSelectionTimeoutMS=3000)
db = client[os.getenv("MONGO_DB", "attendance_db")]
app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0")


@app.on_event("startup")
def create_indexes() -> None:
    db.employees.create_index([("emp_code", ASCENDING)], unique=True, name="uq_employee_code")
    db.employees.create_index([("department", ASCENDING), ("emp_code", ASCENDING)], name="department_employee")
    db.employees.create_index([("department", ASCENDING), ("joined_on", ASCENDING)], name="department_joined_on")
    db.employees.create_index([("joined_on", ASCENDING)], name="employee_joined_on")
    db.attendance_logs.create_index([("emp_code", ASCENDING), ("date", ASCENDING)], unique=True, name="uq_attendance_employee_date")
    db.attendance_logs.create_index([("date", DESCENDING), ("emp_code", ASCENDING)], name="attendance_date_employee")
    db.attendance_logs.create_index([("date", ASCENDING), ("status", ASCENDING), ("emp_code", ASCENDING)], name="attendance_date_status_employee")
    db.attendance_logs.create_index([("emp_code", ASCENDING), ("punch_in", DESCENDING)], name="attendance_employee_punch_in")


def parse_date(value: str, field: str = "date") -> date:
    try:
        parsed = date.fromisoformat(value)
    except (TypeError, ValueError):
        raise HTTPException(422, f"{field} must be YYYY-MM-DD")
    if parsed.isoformat() != value:
        raise HTTPException(422, f"{field} must be YYYY-MM-DD")
    return parsed


def parse_month(value: str) -> tuple[date, date]:
    if not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", value or ""):
        raise HTTPException(422, "month must be YYYY-MM")
    year, month = map(int, value.split("-"))
    return date(year, month, 1), date(year, month, monthrange(year, month)[1])


def timestamp(value: int | None) -> datetime:
    value = int(datetime.now(UTC).timestamp() * 1000) if value is None else value
    if isinstance(value, bool) or not isinstance(value, int) or not MIN_EPOCH_MS <= value <= MAX_EPOCH_MS:
        raise HTTPException(422, "timestamps must be epoch milliseconds")
    try:
        return datetime.fromtimestamp(value // 1000, UTC)
    except (OverflowError, OSError, ValueError):
        raise HTTPException(422, "timestamp is out of range")


def epoch_ms(value: datetime | None) -> int | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return int(value.timestamp() * 1000)


def clock_pair(value: str) -> tuple[int, int]:
    return tuple(map(int, value.split(":")))  # validated by the request model / stored contract


def shift_start_instant(attendance_day: date, shift_start: str, punch_in_ist: datetime, shift_end: str) -> datetime:
    sh, sm = clock_pair(shift_start)
    eh, em = clock_pair(shift_end)
    overnight = (eh, em) <= (sh, sm)
    local_day = punch_in_ist.date()
    if overnight and (punch_in_ist.hour, punch_in_ist.minute) < (eh, em):
        local_day = local_day - timedelta(days=1)
    return datetime.combine(local_day, time(sh, sm), IST)


def attendance_day_for(punch_in: datetime, shift_start: str, shift_end: str) -> date:
    local = punch_in.astimezone(IST)
    sh, sm = clock_pair(shift_start)
    eh, em = clock_pair(shift_end)
    if (eh, em) <= (sh, sm) and (local.hour, local.minute) < (eh, em):
        return local.date() - timedelta(days=1)
    return local.date()


def late_minutes(punch_in: datetime, employee: dict[str, Any]) -> int:
    local = punch_in.astimezone(IST)
    start = shift_start_instant(local.date(), employee["shift_start"], local, employee["shift_end"])
    elapsed = int((local - start).total_seconds())
    return elapsed // 60 if elapsed > 600 else 0


def rounded_hours(punch_in: datetime, punch_out: datetime) -> float:
    seconds = Decimal(str((punch_out - punch_in).total_seconds()))
    return float((seconds / Decimal(3600)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def overtime_minutes(punch_out: datetime, attendance_day: str, shift_end: str, shift_start: str) -> int:
    day = date.fromisoformat(attendance_day)
    sh, sm = clock_pair(shift_start)
    eh, em = clock_pair(shift_end)
    end_day = day + timedelta(days=1) if (eh, em) <= (sh, sm) else day
    end = datetime.combine(end_day, time(eh, em), IST)
    elapsed = int((punch_out - end).total_seconds())
    return elapsed // 60 if elapsed >= 1800 else 0


def api_value(value: Any) -> Any:
    if isinstance(value, datetime):
        return epoch_ms(value)
    if isinstance(value, list):
        return [api_value(item) for item in value]
    if isinstance(value, dict):
        return {key: api_value(item) for key, item in value.items()}
    if value.__class__.__name__ == "ObjectId":
        return str(value)
    return value


def attendance_response(doc: dict[str, Any]) -> dict[str, Any]:
    out = {key: doc.get(key) for key in ("emp_code", "date", "status", "punch_in", "punch_out", "work_hours", "late_minutes", "overtime_minutes", "half_day", "history")}
    out["history"] = out["history"] or []
    out["late_minutes"] = out["late_minutes"] or 0
    out["overtime_minutes"] = out["overtime_minutes"] or 0
    out["half_day"] = bool(out["half_day"] or False)
    return api_value(out)


class EmployeeCreate(BaseModel):
    model_config = ConfigDict(extra="ignore")
    emp_code: str = Field(pattern=r"^EMP\d{4,6}$")
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(min_length=1, max_length=50)
    shift_start: str = Field(default="09:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    shift_end: str = Field(default="18:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    joined_on: str

    @field_validator("joined_on")
    @classmethod
    def valid_joined_on(cls, value: str) -> str:
        parse_date(value, "joined_on")
        return value

    @model_validator(mode="after")
    def different_shift_times(self):
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start must differ from shift_end")
        return self


class PunchRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")
    emp_code: str
    punched_at: StrictInt | None = None

    @field_validator("punched_at")
    @classmethod
    def valid_timestamp(cls, value):
        if value is not None and not MIN_EPOCH_MS <= value <= MAX_EPOCH_MS:
            raise ValueError("punched_at must be valid epoch milliseconds")
        return value


class PunchInRequest(PunchRequest):
    status: Literal["PRESENT", "WFH", "ON_DUTY"] = "PRESENT"


class RegularizeRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")
    status: Literal["PRESENT", "WFH", "ON_DUTY", "ABSENT", "LEAVE"] | None = None
    punch_in: StrictInt | None = None
    punch_out: StrictInt | None = None
    reason: str = Field(min_length=5, max_length=200)
    regularized_by: str = Field(min_length=1, max_length=50)

    @field_validator("punch_in", "punch_out")
    @classmethod
    def valid_times(cls, value):
        if value is not None and not MIN_EPOCH_MS <= value <= MAX_EPOCH_MS:
            raise ValueError("timestamps must be valid epoch milliseconds")
        return value


def db_datetime(value: datetime) -> datetime:
    return value.astimezone(UTC).replace(microsecond=0)


def _presence_expr():
    return {"$cond": ["$half_day", 0.5, 1.0]}


def _weekday_expr(field: str):
    return {"$isoDayOfWeek": {"$dateFromString": {"dateString": field}}}


def monthly_pipeline(emp_code: str, start: date, end: date) -> list[dict[str, Any]]:
    return [
        {"$match": {"emp_code": emp_code, "date": {"$gte": start.isoformat(), "$lte": end.isoformat()}}},
        {"$addFields": {"_weekday": _weekday_expr("$date")}},
        {"$group": {"_id": None,
                    "present_days": {"$sum": {"$cond": [{"$and": [{"$in": ["$status", list(PRESENT_STATUSES)]}, {"$lte": ["$_weekday", 5]}]}, _presence_expr(), 0]}},
                    "leave_days": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
                    "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}},
                    "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
                    "total_overtime_minutes": {"$sum": {"$ifNull": ["$overtime_minutes", 0]}}}},
    ]


def department_summary_pipeline(start: date, end: date, department: str | None) -> list[dict[str, Any]]:
    emp_match: dict[str, Any] = {"joined_on": {"$lte": end.isoformat()}}
    if department is not None:
        emp_match["department"] = department
    return [
        {"$match": emp_match},
        {"$lookup": {"from": "attendance_logs", "let": {"code": "$emp_code"}, "pipeline": [
            {"$match": {"$expr": {"$and": [{"$eq": ["$emp_code", "$$code"]}, {"$gte": ["$date", start.isoformat()]}, {"$lte": ["$date", end.isoformat()]}]}}},
            {"$addFields": {"_weekday": _weekday_expr("$date")}},
            {"$group": {"_id": None,
                        "present_days": {"$sum": {"$cond": [{"$and": [{"$in": ["$status", list(PRESENT_STATUSES)]}, {"$lte": ["$_weekday", 5]}]}, _presence_expr(), 0]}},
                        "work_hours": {"$avg": {"$cond": [{"$and": [{"$in": ["$status", list(PRESENT_STATUSES)]}, {"$ne": ["$work_hours", None]}]}, "$work_hours", None]}},
                        "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}},
                        "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
                        "leave_count": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
                        "on_duty_count": {"$sum": {"$cond": [{"$eq": ["$status", "ON_DUTY"]}, 1, 0]}}}},
        ], "as": "stats"}},
        {"$set": {"stats": {"$ifNull": [{"$first": "$stats"}, {"present_days": 0, "work_hours": None, "late_count": 0, "total_late_minutes": 0, "leave_count": 0, "on_duty_count": 0}]}}},
        {"$group": {"_id": "$department", "headcount": {"$sum": 1},
                    "present_days": {"$sum": "$stats.present_days"},
                    "_hours_sum": {"$sum": {"$ifNull": ["$stats.work_hours", 0]}},
                    "_hours_count": {"$sum": {"$cond": [{"$ne": ["$stats.work_hours", None]}, 1, 0]}},
                    "late_count": {"$sum": "$stats.late_count"}, "total_late_minutes": {"$sum": "$stats.total_late_minutes"},
                    "leave_count": {"$sum": "$stats.leave_count"}, "on_duty_count": {"$sum": "$stats.on_duty_count"}}},
        {"$project": {"_id": 0, "department": "$_id", "headcount": 1, "present_days": 1,
                       "avg_work_hours": {"$cond": [{"$gt": ["$_hours_count", 0]}, {"$divide": ["$_hours_sum", "$_hours_count"]}, None]},
                       "late_count": 1, "total_late_minutes": 1, "leave_count": 1, "on_duty_count": 1}},
        {"$sort": {"department": 1}},
    ]


def leaderboard_pipeline(start: date, end: date, limit: int, department: str | None) -> list[dict[str, Any]]:
    return [
        {"$match": {"date": {"$gte": start.isoformat(), "$lte": end.isoformat()}, "late_minutes": {"$gt": 0}}},
        {"$group": {"_id": "$emp_code", "total_late_minutes": {"$sum": "$late_minutes"}, "late_count": {"$sum": 1}}},
        {"$lookup": {"from": "employees", "localField": "_id", "foreignField": "emp_code", "as": "employee"}},
        {"$unwind": "$employee"},
        *([{ "$match": {"employee.department": department}}] if department is not None else []),
        {"$setWindowFields": {"sortBy": {"total_late_minutes": -1}, "output": {"rank": {"$rank": {}}}}},
        {"$match": {"rank": {"$lte": limit}}},
        {"$sort": {"total_late_minutes": -1, "_id": 1}},
        {"$project": {"_id": 0, "rank": 1, "emp_code": "$_id", "name": "$employee.name", "department": "$employee.department", "total_late_minutes": 1, "late_count": 1}},
    ]


def trend_pipeline(department: str, start: date, end: date) -> list[dict[str, Any]]:
    days = (end - start).days + 1
    return [
        {"$documents": [{}]},
        {"$project": {"dates": {"$map": {"input": {"$range": [0, days]}, "as": "n", "in": {"$dateAdd": {"startDate": {"$dateFromString": {"dateString": start.isoformat()}}, "unit": "day", "amount": "$$n"}}}}}},
        {"$unwind": "$dates"},
        {"$set": {"_date": {"$dateToString": {"date": "$dates", "format": "%Y-%m-%d"}}}},
        {"$lookup": {"from": "employees", "let": {"day": "$_date"}, "pipeline": [
            {"$match": {"$expr": {"$and": [{"$eq": ["$department", department]}, {"$lte": ["$joined_on", "$$day"]}]}}},
            {"$count": "n"},
        ], "as": "_heads"}},
        {"$lookup": {"from": "attendance_logs", "let": {"day": "$_date"}, "pipeline": [
            {"$match": {"$expr": {"$eq": ["$date", "$$day"]}}},
            {"$lookup": {"from": "employees", "localField": "emp_code", "foreignField": "emp_code", "as": "_employee"}},
            {"$unwind": "$_employee"}, {"$match": {"_employee.department": department}},
            {"$group": {"_id": None,
                        "present_count": {"$sum": {"$cond": [{"$in": ["$status", list(PRESENT_STATUSES)]}, {"$cond": ["$half_day", 0.5, 1.0]}, 0]}},
                        "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}}}},
        ], "as": "_stats"}},
        {"$set": {"headcount": {"$ifNull": [{"$first": "$_heads.n"}, 0]}, "present_count": {"$ifNull": [{"$first": "$_stats.present_count"}, 0]}, "late_count": {"$ifNull": [{"$first": "$_stats.late_count"}, 0]}, "is_working_day": {"$lte": [{"$isoDayOfWeek": "$dates"}, 5]}}},
        {"$set": {"attendance_rate": {"$cond": [{"$and": ["$is_working_day", {"$gt": ["$headcount", 0]}]}, {"$divide": [{"$floor": [{"$add": [{"$multiply": [{"$divide": ["$present_count", "$headcount"]}, 10000]}, 0.5]}]}, 10000]}, None]}}},
        {"$setWindowFields": {"sortBy": {"dates": 1}, "output": {"moving_avg_7d": {"$avg": "$attendance_rate", "window": {"documents": [-6, 0]}}}}},
        {"$project": {"_id": 0, "date": "$_date", "is_working_day": 1, "headcount": 1, "present_count": 1, "late_count": 1, "attendance_rate": 1, "moving_avg_7d": {"$cond": [{"$eq": [{"$type": "$moving_avg_7d"}, "missing"]}, None, {"$divide": [{"$floor": [{"$add": [{"$multiply": ["$moving_avg_7d", 10000]}, 0.5]}]}, 10000]}]}}},
    ]


@app.get("/health")
def health():
    try:
        db.command("ping")
    except Exception:
        raise HTTPException(503, "database unavailable")
    return {"status": "ok"}


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeCreate):
    doc = body.model_dump()
    doc["created_at"] = datetime.now(UTC).replace(microsecond=0)
    try:
        db.employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "emp_code already exists")
    doc.pop("_id", None)
    return api_value(doc)


@app.get("/employees")
def list_employees(department: str | None = None, page: Annotated[int, Query(ge=1)] = 1, page_size: Annotated[int, Query(ge=1, le=100)] = 20):
    query = {"department": department} if department is not None else {}
    total = db.employees.count_documents(query)
    cursor = db.employees.find(query, {"_id": 0}).sort("emp_code", ASCENDING).skip((page - 1) * page_size).limit(page_size)
    return {"items": [api_value(row) for row in cursor], "total": total, "page": page, "page_size": page_size}


@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInRequest):
    employee = db.employees.find_one({"emp_code": body.emp_code})
    if employee is None:
        raise HTTPException(404, "employee not found")
    punched = db_datetime(timestamp(body.punched_at))
    day = attendance_day_for(punched, employee["shift_start"], employee["shift_end"]).isoformat()
    doc = {"emp_code": body.emp_code, "date": day, "status": body.status, "punch_in": punched, "punch_out": None,
           "work_hours": None, "late_minutes": late_minutes(punched, employee), "overtime_minutes": 0, "half_day": False, "history": []}
    try:
        db.attendance_logs.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "already punched in for this date")
    return attendance_response(doc)


@app.post("/attendance/punch-out")
def punch_out(body: PunchRequest):
    employee = db.employees.find_one({"emp_code": body.emp_code})
    if employee is None:
        raise HTTPException(404, "employee not found")
    punched = db_datetime(timestamp(body.punched_at))
    doc = db.attendance_logs.find_one({"emp_code": body.emp_code, "punch_in": {"$lte": punched}}, sort=[("punch_in", DESCENDING)])
    if doc is None:
        raise HTTPException(404, "no punch-in found")
    seconds = (punched - doc["punch_in"]).total_seconds()
    if seconds <= 0 or seconds > 24 * 3600:
        raise HTTPException(422, "punch-out must be after punch-in and within 24 hours")
    work = rounded_hours(doc["punch_in"], punched)
    overtime = overtime_minutes(punched, doc["date"], employee["shift_end"], employee["shift_start"])
    half = work < 4.5
    result = db.attendance_logs.update_one({"_id": doc["_id"], "punch_out": None}, {"$set": {"punch_out": punched, "work_hours": work, "overtime_minutes": overtime, "half_day": half}})
    if result.modified_count != 1:
        raise HTTPException(409, "already punched out")
    doc.update({"punch_out": punched, "work_hours": work, "overtime_minutes": overtime, "half_day": half})
    return attendance_response(doc)


@app.get("/attendance")
def list_attendance(emp_code: str | None = None, date_from: str | None = None, date_to: str | None = None,
                    status: Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"] | None = None,
                    page: Annotated[int, Query(ge=1)] = 1, page_size: Annotated[int, Query(ge=1, le=100)] = 20):
    query: dict[str, Any] = {}
    if emp_code is not None:
        query["emp_code"] = emp_code
    if status is not None:
        query["status"] = status
    if date_from is not None or date_to is not None:
        lo = parse_date(date_from, "date_from") if date_from else None
        hi = parse_date(date_to, "date_to") if date_to else None
        if lo and hi and lo > hi:
            raise HTTPException(422, "date_from must be on or before date_to")
        query["date"] = {**({"$gte": lo.isoformat()} if lo else {}), **({"$lte": hi.isoformat()} if hi else {})}
    total = db.attendance_logs.count_documents(query)
    cursor = db.attendance_logs.find(query).sort([("date", DESCENDING), ("emp_code", ASCENDING)]).skip((page - 1) * page_size).limit(page_size)
    return {"items": [attendance_response(row) for row in cursor], "total": total, "page": page, "page_size": page_size}


@app.patch("/attendance/{emp_code}/{date}")
def regularize_attendance(emp_code: str, date: str, body: RegularizeRequest):
    day = parse_date(date).isoformat()
    employee = db.employees.find_one({"emp_code": emp_code})
    if employee is None:
        raise HTTPException(404, "employee not found")
    old = db.attendance_logs.find_one({"emp_code": emp_code, "date": day})
    if old is None:
        raise HTTPException(404, "attendance record not found")
    status = body.status if body.status is not None else old["status"]
    if status in ("ABSENT", "LEAVE"):
        if body.punch_in is not None or body.punch_out is not None:
            raise HTTPException(422, "ABSENT and LEAVE cannot have punch times")
        pi = po = None
    else:
        pi = db_datetime(timestamp(body.punch_in)) if body.punch_in is not None else old.get("punch_in")
        po = db_datetime(timestamp(body.punch_out)) if body.punch_out is not None else old.get("punch_out")
        if pi is None:
            raise HTTPException(422, "presence status requires punch_in")
        if attendance_day_for(pi, employee["shift_start"], employee["shift_end"]).isoformat() != day:
            raise HTTPException(422, "punch_in must belong to the record date")
        if po is not None and ((po - pi).total_seconds() <= 0 or (po - pi).total_seconds() > 24 * 3600):
            raise HTTPException(422, "punch_out must be after punch_in and within 24 hours")
    late = late_minutes(pi, employee) if pi is not None else 0
    work = rounded_hours(pi, po) if pi is not None and po is not None else None
    overtime = overtime_minutes(po, day, employee["shift_end"], employee["shift_start"]) if po is not None else 0
    half = bool(work is not None and work < 4.5)
    final = {"status": status, "punch_in": pi, "punch_out": po, "work_hours": work, "late_minutes": late, "overtime_minutes": overtime, "half_day": half}
    changed = {}
    for field, new_value in final.items():
        old_value = old.get(field)
        if old_value != new_value:
            changed[field] = {"from": old_value, "to": new_value}
    if not changed:
        raise HTTPException(422, "request does not change the record")
    history = list(old.get("history", []))
    history_entry = {"at": datetime.now(UTC).replace(microsecond=0), "by": body.regularized_by, "reason": body.reason, "changes": changed}
    history_match = {"history": old["history"]} if "history" in old else {"$or": [{"history": []}, {"history": {"$exists": False}}]}
    result = db.attendance_logs.update_one({"_id": old["_id"], **history_match}, {"$set": final, "$push": {"history": history_entry}})
    if result.modified_count != 1:
        raise HTTPException(409, "record changed concurrently; retry")
    old.update(final)
    old["history"] = history + [history_entry]
    return attendance_response(old)


@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code: str, month: str):
    start, end = parse_month(month)
    employee = db.employees.find_one({"emp_code": emp_code}, {"_id": 0, "joined_on": 1})
    if employee is None:
        raise HTTPException(404, "employee not found")
    active_start = max(start, parse_date(employee["joined_on"], "joined_on"))
    working_days = sum(1 for n in range(max(0, (end - active_start).days + 1)) if (active_start + timedelta(days=n)).weekday() < 5)
    stats = next(db.attendance_logs.aggregate(monthly_pipeline(emp_code, start, end)), {})
    present = float(stats.get("present_days", 0))
    pct = float(Decimal(str(present * 100 / working_days)).quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)) if working_days else None
    return {"emp_code": emp_code, "month": month, "working_days": working_days, "present_days": round(present, 2),
            "leave_days": stats.get("leave_days", 0), "late_count": stats.get("late_count", 0),
            "total_late_minutes": stats.get("total_late_minutes", 0), "total_overtime_minutes": stats.get("total_overtime_minutes", 0), "attendance_pct": pct}


@app.get("/analytics/departments/summary")
def department_summary(month: str, department: str | None = None):
    start, end = parse_month(month)
    items = list(db.employees.aggregate(department_summary_pipeline(start, end, department)))
    for row in items:
        if row["avg_work_hours"] is not None:
            row["avg_work_hours"] = float(Decimal(str(row["avg_work_hours"])).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))
        row["present_days"] = float(Decimal(str(row["present_days"])).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))
    return {"month": month, "items": items}


@app.get("/analytics/leaderboard/late")
def late_leaderboard(month: str, limit: Annotated[int, Query(ge=1, le=50)] = 10, department: str | None = None):
    start, end = parse_month(month)
    return {"month": month, "items": list(db.attendance_logs.aggregate(leaderboard_pipeline(start, end, limit, department)))}


@app.get("/analytics/departments/{department}/trend")
def department_trend(department: str, from_value: Annotated[str, Query(alias="from")], to_value: Annotated[str, Query(alias="to")]):
    if not from_value or not to_value:
        raise HTTPException(422, "from and to are required")
    start, end = parse_date(from_value, "from"), parse_date(to_value, "to")
    if end < start:
        raise HTTPException(422, "to must be on or after from")
    if (end - start).days + 1 > 92:
        raise HTTPException(422, "trend range may not exceed 92 days")
    if not db.employees.find_one({"department": department}, {"_id": 1}):
        raise HTTPException(404, "department not found")
    items = list(db.command("aggregate", 1, pipeline=trend_pipeline(department, start, end), cursor={})["cursor"]["firstBatch"])
    return {"department": department, "items": items}


@app.get("/admin/explain/{endpoint}")
def explain_endpoint(endpoint: str, emp_code: str | None = None, month: str | None = None, department: str | None = None,
                     limit: Annotated[int, Query(ge=1, le=50)] = 10, date_from: str | None = None, date_to: str | None = None,
                     page: Annotated[int, Query(ge=1)] = 1, page_size: Annotated[int, Query(ge=1, le=100)] = 20,
                     status: Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"] | None = None,
                     from_value: str | None = Query(None, alias="from"), to_value: str | None = Query(None, alias="to")):
    if endpoint not in {"attendance_list", "employee_monthly", "department_summary", "late_leaderboard", "department_trend"}:
        raise HTTPException(422, "unsupported endpoint")
    if endpoint == "attendance_list":
        query: dict[str, Any] = {}
        if emp_code:
            query["emp_code"] = emp_code
        if status:
            query["status"] = status
        if date_from or date_to:
            lower = parse_date(date_from, "date_from") if date_from else None
            upper = parse_date(date_to, "date_to") if date_to else None
            if lower and upper and lower > upper:
                raise HTTPException(422, "date_from must be on or before date_to")
            query["date"] = {**({"$gte": lower.isoformat()} if lower else {}), **({"$lte": upper.isoformat()} if upper else {})}
        pipeline = [{"$match": query}, {"$sort": {"date": -1, "emp_code": 1}}, {"$skip": (page - 1) * page_size}, {"$limit": page_size}]
        coll = "attendance_logs"
        explain = db.command("explain", {"aggregate": coll, "pipeline": pipeline, "cursor": {}}, verbosity="executionStats")
    elif endpoint == "employee_monthly":
        if not emp_code or not month:
            raise HTTPException(422, "emp_code and month are required")
        start, end = parse_month(month)
        pipeline, coll = monthly_pipeline(emp_code, start, end), "attendance_logs"
        explain = db.command("explain", {"aggregate": coll, "pipeline": pipeline, "cursor": {}}, verbosity="executionStats")
    elif endpoint == "department_summary":
        if not month:
            raise HTTPException(422, "month is required")
        start, end = parse_month(month)
        pipeline, coll = department_summary_pipeline(start, end, department), "employees"
        explain = db.command("explain", {"aggregate": coll, "pipeline": pipeline, "cursor": {}}, verbosity="executionStats")
    elif endpoint == "late_leaderboard":
        if not month:
            raise HTTPException(422, "month is required")
        start, end = parse_month(month)
        pipeline, coll = leaderboard_pipeline(start, end, limit, department), "attendance_logs"
        explain = db.command("explain", {"aggregate": coll, "pipeline": pipeline, "cursor": {}}, verbosity="executionStats")
    else:
        if not department or not from_value or not to_value:
            raise HTTPException(422, "department, from, and to are required")
        start, end = parse_date(from_value, "from"), parse_date(to_value, "to")
        if end < start or (end - start).days + 1 > 92:
            raise HTTPException(422, "invalid trend range")
        pipeline = trend_pipeline(department, start, end)
        explain = db.command("explain", {"aggregate": 1, "pipeline": pipeline, "cursor": {}}, verbosity="executionStats")
        coll = "database"
    return {"endpoint": endpoint, "collection": coll, "explain": api_value(explain)}
