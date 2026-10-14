"""Employee Attendance & Analytics API (HROne assignment)."""
import os
import re
import calendar
from datetime import datetime, date, time, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Optional, Literal

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query, Path
from pydantic import BaseModel, ConfigDict, Field, StrictInt, field_validator, model_validator
from pymongo import MongoClient, ASCENDING, DESCENDING, ReturnDocument
from pymongo.errors import DuplicateKeyError, PyMongoError
from bson import json_util
from bson.json_util import dumps

load_dotenv()
MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017")
MONGO_DB = os.getenv("MONGO_DB", "attendance_db")
client = MongoClient(MONGO_URI, tz_aware=True, serverSelectionTimeoutMS=3000)
db = client[MONGO_DB]
employees = db["employees"]
logs = db["attendance_logs"]
IST = timezone(timedelta(hours=5, minutes=30))
UTC = timezone.utc
PRESENCE = {"PRESENT", "WFH", "ON_DUTY"}
STATUSES = {"PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"}
MIN_TS, MAX_TS = 100_000_000_000, 4_102_444_800_000
app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0")


@app.on_event("startup")
def ensure_indexes():
    employees.create_index([("emp_code", ASCENDING)], unique=True, name="uq_employee_code")
    employees.create_index([("department", ASCENDING), ("joined_on", ASCENDING)], name="ix_employee_department_joined")
    employees.create_index([("joined_on", ASCENDING)], name="ix_employee_joined_on")
    logs.create_index([("emp_code", ASCENDING), ("date", ASCENDING)], unique=True, name="uq_attendance_employee_date")
    logs.create_index([("date", DESCENDING), ("emp_code", ASCENDING)], name="ix_attendance_date_employee")
    logs.create_index([("emp_code", ASCENDING), ("date", ASCENDING), ("status", ASCENDING)], name="ix_attendance_employee_date_status")
    logs.create_index([("date", ASCENDING), ("status", ASCENDING), ("late_minutes", DESCENDING)], name="ix_attendance_date_status_late")


def now_utc():
    return datetime.now(UTC).replace(microsecond=0)


def epoch_ms(dt):
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return int(dt.timestamp() * 1000)


