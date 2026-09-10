"""Cross-cutting HTTP concerns: app factory, microversion headers, token resolution
and the global failure-injection hook."""
from __future__ import annotations

import asyncio
import random
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from types import UnionType
from typing import Any, Union, get_args, get_origin

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, model_validator
from sqlalchemy import select, update
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.cors import CORSMiddleware

from app.core.config import API_VERSIONS, now_utc, settings
from app.core.database import SessionLocal
from app.models.failure import FailureInjection
from app.models.identity import Project, Token, User

# --------------------------------------------------------------------------------------
# Service-shaped error bodies
# --------------------------------------------------------------------------------------

_NOVA_KEYS: dict[int, str] = {
    400: "badRequest",
    401: "unauthorized",
    403: "forbidden",
    404: "itemNotFound",
    405: "badMethod",
    409: "conflictingRequest",
    413: "overLimit",
    429: "overLimit",
    500: "computeFault",
    501: "notImplemented",
    503: "serviceUnavailable",
    504: "gatewayTimeout",
}

_TITLES: dict[int, str] = {
    400: "Bad Request",
    401: "Unauthorized",
    403: "Forbidden",
    404: "Not Found",
    405: "Method Not Allowed",
    409: "Conflict",
    413: "Request Entity Too Large",
    429: "Too Many Requests",
    500: "Internal Server Error",
    501: "Not Implemented",
    503: "Service Unavailable",
    504: "Gateway Timeout",
}


def error_body(service: str, status: int, message: str, **extra: Any) -> dict[str, Any]:
    """Render an error payload in the dialect the given service actually speaks."""
    title = _TITLES.get(status, "Error")
    if service in ("nova", "cinder"):
        key = _NOVA_KEYS.get(status, "computeFault")
        body: dict[str, Any] = {key: {"message": message, "code": status}}
        if extra.get("retry_after"):
            body[key]["retryAfter"] = extra["retry_after"]
        return body
    if service == "neutron":
        return {
            "NeutronError": {
                "type": extra.get("type", title.replace(" ", "")),
                "message": message,
                "detail": extra.get("detail", ""),
            }
        }
    if service == "keystone":
        return {"error": {"code": status, "title": title, "message": message}}
    if service == "placement":
        return {
            "errors": [
                {
                    "status": status,
                    "title": title,
                    "detail": message,
                    "code": extra.get("code", "placement.undefined_code"),
                    "request_id": extra.get("request_id", ""),
                }
            ]
        }
    if service == "octavia":
        return {
            "faultcode": "Client" if status < 500 else "Server",
            "faultstring": message,
            "debuginfo": None,
        }
    if service == "glance":
        return {"message": message, "code": status, "title": title}
    return {"error": {"code": status, "title": title, "message": message}}


def fault(
    service: str,
    status: int,
    message: str,
    headers: dict[str, str] | None = None,
    **extra: Any,
) -> HTTPException:
    """Build an HTTPException whose detail is already a service-shaped body."""
    return HTTPException(
        status_code=status,
        detail=error_body(service, status, message, **extra),
        headers=headers,
    )


# --------------------------------------------------------------------------------------
# Request body base model
# --------------------------------------------------------------------------------------


def _accepts_none(annotation: Any) -> bool:
    if annotation is None or annotation is type(None) or annotation is Any:
        return True
    origin = get_origin(annotation)
    if origin is Union or origin is UnionType:
        return any(_accepts_none(arg) for arg in get_args(annotation))
    return False


