"""Map a signed-in Entra user to the Contoso persona whose entitlements apply.

Resolution, in order:

1. A seeded person with the same email (people in `people` are all `@contoso.example`, so this
   only matches in a tenant that uses those addresses).
2. Otherwise the canonical persona for the user's highest ATLAS app role
   (`entitlements.default_persona_role`): approvers act as a manager, engineers as an SRE, everyone
   else as readonly.
3. Then the session's "act as" request is applied, which can only lower privilege.

The effective permission is always the intersection of the app role and the persona entitlement
(`entitlements.can_approve`). Neither can widen the other.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

from atlas_common.entitlements import default_persona_role, narrow, pick_persona
from atlas_common.models import Entitlement, Person
from atlas_common.repositories import EntitlementRepository

from .auth import Principal

_CACHE_TTL_S = 300.0


@dataclass(frozen=True)
class Actor:
    principal: Principal
    person: Person | None
    persona_role: str
    entitlement: Entitlement | None
    narrowed: bool
    binding: str  # "email" | "app-role"

    def public(self) -> dict[str, object]:
        return {
            "name": self.principal.name,
            "email": self.principal.email,
            "app_roles": list(self.principal.app_roles),
            "auth": self.principal.source,
            "persona": {
                "user_id": self.person.user_id if self.person else None,
                "display_name": self.person.display_name if self.person else None,
                "team": self.person.team if self.person else None,
                "role": self.persona_role,
                "binding": self.binding,
                "narrowed": self.narrowed,
            },
            "entitlement": self.entitlement.model_dump() if self.entitlement else None,
        }


class PersonaResolver:
    """Cached lookups: people and entitlements change only at seed time."""

    def __init__(self, repo: EntitlementRepository) -> None:
        self._repo = repo
        self._people: tuple[float, list[Person]] | None = None
        self._entitlements: dict[str, tuple[float, Entitlement | None]] = {}

    async def _all_people(self) -> list[Person]:
        now = time.monotonic()
        if self._people is None or now - self._people[0] > _CACHE_TTL_S:
            self._people = (now, await self._repo.people())
        return self._people[1]

    async def _entitlement(self, role: str) -> Entitlement | None:
        now = time.monotonic()
        cached = self._entitlements.get(role)
        if cached is None or now - cached[0] > _CACHE_TTL_S:
            cached = (now, await self._repo.for_role(role))
            self._entitlements[role] = cached
        return cached[1]

    async def resolve(self, principal: Principal, act_as: str | None = None) -> Actor:
        people = await self._all_people()
        by_email = next((p for p in people if p.email.lower() == principal.email), None)
        base_role = by_email.role if by_email else default_persona_role(principal.app_roles)
        role = narrow(base_role, act_as)
        person = by_email if by_email and role == by_email.role else pick_persona(people, role)
        return Actor(
            principal=principal,
            person=person,
            persona_role=role,
            entitlement=await self._entitlement(role),
            narrowed=role != base_role,
            binding="email" if by_email else "app-role",
        )
