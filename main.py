from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo
import json
from typing import Any

from fastapi import FastAPI, Depends, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from sqlalchemy.orm import Session
from sqlalchemy import text
from auth import issue_token, verify_password, password_hash, current_user, require_roles

from database import engine, Base, SessionLocal
import models


app = FastAPI(
    title="Resource Allocation Dashboard API",
    description="Persistent backend for the VMI Resource Management dashboard",
    version="2.0.0"
)

# Allows the local HTML dashboard to call this API from a browser.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.on_event("startup")
def create_tables_and_migrate():
    Base.metadata.create_all(bind=engine)
    # Add columns introduced after the first version of the database.
    # Seed separate role accounts on first startup. Override these values in backend/.env.
    import os
    from auth import password_hash
    with SessionLocal() as db:
        defaults = [
            (os.getenv("RESOURCES_USERNAME", "resources"), os.getenv("RESOURCES_PASSWORD", "Resource@123"), "Resources"),
            (os.getenv("ARCHITECT_USERNAME", "architect"), os.getenv("ARCHITECT_PASSWORD", "Architect@123"), "Architect"),
            (os.getenv("RMG_USERNAME", "rmg"), os.getenv("RMG_PASSWORD", "Rmg@123"), "RMG"),
        ]
        for username, password, role in defaults:
            if not db.query(models.User).filter_by(username=username).first():
                db.add(models.User(username=username, password_hash=password_hash(password), role=role))
        db.commit()
    with engine.begin() as connection:
        connection.execute(text(
            "ALTER TABLE employees ADD COLUMN IF NOT EXISTS profile_data TEXT NOT NULL DEFAULT '{}'"
        ))
        connection.execute(text(
            "ALTER TABLE assignments ADD COLUMN IF NOT EXISTS link TEXT"
        ))
        # The original dashboard creates employee IDs with JavaScript Date.now(),
        # which can exceed PostgreSQL's 32-bit INTEGER range. Upgrade both
        # employee identifiers to BIGINT so dashboard snapshots can be saved.
        connection.execute(text("ALTER TABLE employees ALTER COLUMN id TYPE BIGINT"))
        connection.execute(text("ALTER TABLE assignments ALTER COLUMN employee_id TYPE BIGINT"))


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


class LoginRequest(BaseModel):
    username: str
    password: str

@app.post("/auth/login")
def login(data: LoginRequest, db: Session = Depends(get_db)):
    user = db.query(models.User).filter_by(username=data.username).first()
    if not user or not verify_password(data.password, user.password_hash):
        raise HTTPException(status_code=401, detail="Incorrect username or password")
    return {"access_token": issue_token(user.username, user.role), "token_type": "bearer", "role": user.role, "username": user.username}

@app.get("/auth/me")
def who_am_i(user=Depends(current_user)):
    return user

@app.get("/")
def home():
    return {"message": "Resource Dashboard Backend is running!"}