def from_ms(value):
    if type(value) is not int or value < MIN_TS or value > MAX_TS:
        raise HTTPException(422, "timestamp must be an integer epoch millisecond value in range")
    return datetime.fromtimestamp(value // 1000, UTC)


def api_record(doc):
    if not doc:
        return doc
    d = dict(doc)
    d.pop("_id", None)
    for key in ("created_at", "punch_in", "punch_out"):
        if key in d and isinstance(d[key], datetime):
            d[key] = epoch_ms(d[key])
    hist = []
    for entry in d.get("history") or []:
        e = dict(entry)
        if isinstance(e.get("at"), datetime):
            e["at"] = epoch_ms(e["at"])
        changes = {}
        for k, v in (e.get("changes") or {}).items():
            v = dict(v)
            for side in ("from", "to"):
                if isinstance(v.get(side), datetime):
                    v[side] = epoch_ms(v[side])
            changes[k] = v
        e["changes"] = changes
        hist.append(e)
    d["history"] = hist
    d.setdefault("punch_in", None); d.setdefault("punch_out", None)
    d.setdefault("work_hours", None); d.setdefault("late_minutes", 0)
    d.setdefault("overtime_minutes", 0); d.setdefault("half_day", False)
    d.setdefault("history", [])
    return d


def round_half_up(value, places=2):
    if value is None:
        return None
    quantum = Decimal("1").scaleb(-places)
    return float(Decimal(str(value)).quantize(quantum, rounding=ROUND_HALF_UP))


def valid_date(s, field="date"):
    try:
        if not isinstance(s, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", s):
            raise ValueError()
        return date.fromisoformat(s)
    except ValueError:
        raise HTTPException(422, f"{field} must be YYYY-MM-DD")


def valid_month(s):
    if not isinstance(s, str) or not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", s):
        raise HTTPException(422, "month must be YYYY-MM")
    y, m = map(int, s.split("-"))
    return date(y, m, 1), date(y, m, calendar.monthrange(y, m)[1])


def valid_shift(s):
    if not isinstance(s, str) or not re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", s):
        raise HTTPException(422, "shift times must be HH:MM")
    return tuple(map(int, s.split(":")))


def shift_dt(day, shift, overnight_end=False):
    h, m = valid_shift(shift)
    return datetime.combine(day, time(h, m), IST).astimezone(UTC)


def attendance_day(punch_in, emp):
    local = punch_in.astimezone(IST)
    d = local.date()
    if emp["shift_end"] <= emp["shift_start"] and local.strftime("%H:%M") < emp["shift_end"]:
        d -= timedelta(days=1)
    return d.isoformat()


def calc_late(punch_in, emp, daystr):
    day = valid_date(daystr)
    start = shift_dt(day, emp["shift_start"])
    seconds = int((punch_in - start).total_seconds())
    return seconds // 60 if seconds > 600 else 0


def calc_work(punch_in, punch_out):
    return float((Decimal(int((punch_out - punch_in).total_seconds())) / Decimal(3600)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def calc_overtime(punch_out, emp, daystr):
    day = valid_date(daystr)
    end_day = day + timedelta(days=1) if emp["shift_end"] <= emp["shift_start"] else day
    end = shift_dt(end_day, emp["shift_end"])
    minutes = int((punch_out - end).total_seconds() // 60)
    return minutes if minutes >= 30 else 0


def derived(emp, day, pin, pout):
    if pin is None:
        return {"late_minutes": 0, "work_hours": None, "overtime_minutes": 0, "half_day": False}
    late = calc_late(pin, emp, day)
    if pout is None:
        return {"late_minutes": late, "work_hours": None, "overtime_minutes": 0, "half_day": False}
    wh = calc_work(pin, pout)
    return {"late_minutes": late, "work_hours": wh, "overtime_minutes": calc_overtime(pout, emp, day), "half_day": wh < 4.50}


def ensure_pagination(page, page_size):
    if page < 1 or page_size < 1 or page_size > 100:
        raise HTTPException(422, "page must be >= 1 and page_size must be between 1 and 100")


def month_match(month):
    start, end = valid_month(month)
    return start.isoformat(), end.isoformat()


class EmployeeCreate(BaseModel):
    model_config = ConfigDict(extra="ignore")
    emp_code: str = Field(pattern=r"^EMP\d{4,6}$")
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(min_length=1, max_length=50)
    shift_start: str = "09:30"
    shift_end: str = "18:30"
    joined_on: str
    @field_validator("joined_on")
    @classmethod
    def date_valid(cls, v):
        valid_date(v, "joined_on"); return v
    @field_validator("shift_start", "shift_end")
    @classmethod
    def shift_valid(cls, v):
        valid_shift(v); return v
    @model_validator(mode="after")
    def shifts_differ(self):
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start must differ from shift_end")
        return self


class PunchInRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")
    emp_code: str
    punched_at: Optional[StrictInt] = None
    status: Literal["PRESENT", "WFH", "ON_DUTY"] = "PRESENT"
    @field_validator("punched_at")
    @classmethod
    def timestamp(cls, v):
        if v is not None: from_ms(v)
        return v


class PunchOutRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")
    emp_code: str
    punched_at: Optional[StrictInt] = None
    @field_validator("punched_at")
    @classmethod
    def timestamp(cls, v):
        if v is not None: from_ms(v)
        return v


class RegularizeRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")
    status: Optional[Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]] = None
    punch_in: Optional[StrictInt] = None
    punch_out: Optional[StrictInt] = None
    reason: str = Field(min_length=5, max_length=200)
    regularized_by: str = Field(min_length=1, max_length=50)
    @field_validator("punch_in", "punch_out")
    @classmethod
    def timestamp(cls, v):
        if v is not None: from_ms(v)
        return v


@app.get("/health")
def health():
    try:
        db.command("ping")
        return {"status": "ok"}
    except Exception:
        raise HTTPException(503, "MongoDB unavailable")


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeCreate):
    doc = body.model_dump()
    doc["created_at"] = now_utc()
    try:
        employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "emp_code already exists")
    return {**doc, "created_at": epoch_ms(doc["created_at"])}


@app.get("/employees")
def list_employees(department: Optional[str] = None, page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=100)):
    q = {"department": department} if department is not None else {}
    total = employees.count_documents(q)
    docs = employees.find(q, {"_id": 0}).sort("emp_code", ASCENDING).skip((page-1)*page_size).limit(page_size)
    return {"items": [{**d, "created_at": epoch_ms(d["created_at"])} for d in docs], "total": total, "page": page, "page_size": page_size}


@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInRequest):
    emp = employees.find_one({"emp_code": body.emp_code}, {"_id": 0})
    if not emp:
        raise HTTPException(404, "employee not found")
    ts = from_ms(body.punched_at) if body.punched_at is not None else now_utc()
    ts = ts.replace(microsecond=0)
    day = attendance_day(ts, emp)
    vals = derived(emp, day, ts, None)
    doc = {"emp_code": body.emp_code, "date": day, "status": body.status, "punch_in": ts,
           "punch_out": None, "work_hours": None, "late_minutes": vals["late_minutes"],
           "overtime_minutes": 0, "half_day": False, "history": []}
    try:
        logs.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "already punched in for this date")
    return api_record(doc)


