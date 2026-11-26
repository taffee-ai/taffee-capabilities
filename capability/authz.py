"""S-008: the authentication and visibility interface the catalog service is built against.

Two questions, nothing else: who is asking (`principal`), and may that principal see one catalog entry
(`may_see`). The real implementation is govsec's decision interface (GOVSEC-DESIGN, AuthZEN-shaped); until it
exists, the only implementations here are `DenyAllAuthorizer` and the explicitly selected development
`DevAuthorizer`. Nothing in this module authenticates anyone for real.
"""

import re
from dataclasses import dataclass

PRINCIPAL_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:@/-]{0,127}")
DEV_HEADER = "x-dev-principal"


@dataclass(frozen=True)
class Principal:
    id: str
    type: str = "user"  # user | agent | service, as in GOVSEC-DESIGN 3.1


class Authorizer:
    """The interface. An implementation that cannot answer must raise; the service then refuses (fail closed)."""

    def principal(self, headers: dict) -> Principal | None:
        """The verified caller, or None. `headers` has lower-case names."""
        raise NotImplementedError

    def may_see(self, principal: Principal, ref: dict) -> bool:
        """May `principal` see the entry `ref` (name, kind, version)?"""
        raise NotImplementedError


class DenyAllAuthorizer(Authorizer):
    """No caller is ever identified. The safe stand-in."""

    def principal(self, headers):
        return None

    def may_see(self, principal, ref):
        return False


class DevAuthorizer(Authorizer):
    """DEVELOPMENT ONLY. Trusts the `X-Dev-Principal` header as the caller's identity and lets every identified
    caller see every entry (the owner's interim "all agents have the highest privilege", 2026-10-20).

    It performs no authentication. It must be selected by name; the service never falls back to it. `hidden`
    (a set of "kind:name:version" strings) lets tests and demonstrations hide specific entries.
    """

    DEVELOPMENT_ONLY = True

    def __init__(self, hidden=()):
        self._hidden = frozenset(hidden)

    def principal(self, headers):
        value = headers.get(DEV_HEADER)
        if not isinstance(value, str) or PRINCIPAL_ID.fullmatch(value) is None:
            return None
        return Principal(value)

    def may_see(self, principal, ref):
        return f"{ref['kind']}:{ref['name']}:{ref['version']}" not in self._hidden


class AdmissionAuthorizer(Authorizer):
    """S-018: the two admission permissions, on top of identity and visibility. Govsec's real implementation
    answers these later; an implementation that cannot answer must raise, and the request is refused."""

    def may_submit(self, principal: Principal) -> bool:
        raise NotImplementedError

    def may_approve(self, principal: Principal) -> bool:
        raise NotImplementedError


class DevAdmissionAuthorizer(DevAuthorizer, AdmissionAuthorizer):
    """DEVELOPMENT ONLY. Identity from the `X-Dev-Principal` header, as DevAuthorizer. Every identified caller
    may submit; only the principals named in `approvers` may approve. It authenticates no one."""

    def __init__(self, approvers=(), hidden=()):
        super().__init__(hidden=hidden)
        self._approvers = frozenset(approvers)

    def may_submit(self, principal):
        return principal is not None

    def may_approve(self, principal):
        return principal is not None and principal.id in self._approvers
