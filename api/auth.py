"""Caller identity + access scoping for the REST layer.

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

from typing import Annotated

from fastapi import Depends, Header, HTTPException

from .config import Settings, get_settings

#: Role values recognised in the ``X-Role`` header.
ROLE_ADMIN = "admin"
ROLE_INSTRUCTOR = "instructor"


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
    settings: Annotated[Settings, Depends(get_settings)],
    x_instructor_id: Annotated[str | None, Header()] = None,
    x_role: Annotated[str | None, Header()] = None,
) -> Principal:
    """Build the caller's :class:`Principal` from the forwarded SkydiveOS headers.

    With enforcement off, every caller is an admin (back-compatible: no scoping).
    With enforcement on, the role comes from ``X-Role`` (defaulting to ``instructor``)
    and a non-admin caller must present an ``X-Instructor-Id``.
    """
    if not settings.enforce_instructor_auth:
        return Principal(instructor_id=x_instructor_id, role=ROLE_ADMIN)

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