@app.post("/attendance/punch-out")
def punch_out(body: PunchOutRequest):
    emp = employees.find_one({"emp_code": body.emp_code}, {"_id": 0})
    if not emp:
        raise HTTPException(404, "employee not found")
    ts = (from_ms(body.punched_at) if body.punched_at is not None else now_utc()).replace(microsecond=0)
    doc = logs.find_one({"emp_code": body.emp_code, "punch_in": {"$lte": ts}}, sort=[("punch_in", DESCENDING)])
    if not doc:
        raise HTTPException(404, "no punch-in found")
    if doc.get("punch_out") is not None:
        raise HTTPException(409, "already punched out")
    pin = doc["punch_in"].replace(microsecond=0)
    if ts <= pin or (ts - pin) > timedelta(hours=24):
        raise HTTPException(422, "punch-out must be after punch-in and within 24 hours")
    vals = derived(emp, doc["date"], pin, ts)
    result = logs.update_one({"_id": doc["_id"], "punch_out": None}, {"$set": {"punch_in": pin.replace(microsecond=0), "punch_out": ts, **{k:v for k,v in vals.items() if k != "late_minutes"}}})
    if result.modified_count != 1:
        raise HTTPException(409, "already punched out")
    updated = logs.find_one({"_id": doc["_id"]})
    return api_record(updated)


@app.get("/attendance")
def list_attendance(emp_code: Optional[str] = None, date_from: Optional[str] = None, date_to: Optional[str] = None,
                    status: Optional[Literal["PRESENT","ABSENT","LEAVE","WFH","ON_DUTY"]] = None,
                    page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=100)):
    ensure_pagination(page, page_size)
    q = {}
    if emp_code is not None: q["emp_code"] = emp_code
    if date_from is not None:
        valid_date(date_from, "date_from"); q.setdefault("date", {})["$gte"] = date_from
    if date_to is not None:
        valid_date(date_to, "date_to"); q.setdefault("date", {})["$lte"] = date_to
    if date_from and date_to and date_from > date_to: raise HTTPException(422, "date_from must be <= date_to")
    if status is not None: q["status"] = status
    total = logs.count_documents(q)
    docs = logs.find(q, {"_id": 0}).sort([("date", DESCENDING), ("emp_code", ASCENDING)]).skip((page-1)*page_size).limit(page_size)
    return {"items": [api_record(d) for d in docs], "total": total, "page": page, "page_size": page_size}


