"""Keystone Identity v3 (port 5000): tokens, projects, users, roles, service catalog."""
from __future__ import annotations

from datetime import timedelta
from typing import Any

from fastapi import APIRouter, Depends, Header, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import (
    DOMAIN_ID,
    DOMAIN_NAME,
    build_catalog,
    gen_id,
    gen_token,
    iso,
    now_utc,
    service_url,
    settings,
)
from app.core.database import get_session
from app.core.middleware import AuthContext, OSPayload, fault, require, resolve_token
from app.models.identity import Endpoint, Project, Role, RoleAssignment, Service, Token, User

SERVICE = "keystone"
router = APIRouter()
auth_dep = require(SERVICE)

DOMAIN = {"id": DOMAIN_ID, "name": DOMAIN_NAME}


# --------------------------------------------------------------------------------------
# Schemas
# --------------------------------------------------------------------------------------


class AuthIdentity(OSPayload):
    methods: list[str] = Field(default_factory=lambda: ["password"])
    password: dict[str, Any] | None = None
    token: dict[str, Any] | None = None


class AuthBlock(OSPayload):
    identity: AuthIdentity
    scope: Any | None = None


class TokenRequest(BaseModel):
    auth: AuthBlock


class ProjectPayload(OSPayload):
    name: str
    description: str = ""
    domain_id: str = DOMAIN_ID
    enabled: bool = True
    tags: list[str] = Field(default_factory=list)


class UserPayload(OSPayload):
    name: str
    password: str = ""
    email: str | None = None
    domain_id: str = DOMAIN_ID
    default_project_id: str | None = None
    enabled: bool = True
    description: str = ""


class RolePayload(OSPayload):
    name: str
    description: str = ""


# --------------------------------------------------------------------------------------
# Serialisers
# --------------------------------------------------------------------------------------


def project_dict(project: Project) -> dict[str, Any]:
    return {
        "id": project.id,
        "name": project.name,
        "description": project.description,
        "domain_id": project.domain_id,
        "parent_id": project.parent_id or project.domain_id,
        "enabled": project.enabled,
        "is_domain": project.is_domain,
        "tags": list(project.tags or []),
        "options": {},
        "links": {"self": service_url(SERVICE, f"/v3/projects/{project.id}")},
    }


def user_dict(user: User) -> dict[str, Any]:
    return {
        "id": user.id,
        "name": user.name,
        "email": user.email,
        "domain_id": user.domain_id,
        "default_project_id": user.default_project_id,
        "enabled": user.enabled,
        "description": user.description,
        "password_expires_at": None,
        "options": {},
        "links": {"self": service_url(SERVICE, f"/v3/users/{user.id}")},
    }


def role_dict(role: Role) -> dict[str, Any]:
    return {
        "id": role.id,
        "name": role.name,
        "domain_id": role.domain_id,
        "description": role.description,
        "options": {},
        "links": {"self": service_url(SERVICE, f"/v3/roles/{role.id}")},
    }


def _version_doc(status: str = "stable") -> dict[str, Any]:
    return {
        "id": "v3.14",
        "status": status,
        "updated": "2020-04-07T00:00:00Z",
        "links": [{"rel": "self", "href": service_url(SERVICE, "/v3/")}],
        "media-types": [
            {"base": "application/json", "type": "application/vnd.openstack.identity-v3+json"}
        ],
    }


# --------------------------------------------------------------------------------------
# Version discovery
# --------------------------------------------------------------------------------------


@router.get("/", include_in_schema=False)
async def versions() -> Response:
    return JSONResponse({"versions": {"values": [_version_doc()]}}, status_code=300)


@router.get("/v3", include_in_schema=False)
@router.get("/v3/", include_in_schema=False)
async def version_v3() -> dict[str, Any]:
    return {"version": _version_doc()}


# --------------------------------------------------------------------------------------
# Token issuance
# --------------------------------------------------------------------------------------


async def _lookup_user(session: AsyncSession, spec: dict[str, Any]) -> User | None:
    if spec.get("id"):
        return await session.get(User, spec["id"])
    if spec.get("name"):
        return (
            await session.execute(select(User).where(User.name == spec["name"]))
        ).scalar_one_or_none()
    return None