@app.get("/db-test")
def database_test():
    try:
        with engine.connect():
            return {"status": "success", "message": "PostgreSQL connected successfully!"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


def employee_to_dict(employee):
    try:
        profile = json.loads(employee.profile_data or "{}")
    except (TypeError, json.JSONDecodeError):
        profile = {}
    return {
        "id": employee.id,
        "name": employee.name,
        "email": employee.email or "",
        "tech": profile.get("tech", [employee.technology] if employee.technology else []),
        "exp": employee.experience or 0,
        "vmiExp": profile.get("vmiExp", 0),
        "projectName": profile.get("projectName", ""),
        "projectDesc": profile.get("projectDesc", ""),
        "status": profile.get("status", "Active"),
        "avatar": profile.get("avatar", (employee.name or "NA")[:2].upper()),
        "bio": profile.get("bio", ""),
        "loc": profile.get("loc", ""),
        "li": profile.get("li", ""),
        "gh": profile.get("gh", ""),
        "photo": profile.get("photo", "")
    }


@app.get("/employees")
def get_employees(db: Session = Depends(get_db), user=Depends(current_user)):
    return [employee_to_dict(e) for e in db.query(models.Employee).order_by(models.Employee.id).all()]


@app.get("/assignments")
def get_assignments(db: Session = Depends(get_db), user=Depends(current_user)):
    result = []
    for a in db.query(models.Assignment).all():
        result.append({
            "id": a.id, "employee_id": a.employee_id,
            "task_title": a.task_title, "task_details": a.task_details or "",
            "link": a.link or "", "start_date": a.start_date.isoformat(),
            "end_date": a.end_date.isoformat()
        })
    return result


class DashboardSnapshot(BaseModel):
    employees: list[dict[str, Any]]
    allocations: dict[str, dict[str, Any]]


@app.get("/dashboard")
def get_dashboard(db: Session = Depends(get_db), user=Depends(current_user)):
    employees = [employee_to_dict(e) for e in db.query(models.Employee).order_by(models.Employee.id).all()]
    allocations = {}
    for a in db.query(models.Assignment).all():
        current = a.start_date
        # The dashboard stores one assignment for each employee/date cell.
        while current <= a.end_date:
            local_midnight = datetime.combine(current, time.min, ZoneInfo("Asia/Kolkata"))
            key = f"{a.employee_id}-{int(local_midnight.timestamp() * 1000)}"
            allocations[key] = {"title": a.task_title, "desc": a.task_details or "", "link": a.link or ""}
            current = date.fromordinal(current.toordinal() + 1)
    return {"employees": employees, "allocations": allocations}


@app.put("/dashboard")
def save_dashboard(snapshot: DashboardSnapshot, db: Session = Depends(get_db), user=Depends(require_roles("Architect", "RMG"))):
    try:
        # Validate the incoming snapshot before changing any saved data.
        employee_ids = {int(item["id"]) for item in snapshot.employees if item.get("id") is not None}
        proposed_slots = set()
        for key in snapshot.allocations:
            try:
                employee_id, timestamp = key.split("-", 1)
                employee_id = int(employee_id)
                assigned_date = datetime.fromtimestamp(
                    int(timestamp) / 1000, ZoneInfo("Asia/Kolkata")
                ).date()
            except (ValueError, TypeError, OverflowError):
                raise HTTPException(status_code=400, detail=f"Invalid allocation key: {key}")

            if employee_id not in employee_ids:
                raise HTTPException(
                    status_code=400,
                    detail=f"Assignment refers to employee {employee_id}, who is not in this dashboard."
                )
            slot = (employee_id, assigned_date)
            if slot in proposed_slots:
                raise HTTPException(
                    status_code=409,
                    detail=f"Employee {employee_id} has more than one assignment on {assigned_date}."
                )
            proposed_slots.add(slot)

        # Reconcile records individually in one transaction; do not wipe/recreate tables.
        incoming_ids = {int(item["id"]) for item in snapshot.employees if item.get("id") is not None}
        existing_employees = {e.id: e for e in db.query(models.Employee).all()}
        for old_id in set(existing_employees) - incoming_ids:
            db.query(models.Assignment).filter(models.Assignment.employee_id == old_id).delete()
            db.delete(existing_employees[old_id])
        for item in snapshot.employees:
            tech = item.get("tech", [])
            profile = {
                "tech": tech,
                "vmiExp": item.get("vmiExp", 0),
                "projectName": item.get("projectName", ""),
                "projectDesc": item.get("projectDesc", ""),
                "status": item.get("status", "Active"),
                "avatar": item.get("avatar", ""),
                "bio": item.get("bio", ""),
                "loc": item.get("loc", ""),
                "li": item.get("li", ""),
                "gh": item.get("gh", ""),
                "photo": item.get("photo", "")
            }
            employee_id = int(item["id"])
            employee = existing_employees.get(employee_id)
            if employee is None:
                employee = models.Employee(id=employee_id, name=item.get("name", "Unnamed"))
                db.add(employee)
            employee.name = item.get("name", "Unnamed")
            employee.email = item.get("email") or None
            employee.technology = ", ".join(tech) if isinstance(tech, list) else str(tech)
            employee.experience = int(item.get("exp", 0))
            employee.profile_data = json.dumps(profile)

        # Upsert one-day allocation records and remove only cells removed by the UI.
        wanted = {}
        for key, allocation in snapshot.allocations.items():
            try:
                employee_id, timestamp = key.split("-", 1)
                assigned_date = datetime.fromtimestamp(int(timestamp) / 1000, ZoneInfo("Asia/Kolkata")).date()
                wanted[(int(employee_id), assigned_date)] = allocation
            except (ValueError, TypeError):
                continue
        existing_slots = {(a.employee_id, a.start_date): a for a in db.query(models.Assignment).all() if a.start_date == a.end_date}
        for slot, record in existing_slots.items():
            if slot not in wanted:
                db.delete(record)
        for (employee_id, assigned_date), allocation in wanted.items():
            record = existing_slots.get((employee_id, assigned_date))
            if record is None:
                record = models.Assignment(employee_id=employee_id, start_date=assigned_date, end_date=assigned_date, task_title="Assignment")
                db.add(record)
            record.task_title = str(allocation.get("title", "Assignment"))[:200]
            record.task_details = allocation.get("desc", "")
            record.link = allocation.get("link", "")

        db.commit()
        return {"status": "success", "message": "Dashboard saved to PostgreSQL"}
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=f"Could not save dashboard: {str(e)}")