@app.patch("/attendance/{emp_code}/{date}")
def regularize(emp_code: str, date: str, body: RegularizeRequest):
    valid_date(date)
    emp = employees.find_one({"emp_code": emp_code}, {"_id": 0})
    if not emp: raise HTTPException(404, "employee not found")
    old = logs.find_one({"emp_code": emp_code, "date": date})
    if not old: raise HTTPException(404, "attendance record not found")
    old_status = old.get("status")
    new_status = body.status if body.status is not None else old_status
    if new_status in {"ABSENT", "LEAVE"} and (body.punch_in is not None or body.punch_out is not None):
        raise HTTPException(422, "ABSENT/LEAVE cannot have punch times")
    pin = from_ms(body.punch_in) if body.punch_in is not None else old.get("punch_in")
    pout = from_ms(body.punch_out) if body.punch_out is not None else old.get("punch_out")
    if pin is not None: pin = pin.replace(microsecond=0)
    if pout is not None: pout = pout.replace(microsecond=0)
    if new_status in PRESENCE and pin is None: raise HTTPException(422, "presence status requires punch_in")
    if new_status in {"ABSENT", "LEAVE"}: pin = pout = None
    if pin is not None:
        if attendance_day(pin, emp) != date: raise HTTPException(422, "punch_in must belong to the attendance date")
        if pout is not None and (pout <= pin or pout-pin > timedelta(hours=24)):
            raise HTTPException(422, "punch_out must be after punch_in and within 24 hours")
    elif pout is not None:
        raise HTTPException(422, "punch_out requires punch_in")
    vals = derived(emp, date, pin, pout)
    newfields = {"status": new_status, "punch_in": pin, "punch_out": pout, **vals}
    oldfields = {k: old.get(k, False if k=="half_day" else 0 if k in ("late_minutes","overtime_minutes") else None) for k in newfields}
    changes = {}
    for k, v in newfields.items():
        ov = oldfields[k]
        if isinstance(ov, datetime) and isinstance(v, datetime):
            equal = ov.replace(microsecond=0) == v.replace(microsecond=0)
        else: equal = ov == v
        if not equal:
            changes[k] = {"from": ov, "to": v}
    if not changes: raise HTTPException(422, "request does not change the record")
    entry = {"at": now_utc(), "by": body.regularized_by, "reason": body.reason, "changes": changes}
    # Compare-and-swap protects concurrent edits and audit history from lost updates.
    filt = {"_id": old["_id"], "history": old.get("history", [])}
    if "history" not in old: filt["history"] = {"$exists": False}
    update = {"$set": newfields, "$push": {"history": entry}}
    result = logs.update_one(filt, update)
    if result.modified_count != 1: raise HTTPException(409, "record changed concurrently; retry")
    return api_record(logs.find_one({"_id": old["_id"]}))


def weekdays_between(start, end):
    if start > end: return 0
    count = (end-start).days+1
    weeks, rem = divmod(count, 7)
    return weeks*5 + sum(1 for i in range(rem) if (start.weekday()+i)%7 < 5)


@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code: str, month: str):
    start, end = valid_month(month)
    emp = employees.find_one({"emp_code": emp_code}, {"_id": 0})
    if not emp: raise HTTPException(404, "employee not found")
    work_start = max(start, valid_date(emp["joined_on"]))
    working = weekdays_between(work_start, end) if work_start <= end else 0
    pipeline = [
        {"$match": {"emp_code": emp_code, "date": {"$gte": start.isoformat(), "$lte": end.isoformat()}}},
        {"$group": {"_id": None,
          "present_days": {"$sum": {"$cond": [
              {"$and": [
                  {"$in": ["$status", list(PRESENCE)]},
                  {"$lt": [{"$isoDayOfWeek": {"$dateFromString": {"dateString": "$date"}}}, 6]}
              ]},
              {"$cond": [{"$eq": [{"$ifNull": ["$half_day", False]}, True]}, 0.5, 1]},
              0
          ]}},
          "leave_days": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
          "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}},
          "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
          "total_overtime_minutes": {"$sum": {"$ifNull": ["$overtime_minutes", 0]}}
        }}
    ]
    agg = list(logs.aggregate(pipeline))
    a = agg[0] if agg else {}
    present = a.get("present_days", 0.0)
    pct = round_half_up(present / working * 100, 2) if working else None
    return {"emp_code": emp_code, "month": month, "working_days": working, "present_days": round_half_up(present, 2),
            "leave_days": a.get("leave_days", 0), "late_count": a.get("late_count", 0),
            "total_late_minutes": a.get("total_late_minutes", 0), "total_overtime_minutes": a.get("total_overtime_minutes", 0),
            "attendance_pct": pct}