async def _lookup_project(session: AsyncSession, spec: dict[str, Any]) -> Project | None:
    if spec.get("id"):
        return await session.get(Project, spec["id"])
    if spec.get("name"):
        return (
            await session.execute(select(Project).where(Project.name == spec["name"]))
        ).scalar_one_or_none()
    return None


async def _roles_for(session: AsyncSession, user_id: str, project_id: str) -> list[Role]:
    stmt = (
        select(Role)
        .join(RoleAssignment, RoleAssignment.role_id == Role.id)
        .where(RoleAssignment.user_id == user_id, RoleAssignment.project_id == project_id)
    )
    roles = list((await session.execute(stmt)).scalars().all())
    if roles:
        return roles
    # Unassigned users still get a reader role so the simulator stays usable.
    fallback = (
        await session.execute(select(Role).where(Role.name == "reader"))
    ).scalar_one_or_none()
    return [fallback] if fallback else []


def _token_body(
    user: User, project: Project, roles: list[Role], token: Token, methods: list[str],
    with_catalog: bool = True,
) -> dict[str, Any]:
    body: dict[str, Any] = {
        "methods": methods,
        "user": {
            "id": user.id,
            "name": user.name,
            "domain": DOMAIN,
            "password_expires_at": None,
        },
        "audit_ids": [token.id[:16]],
        "expires_at": iso(token.expires_at),
        "issued_at": iso(token.issued_at),
        "project": {"id": project.id, "name": project.name, "domain": DOMAIN},
        "is_domain": False,
        "roles": [{"id": role.id, "name": role.name} for role in roles],
    }
    if with_catalog:
        body["catalog"] = build_catalog(project.id)
    return body


@router.post("/v3/auth/tokens")
async def issue_token(
    payload: TokenRequest,
    session: AsyncSession = Depends(get_session),
) -> Response:
    """Password or token re-scope. Returns the opaque UUID in X-Subject-Token."""
    identity = payload.auth.identity
    methods = identity.methods or ["password"]
    user: User | None = None

    if "password" in methods and identity.password:
        spec = identity.password.get("user") or {}
        user = await _lookup_user(session, spec)
        if user is None or not user.enabled:
            raise fault(SERVICE, 401, "The request you have made requires authentication.")
        supplied = spec.get("password", "")
        if user.password and supplied != user.password:
            raise fault(SERVICE, 401, "The request you have made requires authentication.")
    elif "token" in methods and identity.token:
        existing = await resolve_token(str(identity.token.get("id", "")))
        if existing is None:
            raise fault(SERVICE, 401, "The request you have made requires authentication.")
        user = await session.get(User, existing.user_id)
    else:
        raise fault(SERVICE, 400, "Unsupported authentication method.")

    if user is None:
        raise fault(SERVICE, 401, "The request you have made requires authentication.")

    # Resolve the requested scope, defaulting to the user's default project.
    project: Project | None = None
    scope = payload.auth.scope
    if isinstance(scope, dict) and scope.get("project"):
        project = await _lookup_project(session, scope["project"])
        if project is None:
            raise fault(SERVICE, 401, "Could not find project in the requested scope.")
    if project is None and user.default_project_id:
        project = await session.get(Project, user.default_project_id)
    if project is None:
        project = (
            await session.execute(
                select(Project).where(Project.name == settings.admin_project)
            )
        ).scalar_one_or_none()
    if project is None:
        raise fault(SERVICE, 401, "No project available to scope this token to.")

    roles = await _roles_for(session, user.id, project.id)
    token = Token(
        id=gen_token(),
        user_id=user.id,
        project_id=project.id,
        roles=[role.name for role in roles],
        issued_at=now_utc(),
        expires_at=now_utc() + timedelta(hours=settings.token_expiry_hours),
    )
    session.add(token)
    await session.commit()

    return JSONResponse(
        {"token": _token_body(user, project, roles, token, methods)},
        status_code=201,
        headers={"X-Subject-Token": token.id},
    )