# Keep the original simple endpoints available for API/Swagger testing.
@app.post("/employees")
def create_employee(name: str, email: str = None, technology: str = "", experience: int = 0,
                    db: Session = Depends(get_db), user=Depends(require_roles("RMG"))):
    employee = models.Employee(name=name, email=email, technology=technology, experience=experience)
    db.add(employee)
    db.commit()
    db.refresh(employee)
    return employee_to_dict(employee)


@app.delete("/employees/{employee_id}")
def delete_employee(employee_id: int, db: Session = Depends(get_db), user=Depends(require_roles("RMG"))):
    employee = db.query(models.Employee).filter(models.Employee.id == employee_id).first()
    if not employee:
        raise HTTPException(status_code=404, detail="Employee not found")
    db.query(models.Assignment).filter(models.Assignment.employee_id == employee_id).delete()
    db.delete(employee)
    db.commit()
    return {"message": "Employee deleted successfully"}


@app.post("/assignments")
def create_assignment(employee_id: int, task_title: str, task_details: str = "",
                      start_date: str = "", end_date: str = "", db: Session = Depends(get_db), user=Depends(require_roles("Architect"))):
    try:
        start = date.fromisoformat(start_date)
        end = date.fromisoformat(end_date)
        if end < start:
            raise ValueError()
    except ValueError:
        raise HTTPException(status_code=400, detail="Dates must use YYYY-MM-DD and end_date must not be before start_date")
    employee = db.query(models.Employee).filter(models.Employee.id == employee_id).first()
    if not employee:
        raise HTTPException(status_code=404, detail="Employee not found")

    # Two assignments for the same employee may not share even one calendar day.
    overlap = db.query(models.Assignment).filter(
        models.Assignment.employee_id == employee_id,
        models.Assignment.start_date <= end,
        models.Assignment.end_date >= start
    ).first()
    if overlap:
        raise HTTPException(
            status_code=409,
            detail=(
                f"Assignment conflict: this employee is already assigned to "
                f"'{overlap.task_title}' from {overlap.start_date} to {overlap.end_date}."
            )
        )

    assignment = models.Assignment(employee_id=employee_id, task_title=task_title,
                                   task_details=task_details, start_date=start, end_date=end)
    db.add(assignment)
    db.commit()
    db.refresh(assignment)
    return {"id": assignment.id, "employee_id": employee_id, "task_title": task_title}


class EmployeeUpdate(BaseModel):
    name: str
    email: str | None = None
    technology: str = ""
    experience: int = 0
    profile_data: dict[str, Any] = {}

@app.put("/employees/{employee_id}")
def update_employee(employee_id: int, data: EmployeeUpdate, db: Session = Depends(get_db), user=Depends(require_roles("RMG"))):
    employee = db.query(models.Employee).filter_by(id=employee_id).first()
    if not employee:
        raise HTTPException(status_code=404, detail="Employee not found")
    if data.experience < 0:
        raise HTTPException(status_code=400, detail="Experience cannot be negative")
    employee.name = data.name.strip()
    employee.email = data.email
    employee.technology = data.technology
    employee.experience = data.experience
    employee.profile_data = json.dumps(data.profile_data)
    db.commit(); db.refresh(employee)
    return employee_to_dict(employee)

@app.get("/analytics")
def analytics(db: Session = Depends(get_db), user=Depends(current_user)):
    employees = db.query(models.Employee).all()
    active = [e for e in employees if json.loads(e.profile_data or "{}").get("status", "Active") != "Inactive"]
    assignments = db.query(models.Assignment).all()
    allocated_ids = {a.employee_id for a in assignments}
    tech_counts = {}
    for employee in active:
        for tech in json.loads(employee.profile_data or "{}").get("tech", [employee.technology] if employee.technology else []):
            tech_counts[tech] = tech_counts.get(tech, 0) + 1
    project_counts = {}
    for assignment in assignments:
        project_counts[assignment.task_title] = project_counts.get(assignment.task_title, 0) + 1
    allocated = len(allocated_ids.intersection({e.id for e in active}))
    return {"total_employees": len(active), "allocated_resources": allocated,
            "available_resources": max(0, len(active)-allocated),
            "utilization_percent": round(allocated/len(active)*100) if active else 0,
            "technology_distribution": tech_counts, "project_allocation": project_counts}

@app.delete("/assignments/{assignment_id}")
def delete_assignment(assignment_id: int, db: Session = Depends(get_db), user=Depends(require_roles("Architect"))):
    assignment = db.query(models.Assignment).filter_by(id=assignment_id).first()
    if not assignment:
        raise HTTPException(status_code=404, detail="Assignment not found")
    db.delete(assignment)
    db.commit()
    return {"message": "Assignment deleted successfully"}