@app.get("/analytics/departments/summary")
def department_summary(month: str, department: Optional[str] = None):
    start, end = valid_month(month)
    emp_match = {"joined_on": {"$lte": end.isoformat()}}
    if department is not None: emp_match["department"] = department
    pipeline = [
        {"$match": emp_match},
        {"$lookup": {"from":"attendance_logs","let":{"code":"$emp_code","joined":"$joined_on"},
          "pipeline":[{"$match":{"$expr":{"$and":[{"$eq":["$emp_code","$$code"]},{"$gte":["$date",start.isoformat()]},{"$lte":["$date",end.isoformat()]},{"$gte":["$date","$$joined"]}]}}},
          {"$group":{"_id":None,
             "present_days":{"$sum":{"$cond":[{"$and":[{"$in":["$status",list(PRESENCE)]},{"$lt":[{"$isoDayOfWeek":{"$dateFromString":{"dateString":"$date"}}},6]}]},{"$cond":[{"$eq":[{"$ifNull":["$half_day",False]},True]},0.5,1]},0]}},
             "avg_work_hours":{"$avg":{"$cond":[{"$and":[{"$in":["$status",list(PRESENCE)]},{"$ne":["$work_hours",None]}]},"$work_hours",None]}},
             "late_count":{"$sum":{"$cond":[{"$gt":[{"$ifNull":["$late_minutes",0]},0]},1,0]}},
             "total_late_minutes":{"$sum":{"$ifNull":["$late_minutes",0]}},
             "leave_count":{"$sum":{"$cond":[{"$eq":["$status","LEAVE"]},1,0]}},
             "on_duty_count":{"$sum":{"$cond":[{"$eq":["$status","ON_DUTY"]},1,0]}}
          }}],
          "as":"stats"}},
        {"$addFields":{"stats":{"$ifNull":[{"$arrayElemAt":["$stats",0]},{"present_days":0,"avg_work_hours":None,"late_count":0,"total_late_minutes":0,"leave_count":0,"on_duty_count":0}]}}},
        {"$group":{"_id":"$department","headcount":{"$sum":1},
          "present_days":{"$sum":"$stats.present_days"},
          "avg_work_hours":{"$push":"$stats.avg_work_hours"},
          "late_count":{"$sum":"$stats.late_count"},"total_late_minutes":{"$sum":"$stats.total_late_minutes"},
          "leave_count":{"$sum":"$stats.leave_count"},"on_duty_count":{"$sum":"$stats.on_duty_count"}}},
        {"$sort":{"_id":1}}
    ]
    rows = []
    for r in employees.aggregate(pipeline):
        # Average must be across records; re-aggregate directly by department for exact record-weighted mean.
        avg_pipe = [{"$match":{"date":{"$gte":start.isoformat(),"$lte":end.isoformat()},"status":{"$in":list(PRESENCE)}}},
                    {"$lookup":{"from":"employees","localField":"emp_code","foreignField":"emp_code","as":"e"}},
                    {"$unwind":"$e"},{"$match":{"e.department":r["_id"],"work_hours":{"$ne":None}}},
                    {"$group":{"_id":None,"avg":{"$avg":"$work_hours"}}}]
        av = list(logs.aggregate(avg_pipe))
        rows.append({"department":r["_id"],"headcount":r["headcount"],"present_days":round_half_up(r["present_days"],2),
          "avg_work_hours":round_half_up(av[0]["avg"],2) if av and av[0].get("avg") is not None else None,
          "late_count":r["late_count"],"total_late_minutes":r["total_late_minutes"],"leave_count":r["leave_count"],"on_duty_count":r["on_duty_count"]})
    return {"month":month,"items":rows}