async def _validate_subject(
    session: AsyncSession, subject_token: str | None
) -> tuple[User, Project, list[Role], Token]:
    if not subject_token:
        raise fault(SERVICE, 404, "Could not find token: <missing>")
    token = await session.get(Token, subject_token)
    if token is None or token.revoked or token.expires_at <= now_utc():
        raise fault(SERVICE, 404, f"Could not find token: {subject_token}")
    user = await session.get(User, token.user_id)
    project = await session.get(Project, token.project_id) if token.project_id else None
    if user is None or project is None:
        raise fault(SERVICE, 404, f"Could not find token: {subject_token}")
    roles = await _roles_for(session, user.id, project.id)
    return user, project, roles, token


@router.get("/v3/auth/tokens")
async def validate_token(
    request: Request,
    x_subject_token: str | None = Header(default=None),
    session: AsyncSession = Depends(get_session),
) -> Response:
    user, project, roles, token = await _validate_subject(session, x_subject_token)
    with_catalog = "nocatalog" not in request.query_params
    return JSONResponse(
        {"token": _token_body(user, project, roles, token, ["password"], with_catalog)},
        headers={"X-Subject-Token": token.id},
    )


@router.head("/v3/auth/tokens")
async def check_token(
    x_subject_token: str | None = Header(default=None),
    session: AsyncSession = Depends(get_session),
) -> Response:
    await _validate_subject(session, x_subject_token)
    return Response(status_code=200)


@router.delete("/v3/auth/tokens", status_code=204)
async def revoke_token(
    x_subject_token: str | None = Header(default=None),
    session: AsyncSession = Depends(get_session),
) -> Response:
    token = await session.get(Token, x_subject_token or "")
    if token is not None:
        token.revoked = True
        await session.commit()
    return Response(status_code=204)


@router.get("/v3/auth/catalog")
async def auth_catalog(auth: AuthContext = auth_dep) -> dict[str, Any]:
    return {"catalog": build_catalog(auth.project_id)}


@router.get("/v3/auth/projects")
async def auth_projects(
    auth: AuthContext = auth_dep, session: AsyncSession = Depends(get_session)
) -> dict[str, Any]:
    projects = (await session.execute(select(Project))).scalars().all()
    return {
        "projects": [project_dict(p) for p in projects],
        "links": {"self": service_url(SERVICE, "/v3/auth/projects"), "next": None, "previous": None},
    }


@router.get("/v3/auth/domains")
async def auth_domains(auth: AuthContext = auth_dep) -> dict[str, Any]:
    return {"domains": [_domain_dict()], "links": {"self": service_url(SERVICE, "/v3/auth/domains")}}


# --------------------------------------------------------------------------------------
# Projects
# --------------------------------------------------------------------------------------


