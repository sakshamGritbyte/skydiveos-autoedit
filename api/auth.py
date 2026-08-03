"""Caller identity + access scoping for the REST layer.

Two independent controls live here:

1. **A service-token gate** (:func:`require_service_token`) on every route except
   the customer gallery — *who may talk to this API at all*.
2. **Per-instructor scoping** (:class:`Principal`) — *which jobs a caller may see*.

The gate exists because the identity below is **self-asserted**: a caller states
its own instructor id and role in headers, which is only safe if the caller is
trusted to begin with. That assumption broke in production (verified 2026-08-03):
the service is internet-facing at ``https://ai.ultimatedzm.com`` — the SkydiveOS
frontend was built to call it straight from the browser — and with
``ENFORCE_INSTRUCTOR_AUTH`` off every anonymous caller resolves to an admin. An
anonymous ``GET /jobs`` returned every customer's name, email and delivery links,
and ``/jobs/{id}/deliverables/{name}`` streamed their finished video. So a shared
secret now gates the whole surface, independent of the network.

SkydiveOS is the front door and already authenticates users; this API trusts the
identity it forwards on each request rather than running its own login. Identity
arrives in two headers:

* ``X-Instructor-Id`` — the calling instructor's SkydiveOS account id.
* ``X-Role`` — ``"instructor"`` (default) or ``"admin"``.

From those we build a :class:`Principal` and use it to scope access: an instructor
sees and acts on only their own jobs and cameras; an admin sees everything and is
the only role allowed to manage the camera registry.

Enforcement is gated by ``ENFORCE_INSTRUCTOR_AUTH`` (off by default). When off, the
endpoints behave exactly as before — every caller is treated as an admin — so the
existing flow and tests are unaffected; turn it on in production once SkydiveOS is
forwarding the headers. *Tagging* (stamping a job's owning instructor) always
happens regardless of this flag; only the access checks are gated.
"""

from __future__ import annotations

import hmac
import logging
from typing import Annotated

from fastapi import Depends, Header, HTTPException, Request

from .config import Settings, get_settings

logger = logging.getLogger(__name__)

#: Role values recognised in the ``X-Role`` header.
ROLE_ADMIN = "admin"
ROLE_INSTRUCTOR = "instructor"

#: Path prefix of the CUSTOMER-facing gallery (``/j/{code}``). These requests come
#: from a member of the public who has no SkydiveOS account and forwards no identity
#: headers, so identity enforcement can't apply — the unguessable short code in the
#: URL is the credential (see :func:`api.app.create_app`'s gallery routes). Such a
#: request resolves to an *anonymous* principal that owns nothing, so if one ever
#: reached a job-scoped route it would 404 rather than see someone else's jump.
PUBLIC_PATH_PREFIX = "/j/"


def service_token_allows(
    path: str, method: str, authorization: str | None, settings: Settings
) -> bool:
    """Whether this request may enter at all — the service-token gate.

    Pure (strings in, bool out) and enforced by a middleware rather than a route
    dependency, because FastAPI registers ``/docs``, ``/redoc`` and ``/openapi.json``
    as raw Starlette routes that **skip** app-level dependencies — and those hand an
    attacker the whole API surface. A middleware also covers any route added later
    without per-endpoint wiring.

    Two exemptions:

    * :data:`PUBLIC_PATH_PREFIX` (``/j/{code}``) — the customer gallery. Its
      unguessable short code is its own credential, and a customer has no SkydiveOS
      account or token; gating it would break the product.
    * ``OPTIONS`` — a CORS preflight carries no credentials by design. The real
      request behind it is still gated.

    **Off until ``AUTO_EDIT_API_KEY`` is set** (like ``ENFORCE_INSTRUCTOR_AUTH``), so
    enabling it is one config change on each side and rolling back is the same. No
    client change is needed: SkydiveOS already sends this value as
    ``Authorization: Bearer`` on every proxied call (its ``AI_BACKEND_API_KEY`` /
    ``AUTO_EDIT_API_KEY``) — set the same secret in both env files.

    Compared with :func:`hmac.compare_digest` so a wrong token can't be recovered by
    timing; the presented value is never logged.
    """
    expected = settings.service_token
    if not expected:
        return True
    if path.startswith(PUBLIC_PATH_PREFIX) or method.upper() == "OPTIONS":
        return True
    scheme, _, presented = (authorization or "").partition(" ")
    if scheme.lower() != "bearer":
        return False
    return hmac.compare_digest(presented.strip(), expected)