class OSPayload(BaseModel):
    """Base for request bodies.

    OpenStack clients happily send ``"availability_zone": null`` for options the user
    never set. Pydantic would reject those against a non-optional field, so nulls are
    dropped here and the field's own default applies instead. Unknown keys are kept:
    the real APIs carry a long tail of extensions and vendor attributes.
    """

    model_config = ConfigDict(extra="allow", populate_by_name=True)

    @model_validator(mode="before")
    @classmethod
    def _drop_meaningless_nulls(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        nullable: set[str] = set()
        for name, info in cls.model_fields.items():
            if _accepts_none(info.annotation):
                nullable.add(name)
                if info.alias:
                    nullable.add(info.alias)
        return {
            key: value
            for key, value in data.items()
            if value is not None or key in nullable or key not in cls.model_fields
        }


# --------------------------------------------------------------------------------------
# Auth context / token resolution
# --------------------------------------------------------------------------------------


@dataclass(slots=True)
class AuthContext:
    token_id: str
    user_id: str
    user_name: str
    project_id: str
    project_name: str
    roles: list[str] = field(default_factory=list)

    @property
    def is_admin(self) -> bool:
        return "admin" in self.roles


async def resolve_token(token_id: str) -> AuthContext | None:
    """Look a bearer token up and hydrate the project/user it is scoped to."""
    if not token_id:
        return None
    async with SessionLocal() as session:
        token = await session.get(Token, token_id)
        if token is None or token.revoked or token.expires_at <= now_utc():
            return None
        user = await session.get(User, token.user_id)
        project = await session.get(Project, token.project_id) if token.project_id else None
        if user is None or project is None:
            return None
        return AuthContext(
            token_id=token.id,
            user_id=user.id,
            user_name=user.name,
            project_id=project.id,
            project_name=project.name,
            roles=list(token.roles or []),
        )


def auth_dependency(service: str) -> Callable[[Request], Awaitable[AuthContext]]:
    """Build a FastAPI dependency that enforces (or fakes) X-Auth-Token for a service."""

    async def _dependency(request: Request) -> AuthContext:
        token_id = request.headers.get("X-Auth-Token", "")
        ctx = await resolve_token(token_id)
        if ctx is not None:
            request.state.auth = ctx
            return ctx
        if settings.require_auth:
            raise fault(
                service,
                401,
                "The request you have made requires authentication.",
                headers={"WWW-Authenticate": "Keystone uri='%s'" % settings.advertise_host},
            )
        ctx = await _anonymous_context()
        request.state.auth = ctx
        return ctx

    return _dependency


async def _anonymous_context() -> AuthContext:
    """Fallback identity used when OSSIM_REQUIRE_AUTH=0 (handy for curl-driven demos)."""
    async with SessionLocal() as session:
        project = (
            await session.execute(
                select(Project).where(Project.name == settings.admin_project)
            )
        ).scalar_one_or_none()
        user = (
            await session.execute(select(User).where(User.name == settings.admin_user))
        ).scalar_one_or_none()
    return AuthContext(
        token_id="anonymous",
        user_id=user.id if user else "anonymous",
        user_name=user.name if user else "anonymous",
        project_id=project.id if project else "anonymous",
        project_name=project.name if project else "anonymous",
        roles=["admin", "member", "reader"],
    )


# --------------------------------------------------------------------------------------
# Failure injection
# --------------------------------------------------------------------------------------

_CACHE_TTL_SECONDS = 1.0
_scenario_cache: dict[str, Any] = {"at": 0.0, "rules": []}


def invalidate_scenario_cache() -> None:
    _scenario_cache["at"] = 0.0


async def active_rules() -> list[dict[str, Any]]:
    """Active injections, cached for a second so hot paths don't hammer SQLite."""
    now = time.monotonic()
    if now - float(_scenario_cache["at"]) < _CACHE_TTL_SECONDS:
        return list(_scenario_cache["rules"])
    async with SessionLocal() as session:
        rows = (
            (
                await session.execute(
                    select(FailureInjection).where(
                        FailureInjection.active.is_(True),
                        FailureInjection.expires_at > now_utc(),
                    )
                )
            )
            .scalars()
            .all()
        )
        rules = [
            {
                "id": r.id,
                "service": r.service,
                "action": r.action,
                "path_contains": r.path_contains,
                "method": r.method,
                "probability": r.probability,
                "latency_ms": r.latency_ms,
                "message": r.message,
                "params": dict(r.params or {}),
            }
            for r in rows
        ]
    _scenario_cache["at"] = now
    _scenario_cache["rules"] = rules
    return list(rules)


async def _record_hit(rule_id: str) -> None:
    async with SessionLocal() as session:
        await session.execute(
            update(FailureInjection)
            .where(FailureInjection.id == rule_id)
            .values(hits=FailureInjection.hits + 1)
        )
        await session.commit()


def _matches(rule: dict[str, Any], service: str, request: Request) -> bool:
    if rule["service"] not in (service, "all"):
        return False
    if rule["method"] and rule["method"].upper() != request.method.upper():
        return False
    if rule["path_contains"] and rule["path_contains"] not in request.url.path:
        return False
    return random.random() <= float(rule["probability"])


async def apply_failure(service: str, request: Request) -> Response | None:
    """Return a synthetic failure response when a scenario matches this request."""
    for rule in await active_rules():
        if not _matches(rule, service, request):
            continue
        action = rule["action"]
        message = rule["message"]
        await _record_hit(rule["id"])
        if action == "latency":
            await asyncio.sleep(max(rule["latency_ms"], 0) / 1000.0)
            return None  # delay only, let the real handler run
        if action == "timeout":
            await asyncio.sleep(max(rule["latency_ms"] or 30000, 0) / 1000.0)
            return _fail_response(service, 504, message or "Gateway timeout (injected).")
        if action == "rate_limit":
            retry_after = int(rule["params"].get("retry_after", 5))
            return _fail_response(
                service,
                429,
                message or "Rate limit exceeded (injected).",
                headers={"Retry-After": str(retry_after)},
                retry_after=retry_after,
            )
        if action == "quota_exhausted":
            resource = rule["params"].get("resource", "cores")
            return _fail_response(
                service,
                403,
                message or f"Quota exceeded for {resource} (injected).",
            )
        if action == "503_error":
            return _fail_response(
                service, 503, message or "Service Unavailable (injected)."
            )
        # default: 500_error
        return _fail_response(
            service, 500, message or "Unexpected API Error (injected)."
        )
    return None


def _fail_response(
    service: str,
    status: int,
    message: str,
    headers: dict[str, str] | None = None,
    **extra: Any,
) -> JSONResponse:
    response = JSONResponse(
        error_body(service, status, message, **extra), status_code=status
    )
    response.headers["X-OpenStack-Sim-Injected"] = "true"
    for key, value in (headers or {}).items():
        response.headers[key] = value
    return response


# --------------------------------------------------------------------------------------
# Application factory
# --------------------------------------------------------------------------------------


def version_headers(service: str) -> dict[str, str]:
    """Echo the modern microversion for this service regardless of what was asked for."""
    entry = API_VERSIONS.get(service)
    if entry is None:
        return {}
    name, minimum, maximum = entry
    headers = {
        "OpenStack-API-Version": f"{name} {maximum}",
        f"X-OpenStack-{name.capitalize()}-API-Version": maximum,
        f"X-OpenStack-{name.capitalize()}-API-Minimum-Version": minimum,
        f"X-OpenStack-{name.capitalize()}-API-Maximum-Version": maximum,
        "Vary": "OpenStack-API-Version",
    }
    if service == "nova":
        headers["X-OpenStack-Nova-API-Version"] = maximum
        headers["X-OpenStack-Nova-API-Minimum-Version"] = minimum
        headers["X-OpenStack-Nova-API-Maximum-Version"] = maximum
    return headers


def create_service_app(service: str, title: str, description: str = "") -> FastAPI:
    """Build a FastAPI app pre-wired with scenario hooks, version headers and errors."""
    app = FastAPI(
        title=title,
        description=description,
        version="1.0.0",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.state.service = service
    static_headers = version_headers(service)

    @app.middleware("http")
    async def _decorate(request: Request, call_next: Callable[..., Any]) -> Response:
        request.state.service = service
        response = await call_next(request)
        for key, value in static_headers.items():
            response.headers[key] = value
        response.headers.setdefault(
            "x-openstack-request-id", f"req-{random.getrandbits(64):016x}"
        )
        return response

    @app.middleware("http")
    async def _scenarios(request: Request, call_next: Callable[..., Any]) -> Response:
        injected = await apply_failure(service, request)
        if injected is not None:
            return injected
        return await call_next(request)

    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["*"],
        allow_headers=["*"],
        expose_headers=["*"],
    )

    @app.exception_handler(StarletteHTTPException)
    async def _http_error(request: Request, exc: StarletteHTTPException) -> Response:
        detail = exc.detail
        body = (
            detail
            if isinstance(detail, dict)
            else error_body(service, exc.status_code, str(detail))
        )
        return JSONResponse(body, status_code=exc.status_code, headers=exc.headers)

    @app.exception_handler(RequestValidationError)
    async def _validation_error(
        request: Request, exc: RequestValidationError
    ) -> Response:
        first = exc.errors()[0] if exc.errors() else {}
        location = ".".join(str(p) for p in first.get("loc", ())[1:]) or "body"
        message = f"Invalid input for field '{location}': {first.get('msg', 'invalid')}"
        return JSONResponse(error_body(service, 400, message), status_code=400)

    return app


def require(service: str) -> Any:
    """Shorthand: ``auth: AuthContext = Depends(require("nova"))``."""
    return Depends(auth_dependency(service))
