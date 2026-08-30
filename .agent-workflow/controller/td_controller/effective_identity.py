"""Explicit real/effective process identity gate for trusted effects."""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Callable

IdentityGetter = Callable[[], int]


class EffectiveIdentityError(RuntimeError):
    """Raised when trusted effects would run under an ambiguous identity."""


@dataclass(frozen=True, slots=True)
class EffectiveIdentity:
    """Unprivileged identity shared by real and effective credentials."""

    uid: int
    gid: int


def validate_effective_identity(
    *,
    getuid: IdentityGetter = os.getuid,
    geteuid: IdentityGetter = os.geteuid,
    getgid: IdentityGetter = os.getgid,
    getegid: IdentityGetter = os.getegid,
) -> EffectiveIdentity:
    """Reject privilege transitions and return the effective filesystem owner."""
    getters = (getuid, geteuid, getgid, getegid)
    if any(not callable(getter) for getter in getters):
        raise EffectiveIdentityError("identity getter is invalid")
    getter_failed = False
    try:
        values = tuple(getter() for getter in getters)
    except Exception:
        getter_failed = True
        values = ()
    if getter_failed:
        raise EffectiveIdentityError("process identity is unavailable") from None
    uid, effective_uid, gid, effective_gid = values
    if any(
        isinstance(value, bool) or not isinstance(value, int) or value < 0
        for value in values
    ):
        raise EffectiveIdentityError("process identity is invalid")
    if uid != effective_uid or gid != effective_gid:
        raise EffectiveIdentityError("process identity transition is prohibited")
    return EffectiveIdentity(effective_uid, effective_gid)
