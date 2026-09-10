"""Failure-injection rules consumed by the global scenario middleware."""
from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import JSON, Boolean, DateTime, Float, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.core.config import gen_id, now_utc
from app.core.database import Base

# Recognised failure actions.
ACTION_500 = "500_error"
ACTION_503 = "503_error"
ACTION_429 = "rate_limit"
ACTION_LATENCY = "latency"
ACTION_TIMEOUT = "timeout"
ACTION_QUOTA = "quota_exhausted"

VALID_ACTIONS: tuple[str, ...] = (
    ACTION_500,
    ACTION_503,
    ACTION_429,
    ACTION_LATENCY,
    ACTION_TIMEOUT,
    ACTION_QUOTA,
)

# Services a scenario may target ("all" matches every service).
VALID_SERVICES: tuple[str, ...] = (
    "all",
    "keystone",
    "nova",
    "cinder",
    "glance",
    "neutron",
    "placement",
    "octavia",
    "swift",
    "cloudkitty",
)


class FailureInjection(Base):
    __tablename__ = "failure_injections"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=gen_id)
    service: Mapped[str] = mapped_column(String(32), index=True)
    action: Mapped[str] = mapped_column(String(32))
    # Only requests whose path contains this fragment are hit (None = whole service).
    path_contains: Mapped[str | None] = mapped_column(String(255), nullable=True)
    method: Mapped[str | None] = mapped_column(String(16), nullable=True)
    # 0.0-1.0 chance of firing per matching request.
    probability: Mapped[float] = mapped_column(Float, default=1.0)
    latency_ms: Mapped[int] = mapped_column(Integer, default=0)
    message: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    params: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)

    duration_seconds: Mapped[int] = mapped_column(Integer, default=30)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now_utc)
    expires_at: Mapped[datetime] = mapped_column(DateTime, index=True)
    hits: Mapped[int] = mapped_column(Integer, default=0)
    active: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