@router.get("/v3/projects")
async def list_projects(
    name: str | None = None,
    auth: AuthContext = auth_dep,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    stmt = select(Project)
    if name:
        stmt = stmt.where(Project.name == name)
    projects = (await session.execute(stmt)).scalars().all()
    return {
        "projects": [project_dict(p) for p in projects],
        "links": {"self": service_url(SERVICE, "/v3/projects"), "next": None, "previous": None},
    }


@router.post("/v3/projects", status_code=201)
async def create_project(
    body: dict[str, Any],
    auth: AuthContext = auth_dep,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    payload = ProjectPayload(**(body.get("project") or {}))
    existing = (
        await session.execute(select(Project).where(Project.name == payload.name))
    ).scalar_one_or_none()
    if existing is not None:
        raise fault(SERVICE, 409, f"Conflict occurred attempting to store project: {payload.name}")
    project = Project(
        id=gen_id(),
        name=payload.name,
        description=payload.description,
        domain_id=payload.domain_id,
        enabled=payload.enabled,
        tags=payload.tags,
    )
    session.add(project)
    await session.commit()
    return {"project": project_dict(project)}


@router.get("/v3/projects/{project_id}")
async def get_project(
    project_id: str,
    auth: AuthContext = auth_dep,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    project = await session.get(Project, project_id)
    if project is None:
        raise fault(SERVICE, 404, f"Could not find project: {project_id}")
    return {"project": project_dict(project)}


@router.patch("/v3/projects/{project_id}")
async def update_project(
    project_id: str,
    body: dict[str, Any],
    auth: AuthContext = auth_dep,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    project = await session.get(Project, project_id)
    if project is None:
        raise fault(SERVICE, 404, f"Could not find project: {project_id}")
    for key, value in (body.get("project") or {}).items():
        if key in ("name", "description", "enabled", "tags"):
            setattr(project, key, value)
    await session.commit()
    return {"project": project_dict(project)}


@router.delete("/v3/projects/{project_id}", status_code=204)
async def delete_project(
    project_id: str,
    auth: AuthContext = auth_dep,
    session: AsyncSession = Depends(get_session),
) -> Response:
    project = await session.get(Project, project_id)
    if project is None:
        raise fault(SERVICE, 404, f"Could not find project: {project_id}")
    await session.delete(project)
    await session.commit()
    return Response(status_code=204)


# --------------------------------------------------------------------------------------
# Users
# --------------------------------------------------------------------------------------


@router.get("/v3/users")
async def list_users(
    name: str | None = None,
    auth: AuthContext = auth_dep,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    stmt = select(User)
    if name:
        stmt = stmt.where(User.name == name)
    users = (await session.execute(stmt)).scalars().all()
    return {
        "users": [user_dict(u) for u in users],
        "links": {"self": service_url(SERVICE, "/v3/users"), "next": None, "previous": None},
    }


@router.post("/v3/users", status_code=201)
async def create_user(
    body: dict[str, Any],
    auth: AuthContext = auth_dep,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    payload = UserPayload(**(body.get("user") or {}))
    user = User(
        id=gen_id(),
        name=payload.name,
        password=payload.password,
        email=payload.email,
        domain_id=payload.domain_id,
        default_project_id=payload.default_project_id,
        enabled=payload.enabled,
        description=payload.description,
    )
    session.add(user)
    await session.commit()
    return {"user": user_dict(user)}


@router.get("/v3/users/{user_id}")
async def get_user(
    user_id: str,
    auth: AuthContext = auth_dep,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    user = await session.get(User, user_id)
    if user is None:
        raise fault(SERVICE, 404, f"Could not find user: {user_id}")
    return {"user": user_dict(user)}


@router.patch("/v3/users/{user_id}")
async def update_user(
    user_id: str,
    body: dict[str, Any],
    auth: AuthContext = auth_dep,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    user = await session.get(User, user_id)
    if user is None:
        raise fault(SERVICE, 404, f"Could not find user: {user_id}")
    for key, value in (body.get("user") or {}).items():
        if key in ("name", "password", "email", "enabled", "description", "default_project_id"):
            setattr(user, key, value)
    await session.commit()
    return {"user": user_dict(user)}


@router.delete("/v3/users/{user_id}", status_code=204)
async def delete_user(
    user_id: str,
    auth: AuthContext = auth_dep,
    session: AsyncSession = Depends(get_session),
) -> Response:
    user = await session.get(User, user_id)
    if user is None:
        raise fault(SERVICE, 404, f"Could not find user: {user_id}")
    await session.delete(user)
    await session.commit()
    return Response(status_code=204)


@router.get("/v3/users/{user_id}/projects")
async def user_projects(
    user_id: str,
    auth: AuthContext = auth_dep,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    stmt = (
        select(Project)
        .join(RoleAssignment, RoleAssignment.project_id == Project.id)
        .where(RoleAssignment.user_id == user_id)
        .distinct()
    )
    projects = (await session.execute(stmt)).scalars().all()
    return {"projects": [project_dict(p) for p in projects], "links": {"self": ""}}


# --------------------------------------------------------------------------------------
# Roles, assignments, domains, catalog introspection
# --------------------------------------------------------------------------------------


@router.get("/v3/roles")
async def list_roles(
    name: str | None = None,
    auth: AuthContext = auth_dep,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    stmt = select(Role)
    if name:
        stmt = stmt.where(Role.name == name)
    roles = (await session.execute(stmt)).scalars().all()
    return {"roles": [role_dict(r) for r in roles], "links": {"self": service_url(SERVICE, "/v3/roles")}}


@router.post("/v3/roles", status_code=201)
async def create_role(
    body: dict[str, Any],
    auth: AuthContext = auth_dep,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    payload = RolePayload(**(body.get("role") or {}))
    role = Role(id=gen_id(), name=payload.name, description=payload.description)
    session.add(role)
    await session.commit()
    return {"role": role_dict(role)}


@router.get("/v3/role_assignments")
async def list_assignments(
    auth: AuthContext = auth_dep, session: AsyncSession = Depends(get_session)
) -> dict[str, Any]:
    rows = (await session.execute(select(RoleAssignment))).scalars().all()
    return {
        "role_assignments": [
            {
                "role": {"id": row.role_id},
                "scope": {"project": {"id": row.project_id}},
                "user": {"id": row.user_id},
                "links": {"assignment": ""},
            }
            for row in rows
        ],
        "links": {"self": service_url(SERVICE, "/v3/role_assignments")},
    }


@router.put("/v3/projects/{project_id}/users/{user_id}/roles/{role_id}", status_code=204)
async def grant_role(
    project_id: str,
    user_id: str,
    role_id: str,
    auth: AuthContext = auth_dep,
    session: AsyncSession = Depends(get_session),
) -> Response:
    existing = (
        await session.execute(
            select(RoleAssignment).where(
                RoleAssignment.project_id == project_id,
                RoleAssignment.user_id == user_id,
                RoleAssignment.role_id == role_id,
            )
        )
    ).scalar_one_or_none()
    if existing is None:
        session.add(
            RoleAssignment(project_id=project_id, user_id=user_id, role_id=role_id)
        )
        await session.commit()
    return Response(status_code=204)


def _domain_dict() -> dict[str, Any]:
    return {
        "id": DOMAIN_ID,
        "name": DOMAIN_NAME,
        "description": "The default domain",
        "enabled": True,
        "tags": [],
        "options": {},
        "links": {"self": service_url(SERVICE, f"/v3/domains/{DOMAIN_ID}")},
    }


@router.get("/v3/domains")
async def list_domains(auth: AuthContext = auth_dep) -> dict[str, Any]:
    return {"domains": [_domain_dict()], "links": {"self": service_url(SERVICE, "/v3/domains")}}


@router.get("/v3/domains/{domain_id}")
async def get_domain(domain_id: str, auth: AuthContext = auth_dep) -> dict[str, Any]:
    if domain_id not in (DOMAIN_ID, DOMAIN_NAME):
        raise fault(SERVICE, 404, f"Could not find domain: {domain_id}")
    return {"domain": _domain_dict()}


@router.get("/v3/regions")
async def list_regions(auth: AuthContext = auth_dep) -> dict[str, Any]:
    return {
        "regions": [
            {
                "id": "RegionOne",
                "description": "Simulated region",
                "parent_region_id": None,
                "links": {"self": service_url(SERVICE, "/v3/regions/RegionOne")},
            }
        ],
        "links": {"self": service_url(SERVICE, "/v3/regions")},
    }


@router.get("/v3/services")
async def list_services(
    type: str | None = None,
    auth: AuthContext = auth_dep,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    stmt = select(Service)
    if type:
        stmt = stmt.where(Service.type == type)
    services = (await session.execute(stmt)).scalars().all()
    return {
        "services": [
            {
                "id": s.id,
                "type": s.type,
                "name": s.name,
                "description": s.description,
                "enabled": s.enabled,
                "links": {"self": service_url(SERVICE, f"/v3/services/{s.id}")},
            }
            for s in services
        ],
        "links": {"self": service_url(SERVICE, "/v3/services")},
    }


@router.get("/v3/endpoints")
async def list_endpoints(
    auth: AuthContext = auth_dep, session: AsyncSession = Depends(get_session)
) -> dict[str, Any]:
    endpoints = (await session.execute(select(Endpoint))).scalars().all()
    return {
        "endpoints": [
            {
                "id": e.id,
                "service_id": e.service_id,
                "interface": e.interface,
                "region": e.region_id,
                "region_id": e.region_id,
                "url": e.url % {"project_id": auth.project_id}
                if "%(project_id)s" in e.url
                else e.url,
                "enabled": e.enabled,
                "links": {"self": service_url(SERVICE, f"/v3/endpoints/{e.id}")},
            }
            for e in endpoints
        ],
        "links": {"self": service_url(SERVICE, "/v3/endpoints")},
    }