def service_auth_headers(settings: Settings | None = None) -> dict[str, str]:
    """The header a *client* of this API must send — ``{}`` when the gate is off.

    Used by the repo's own operator scripts (demo/QA drivers) so they keep working
    once ``AUTO_EDIT_API_KEY`` is set, without each one re-deriving the contract.
    """
    settings = settings or get_settings()
    if not settings.service_token:
        return {}
    return {"Authorization": f"Bearer {settings.service_token}"}


def require_service_token(
    request: Request,
    settings: Annotated[Settings, Depends(get_settings)],
    authorization: Annotated[str | None, Header()] = None,
) -> None:
    """Dependency form of :func:`service_token_allows` (401s a caller without it).

    The middleware in :func:`api.app.create_app` is what actually protects the
    service; this exists so an individual route can demand the token explicitly, and
    so the requirement shows up in the OpenAPI schema.
    """
    if not service_token_allows(
        request.url.path, request.method, authorization, settings
    ):
        # Log the path, never the token — and don't tell the caller whether it was
        # missing or wrong.
        logger.warning("rejected an unauthenticated request to %s", request.url.path)
        raise HTTPException(
            status_code=401,
            detail="a valid service token is required",
            headers={"WWW-Authenticate": "Bearer"},
        )


class Principal:
    """The authenticated caller: an instructor id and a role (admin or instructor)."""

    def __init__(self, instructor_id: str | None, role: str) -> None:
        self.instructor_id = instructor_id
        self.role = role

    @property
    def is_admin(self) -> bool:
        return self.role == ROLE_ADMIN

    def owns(self, instructor_id: str | None) -> bool:
        """Whether this caller may access a resource owned by ``instructor_id``.

        Admins may access anything; an instructor may access only resources stamped
        with their own id (and never an unowned one).
        """
        if self.is_admin:
            return True
        return instructor_id is not None and instructor_id == self.instructor_id


def get_principal(
    request: Request,
    settings: Annotated[Settings, Depends(get_settings)],
    x_instructor_id: Annotated[str | None, Header()] = None,
    x_role: Annotated[str | None, Header()] = None,
) -> Principal:
    """Build the caller's :class:`Principal` from the forwarded SkydiveOS headers.

    With enforcement off, every caller is an admin (back-compatible: no scoping).
    With enforcement on, the role comes from ``X-Role`` (defaulting to ``instructor``)
    and a non-admin caller must present an ``X-Instructor-Id`` — except on the public
    customer gallery (:data:`PUBLIC_PATH_PREFIX`), where there is no SkydiveOS user to
    identify and the request resolves to an anonymous, owns-nothing principal.
    """
    if not settings.enforce_instructor_auth:
        return Principal(instructor_id=x_instructor_id, role=ROLE_ADMIN)
    if request.url.path.startswith(PUBLIC_PATH_PREFIX):
        return Principal(instructor_id=None, role=ROLE_INSTRUCTOR)

    role = (x_role or ROLE_INSTRUCTOR).strip().lower()
    if role not in (ROLE_ADMIN, ROLE_INSTRUCTOR):
        raise HTTPException(status_code=403, detail=f"unknown role: {role!r}")
    if role != ROLE_ADMIN and not x_instructor_id:
        raise HTTPException(
            status_code=401, detail="missing X-Instructor-Id for a non-admin caller"
        )
    return Principal(instructor_id=x_instructor_id, role=role)


PrincipalDep = Annotated[Principal, Depends(get_principal)]


def require_admin(principal: PrincipalDep) -> Principal:
    """Dependency that 403s a non-admin caller (camera-registry management)."""
    if not principal.is_admin:
        raise HTTPException(status_code=403, detail="admin role required")
    return principal


AdminDep = Annotated[Principal, Depends(require_admin)]