@app.get("/analytics/leaderboard/late")
def late_leaderboard(month: str, limit: int = Query(10, ge=1, le=50), department: Optional[str] = None):
    start, end = valid_month(month)
    pipeline = [
      {"$match":{"date":{"$gte":start.isoformat(),"$lte":end.isoformat()}}},
      {"$group":{"_id":"$emp_code","total_late_minutes":{"$sum":{"$ifNull":["$late_minutes",0]}},"late_count":{"$sum":{"$cond":[{"$gt":[{"$ifNull":["$late_minutes",0]},0]},1,0]}}}},
      {"$match":{"total_late_minutes":{"$gt":0}}},
      {"$lookup":{"from":"employees","localField":"_id","foreignField":"emp_code","as":"employee"}},
      {"$unwind":"$employee"},
    ]
    if department is not None: pipeline.append({"$match":{"employee.department":department}})
    pipeline += [
      {"$setWindowFields":{"sortBy":{"total_late_minutes":-1},"output":{"rank":{"$rank":{}}}}},
      {"$match":{"rank":{"$lte":limit}}},
      {"$sort":{"total_late_minutes":-1,"_id":1}},
      {"$project":{"_id":0,"rank":1,"emp_code":"$_id","name":"$employee.name","department":"$employee.department","total_late_minutes":1,"late_count":1}}
    ]
    return {"month":month,"items":list(logs.aggregate(pipeline))}


@app.get("/analytics/departments/{department}/trend")
def department_trend(department: str, from_: str = Query(..., alias="from"), to: str = Query(...)):
    start = valid_date(from_, "from"); end = valid_date(to, "to")
    if end < start or (end-start).days+1 > 92: raise HTTPException(422, "invalid date range (maximum 92 days)")
    if not employees.find_one({"department":department}, {"_id":1}): raise HTTPException(404, "department not found")
    # Generate the requested calendar inside MongoDB, then left-join each employee's log.
    # Starting from employees guarantees rows exist even when a day (or the whole range) has no logs.
    pipeline = [
      {"$match": {"department": department, "joined_on": {"$lte": end.isoformat()}}},
      {"$addFields": {"_days": {"$map": {
          "input": {"$range": [0, (end-start).days + 1]}, "as": "i",
          "in": {"$dateAdd": {"startDate": {"$dateFromString": {"dateString": start.isoformat()}},
                              "unit": "day", "amount": "$$i"}}
      }}}},
      {"$unwind": "$_days"},
      {"$addFields": {"_date_str": {"$dateToString": {"date": "$_days", "format": "%Y-%m-%d", "timezone": "UTC"}}}},
      {"$lookup": {"from": "attendance_logs", "let": {"code": "$emp_code", "day": "$_date_str"},
          "pipeline": [{"$match": {"$expr": {"$and": [
              {"$eq": ["$emp_code", "$$code"]}, {"$eq": ["$date", "$$day"]}
          ]}}}], "as": "_logs"}},
      {"$unwind": {"path": "$_logs", "preserveNullAndEmptyArrays": True}},
      {"$group": {
          "_id": "$_date_str",
          "date_obj": {"$first": "$_days"},
          "headcount": {"$sum": {"$cond": [{"$lte": ["$joined_on", "$_date_str"]}, 1, 0]}},
          "present_count": {"$sum": {"$cond": [
              {"$and": [{"$lte": ["$joined_on", "$_date_str"]}, {"$in": ["$_logs.status", list(PRESENCE)]}]},
              {"$cond": [{"$eq": [{"$ifNull": ["$_logs.half_day", False]}, True]}, 0.5, 1]}, 0
          ]}},
          "late_count": {"$sum": {"$cond": [
              {"$and": [{"$lte": ["$joined_on", "$_date_str"]}, {"$gt": [{"$ifNull": ["$_logs.late_minutes", 0]}, 0]}]}, 1, 0
          ]}}
      }},
      {"$set": {"is_working_day": {"$lte": [{"$isoDayOfWeek": "$date_obj"}, 5]},
                "attendance_rate": {"$cond": [
                    {"$and": [{"$lte": [{"$isoDayOfWeek": "$date_obj"}, 5]}, {"$gt": ["$headcount", 0]}]},
                    {"$divide": ["$present_count", "$headcount"]}, None
                ]}}},
      {"$setWindowFields": {"sortBy": {"date_obj": 1}, "output": {
          "moving_avg_raw": {"$avg": "$attendance_rate", "window": {"documents": [-6, 0]}}
      }}},
      {"$project": {"_id": 0, "date": "$_id", "is_working_day": 1, "headcount": 1, "present_count": 1, "late_count": 1,
          "attendance_rate": {"$cond": [{"$eq": ["$attendance_rate", None]}, None, {"$divide": [{"$floor": {"$add": [{"$multiply": ["$attendance_rate", 10000]}, 0.5]}}, 10000]}]},
          "moving_avg_7d": {"$cond": [{"$eq": ["$moving_avg_raw", None]}, None, {"$divide": [{"$floor": {"$add": [{"$multiply": ["$moving_avg_raw", 10000]}, 0.5]}}, 10000]}]}}},
      {"$sort": {"date": 1}}
    ]
    return {"department":department,"items":list(employees.aggregate(pipeline))}


