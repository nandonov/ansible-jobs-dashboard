from __future__ import annotations

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Query
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from sqlalchemy import DateTime, Float, Integer, String, Text, create_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker
from datetime import datetime, timedelta
import os
import asyncio
import json
from pathlib import Path

DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./database.db")

if DATABASE_URL.startswith("sqlite:///"):
    db_relative = DATABASE_URL.replace("sqlite:///", "", 1)
    db_path = Path(db_relative)
    if not db_path.is_absolute():
        db_path = Path.cwd() / db_path
    db_path.parent.mkdir(parents=True, exist_ok=True)

engine = create_engine(DATABASE_URL, connect_args={"check_same_thread": False})
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


class Base(DeclarativeBase):
    pass

class Job(Base):
    __tablename__ = "jobs"
    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    job_name: Mapped[str] = mapped_column(String)
    scope: Mapped[str] = mapped_column(String)
    triggered_by: Mapped[str] = mapped_column(String)
    status: Mapped[str] = mapped_column(String, default="running")
    progress: Mapped[float] = mapped_column(Float, default=0)
    start_time: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    end_time: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

class JobLog(Base):
    __tablename__ = "job_logs"
    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    job_id: Mapped[int] = mapped_column(Integer, index=True)
    ts: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    level: Mapped[str] = mapped_column(String, default="info")
    message: Mapped[str] = mapped_column(Text)

Base.metadata.create_all(bind=engine)

app = FastAPI()

# Allow overriding CORS origins via env (comma-separated). Defaults to "*" for dev.
cors_env = os.getenv("BACKEND_CORS_ORIGINS", "*")
allow_origins = ["*"] if cors_env.strip() in ("*", "",) else [o.strip() for o in cors_env.split(",") if o.strip()]
app.add_middleware(
    CORSMiddleware,
    allow_origins=allow_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# WebSocket manager
class ConnectionManager:
    def __init__(self):
        self.active = []

    async def connect(self, websocket: WebSocket):
        await websocket.accept()
        self.active.append(websocket)

    def disconnect(self, websocket: WebSocket):
        try:
            self.active.remove(websocket)
        except ValueError:
            pass

    async def broadcast(self, message: dict):
        if not self.active:
            return
        data = json.dumps(message, default=str)
        await asyncio.gather(*[ws.send_text(data) for ws in list(self.active)], return_exceptions=True)

manager = ConnectionManager()

# Pydantic models
class StartPayload(BaseModel):
    job_name: str
    scope: str
    triggered_by: str
    job_id: str | None = None

class ProgressPayload(BaseModel):
    job_id: int
    progress: float | None = None
    message: str | None = None
    level: str | None = "info"

class CompletePayload(BaseModel):
    job_id: int
    status: str
    message: str | None = None

# Helpers
def job_to_dict(job: Job):
    return {
        "id": job.id,
        "job_name": job.job_name,
        "scope": job.scope,
        "triggered_by": job.triggered_by,
        "status": job.status,
        "progress": float(job.progress or 0),
        "start_time": (job.start_time.isoformat() if job.start_time else None),
        "end_time": (job.end_time.isoformat() if job.end_time else None),
    }

# API endpoints
@app.post("/api/jobs/start")
async def api_start(payload: StartPayload):
    # Use a session context to ensure connections are returned to the pool
    with SessionLocal() as db:
        new_job = Job(job_name=payload.job_name, scope=payload.scope, triggered_by=payload.triggered_by)
        db.add(new_job)
        db.commit()
        db.refresh(new_job)
        # optional initial log
        log = JobLog(job_id=new_job.id, message="Job started")
        db.add(log)
        db.commit()

        msg = {"type": "job_start", "job": job_to_dict(new_job)}
        await manager.broadcast(msg)
        return {"job_id": new_job.id}

@app.post("/api/jobs/progress")
async def api_progress(payload: ProgressPayload):
    with SessionLocal() as db:
        job = db.query(Job).filter(Job.id == payload.job_id).first()
        if not job:
            return JSONResponse(status_code=404, content={"error": "job not found"})
        if payload.progress is not None:
            job.progress = payload.progress
        if payload.message:
            log = JobLog(job_id=payload.job_id, message=payload.message, level=payload.level or "info")
            db.add(log)
        db.commit()
        db.refresh(job)
        msg = {"type": "job_progress", "job": job_to_dict(job)}
        await manager.broadcast(msg)
        # also broadcast log if present
        if payload.message:
            await manager.broadcast({"type": "job_log", "log": {"job_id": payload.job_id, "message": payload.message, "level": payload.level, "ts": datetime.utcnow().isoformat()}})
        return {"ok": True}

@app.post("/api/jobs/complete")
async def api_complete(payload: CompletePayload):
    with SessionLocal() as db:
        job = db.query(Job).filter(Job.id == payload.job_id).first()
        if not job:
            return JSONResponse(status_code=404, content={"error": "job not found"})
        job.status = payload.status
        job.progress = 100.0
        job.end_time = datetime.utcnow()
        if payload.message:
            log = JobLog(job_id=payload.job_id, message=payload.message, level="info")
            db.add(log)
        db.commit()
        db.refresh(job)
        await manager.broadcast({"type": "job_complete", "job": job_to_dict(job)})
        return {"ok": True}

@app.get("/api/jobs")
def api_jobs(range: str = Query("24h")):
    with SessionLocal() as db:
        now = datetime.utcnow()
        if range == "24h":
            cutoff = now - timedelta(hours=24)
        elif range == "7d":
            cutoff = now - timedelta(days=7)
        elif range == "30d":
            cutoff = now - timedelta(days=30)
        else:
            cutoff = datetime.min
        rows = db.query(Job).filter(Job.start_time >= cutoff).order_by(Job.start_time.desc()).all()
        return {"jobs": [job_to_dict(r) for r in rows]}

@app.get("/api/jobs/{job_id}/logs")
def api_job_logs(job_id: int, limit: int = 100, offset: int = 0):
    """
    Return logs for a job, oldest-first.
    - limit: max number of log entries to return. If <= 0, return all.
    - offset: number of entries to skip from the start (for pagination).
    """
    with SessionLocal() as db:
        q = db.query(JobLog).filter(JobLog.job_id == job_id).order_by(JobLog.ts.asc())
        if offset and offset > 0:
            q = q.offset(offset)
        if limit and limit > 0:
            q = q.limit(limit)
        rows = q.all()
        return {"logs": [{"ts": r.ts.isoformat(), "level": r.level, "message": r.message} for r in rows]}

@app.websocket("/ws")
async def websocket(ws: WebSocket):
    await manager.connect(ws)
    try:
        while True:
            # server doesn't need inbound messages for now
            await ws.receive_text()
    except WebSocketDisconnect:
        manager.disconnect(ws)