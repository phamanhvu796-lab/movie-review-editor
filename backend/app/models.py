import uuid
from datetime import datetime, timezone

from sqlalchemy import JSON, Column, DateTime, Float, ForeignKey, Integer, String, Text
from sqlalchemy.orm import declarative_base, relationship

Base = declarative_base()


def _uid() -> str:
    return uuid.uuid4().hex


def _now():
    return datetime.now(timezone.utc)


class Project(Base):
    __tablename__ = "projects"
    id = Column(String(32), primary_key=True, default=_uid)
    user_id = Column(String(64), default="local")
    title = Column(Text, nullable=False)
    movie_title = Column(Text)
    genre = Column(Text)
    language = Column(String(10), default="vi")
    target_duration_seconds = Column(Integer, default=480)
    aspect_ratio = Column(String(10), default="16:9")
    status = Column(String(30), default="draft")   # draft|analyzing|plan_ready|rendering|completed|failed
    error_message = Column(Text)
    settings = Column(JSON, default=dict)
    created_at = Column(DateTime(timezone=True), default=_now)
    updated_at = Column(DateTime(timezone=True), default=_now, onupdate=_now)
    assets = relationship("Asset", cascade="all, delete-orphan")
    cues = relationship("SrtCue", cascade="all, delete-orphan")
    plans = relationship("EditPlanRow", cascade="all, delete-orphan")
    renders = relationship("Render", cascade="all, delete-orphan")


class Asset(Base):
    __tablename__ = "assets"
    id = Column(String(32), primary_key=True, default=_uid)
    project_id = Column(String(32), ForeignKey("projects.id", ondelete="CASCADE"), index=True)
    asset_type = Column(String(30), nullable=False)  # footage|broll|voice|music|logo|image|subtitle|lut|font
    file_name = Column(Text, nullable=False)
    file_path = Column(Text, nullable=False)
    size_bytes = Column(Integer)
    duration_seconds = Column(Float)
    width = Column(Integer)
    height = Column(Integer)
    processing_status = Column(String(20), default="ready")   # processing|ready|failed
    meta = Column(JSON, default=dict)                          # scenes, description, error...
    created_at = Column(DateTime(timezone=True), default=_now)


class SrtCue(Base):
    __tablename__ = "srt_cues"
    id = Column(String(32), primary_key=True, default=_uid)
    project_id = Column(String(32), ForeignKey("projects.id", ondelete="CASCADE"), index=True)
    cue_index = Column(Integer, nullable=False)
    start_seconds = Column(Float, nullable=False)
    end_seconds = Column(Float, nullable=False)
    text = Column(Text, nullable=False)
    language = Column(String(10), default="vi")


class EditPlanRow(Base):
    __tablename__ = "edit_plans"
    id = Column(String(32), primary_key=True, default=_uid)
    project_id = Column(String(32), ForeignKey("projects.id", ondelete="CASCADE"), index=True)
    version = Column(Integer, nullable=False, default=1)
    plan = Column(JSON, nullable=False)
    validation_status = Column(String(30), default="pending")  # pending|valid|invalid|needs_manual_review
    validation_errors = Column(JSON, default=list)
    created_at = Column(DateTime(timezone=True), default=_now)


class Render(Base):
    __tablename__ = "renders"
    id = Column(String(32), primary_key=True, default=_uid)
    project_id = Column(String(32), ForeignKey("projects.id", ondelete="CASCADE"), index=True)
    edit_plan_id = Column(String(32), ForeignKey("edit_plans.id"))
    format = Column(String(10), nullable=False)
    resolution = Column(String(20), nullable=False)
    status = Column(String(30), default="queued")   # queued|running|completed|failed
    progress = Column(Integer, default=0)
    options = Column(JSON, default=dict)
    output_path = Column(Text)
    error_message = Column(Text)
    created_at = Column(DateTime(timezone=True), default=_now)
    completed_at = Column(DateTime(timezone=True))


def to_dict(o, exclude=()):
    return {c.name: getattr(o, c.name) for c in o.__table__.columns if c.name not in exclude}