def explain_query(endpoint, params):
    if endpoint == "attendance_list":
        page=params.get("page",1); size=params.get("page_size",20)
        q={}
        if params.get("emp_code"): q["emp_code"]=params["emp_code"]
        if params.get("date_from") or params.get("date_to"):
            q["date"]={}
            if params.get("date_from"): q["date"]["$gte"]=params["date_from"]
            if params.get("date_to"): q["date"]["$lte"]=params["date_to"]
        if params.get("status"): q["status"]=params["status"]
        return "attendance_logs", logs.find(q).sort([("date",-1),("emp_code",1)]).skip((page-1)*size).limit(size).explain()
    if endpoint == "employee_monthly":
        if not params.get("emp_code") or not params.get("month"): raise HTTPException(422,"emp_code and month required")
        s,e=month_match(params["month"])
        p=[{"$match":{"emp_code":params["emp_code"],"date":{"$gte":s,"$lte":e}}},{"$group":{"_id":"$emp_code","late":{"$sum":{"$ifNull":["$late_minutes",0]}}}}]
        return "attendance_logs", logs.database.command("explain", {"aggregate":"attendance_logs","pipeline":p,"cursor":{}}, verbosity="executionStats")
    if endpoint in ("department_summary","late_leaderboard"):
        if not params.get("month"): raise HTTPException(422,"month required")
        s,e=month_match(params["month"])
        if endpoint=="late_leaderboard":
            p=[{"$match":{"date":{"$gte":s,"$lte":e}}},{"$group":{"_id":"$emp_code","total":{"$sum":{"$ifNull":["$late_minutes",0]}}}}]
            return "attendance_logs", logs.database.command("explain", {"aggregate":"attendance_logs","pipeline":p,"cursor":{}}, verbosity="executionStats")
        p=[{"$match":{"joined_on":{"$lte":e}}}]
        if params.get("department"): p[0]["$match"]["department"]=params["department"]
        return "employees", employees.database.command("explain", {"aggregate":"employees","pipeline":p,"cursor":{}}, verbosity="executionStats")
    if endpoint == "department_trend":
        if not params.get("department") or not params.get("from") or not params.get("to"): raise HTTPException(422,"department, from and to required")
        s=valid_date(params["from"]); e=valid_date(params["to"])
        if e<s or (e-s).days+1>92: raise HTTPException(422,"invalid date range")
        # Same indexed driving query as the real trend pipeline (employee-driven to preserve empty days).
        p=[{"$match":{"department":params["department"],"joined_on":{"$lte":e.isoformat()}}}]
        return "employees", employees.database.command("explain", {"aggregate":"employees","pipeline":p,"cursor":{}}, verbosity="executionStats")
    raise HTTPException(422,"unknown endpoint")


@app.get("/admin/explain/{endpoint}")
def admin_explain(endpoint: Literal["attendance_list","employee_monthly","department_summary","late_leaderboard","department_trend"],
                  emp_code: Optional[str]=None, month: Optional[str]=None, department: Optional[str]=None,
                  limit: int=Query(10,ge=1,le=50), date_from: Optional[str]=None, date_to: Optional[str]=None,
                  status: Optional[Literal["PRESENT","ABSENT","LEAVE","WFH","ON_DUTY"]]=None,
                  from_: Optional[str]=Query(None,alias="from"), to: Optional[str]=None,
                  page: int=Query(1,ge=1), page_size: int=Query(20,ge=1,le=100)):
    params={"emp_code":emp_code,"month":month,"department":department,"limit":limit,"date_from":date_from,"date_to":date_to,
            "status":status,"from":from_,"to":to,"page":page,"page_size":page_size}
    coll, exp = explain_query(endpoint,params)
    return {"endpoint":endpoint,"collection":coll,"explain":exp}

