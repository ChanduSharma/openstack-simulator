"""Failure-injection control plane (port 8999).

Rules created here are picked up by every service's scenario middleware within a second,
letting clients be tested against timeouts, 429s, 500s and quota exhaustion on demand.
"""
from __future__ import annotations

from datetime import timedelta
from typing import Any

from fastapi import APIRouter, Depends, Response
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import gen_id, iso, now_utc
from app.core.database import get_session
from app.core.middleware import fault, invalidate_scenario_cache
from app.models.failure import VALID_ACTIONS, VALID_SERVICES, FailureInjection

SERVICE = "scenarios"
router = APIRouter()

ACTION_HELP: dict[str, str] = {
    "500_error": "Return an Unexpected API Error (500) in the target service's dialect.",
    "503_error": "Return Service Unavailable (503).",
    "rate_limit": "Return 429 with a Retry-After header (params.retry_after).",
    "latency": "Delay the request by latency_ms, then let it through.",
    "timeout": "Hang for latency_ms (default 30000) and return 504.",
    "quota_exhausted": "Return 403 quota exceeded (params.resource names the quota).",
}


class ScenarioPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    service: str
    action: str
    duration_seconds: int = 30
    path_contains: str | None = None
    method: str | None = None
    probability: float = 1.0
    latency_ms: int = 0
    message: str | None = None
    params: dict[str, Any] = Field(default_factory=dict)

    @field_validator("service")
    @classmethod
    def _known_service(cls, value: str) -> str:
        if value not in VALID_SERVICES:
            raise ValueError(f"service must be one of {', '.join(VALID_SERVICES)}")
        return value

    @field_validator("action")
    @classmethod
    def _known_action(cls, value: str) -> str:
        if value not in VALID_ACTIONS:
            raise ValueError(f"action must be one of {', '.join(VALID_ACTIONS)}")
        return value

    @field_validator("probability")
    @classmethod
    def _sane_probability(cls, value: float) -> float:
        if not 0.0 <= value <= 1.0:
            raise ValueError("probability must be between 0.0 and 1.0")
        return value

    @field_validator("duration_seconds")
    @classmethod
    def _sane_duration(cls, value: int) -> int:
        if value <= 0:
            raise ValueError("duration_seconds must be positive")
        return value


def scenario_dict(rule: FailureInjection) -> dict[str, Any]:
    remaining = (rule.expires_at - now_utc()).total_seconds()
    return {
        "id": rule.id,
        "service": rule.service,
        "action": rule.action,
        "path_contains": rule.path_contains,
        "method": rule.method,
        "probability": rule.probability,
        "latency_ms": rule.latency_ms,
        "message": rule.message,
        "params": dict(rule.params or {}),
        "duration_seconds": rule.duration_seconds,
        "created_at": iso(rule.created_at),
        "expires_at": iso(rule.expires_at),
        "remaining_seconds": round(max(remaining, 0.0), 1),
        "active": rule.active and remaining > 0,
        "hits": rule.hits,
    }


@router.get("/")
async def index() -> dict[str, Any]:
    return {
        "name": "openstack-sim scenarios",
        "versions": [{"id": "v1", "status": "CURRENT", "links": [{"rel": "self", "href": "/v1"}]}],
    }


@router.get("/v1/scenarios/actions")
async def list_actions() -> dict[str, Any]:
    return {
        "services": list(VALID_SERVICES),
        "actions": [{"action": name, "description": ACTION_HELP[name]} for name in VALID_ACTIONS],
        "example": {
            "service": "nova",
            "action": "500_error",
            "duration_seconds": 30,
        },
    }


@router.get("/v1/scenarios")
async def list_scenarios(
    include_expired: bool = False, session: AsyncSession = Depends(get_session)
) -> dict[str, Any]:
    stmt = select(FailureInjection).order_by(FailureInjection.created_at.desc())
    if not include_expired:
        stmt = stmt.where(
            FailureInjection.active.is_(True), FailureInjection.expires_at > now_utc()
        )
    rules = (await session.execute(stmt)).scalars().all()
    return {"scenarios": [scenario_dict(r) for r in rules]}


@router.post("/v1/scenarios", status_code=201)
async def create_scenario(
    payload: ScenarioPayload, session: AsyncSession = Depends(get_session)
) -> dict[str, Any]:
    """e.g. ``{"service": "nova", "action": "500_error", "duration_seconds": 30}``."""
    latency = payload.latency_ms
    if payload.action == "latency" and latency <= 0:
        latency = 2000
    if payload.action == "timeout" and latency <= 0:
        latency = 30000

    rule = FailureInjection(
        id=gen_id(),
        service=payload.service,
        action=payload.action,
        path_contains=payload.path_contains,
        method=payload.method,
        probability=payload.probability,
        latency_ms=latency,
        message=payload.message,
        params=payload.params,
        duration_seconds=payload.duration_seconds,
        expires_at=now_utc() + timedelta(seconds=payload.duration_seconds),
    )
    session.add(rule)
    await session.commit()
    invalidate_scenario_cache()
    return {"scenario": scenario_dict(rule)}


@router.get("/v1/scenarios/{scenario_id}")
async def get_scenario(
    scenario_id: str, session: AsyncSession = Depends(get_session)
) -> dict[str, Any]:
    rule = await session.get(FailureInjection, scenario_id)
    if rule is None:
        raise fault(SERVICE, 404, f"Scenario {scenario_id} not found.")
    return {"scenario": scenario_dict(rule)}


@router.delete("/v1/scenarios/{scenario_id}", status_code=204)
async def delete_scenario(
    scenario_id: str, session: AsyncSession = Depends(get_session)
) -> Response:
    rule = await session.get(FailureInjection, scenario_id)
    if rule is None:
        raise fault(SERVICE, 404, f"Scenario {scenario_id} not found.")
    await session.delete(rule)
    await session.commit()
    invalidate_scenario_cache()
    return Response(status_code=204)


@router.delete("/v1/scenarios", status_code=200)
async def clear_scenarios(session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    rules = (
        await session.execute(
            select(FailureInjection).where(FailureInjection.active.is_(True))
        )
    ).scalars().all()
    for rule in rules:
        await session.delete(rule)
    await session.commit()
    invalidate_scenario_cache()
    return {"cleared": len(rules)}
