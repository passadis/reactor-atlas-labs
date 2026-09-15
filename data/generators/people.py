"""People and entitlements.

`entitlements.json` is the AUTHORIZATION SOURCE OF TRUTH for the deterministic policy gate
(DATA-MODEL.md section 6). The model never decides whether an action is permitted.

The `readonly` persona exists so Episode 4's `unauthorized` eval category has a principal that MUST
be refused. It never gets `may_execute`, its `max_blast_radius` is zero, and it must never be
granted the `Atlas.Approver` app role. If any of those change, that eval category silently stops
testing anything.
"""

from __future__ import annotations

import random

from atlas_common.models import Entitlement, Person

from .common import EMAIL_DOMAIN

# (user_id, display name, team, role). Fixed rather than generated, so approver identities are
# stable across rehearsals and the runbooks can name a specific approver.
PEOPLE: list[tuple[str, str, str, str]] = [
    ("u-anovak", "Ana Novak", "team-commerce", "sre"),
    ("u-boyelaran", "Bode Oyelaran", "team-payments", "sre"),
    ("u-cdubois", "Camille Dubois", "team-platform", "engineer"),
    ("u-dsvensson", "Dag Svensson", "team-payments", "engineer"),
    ("u-emoreau", "Elise Moreau", "team-commerce", "engineer"),
    ("u-fhaddad", "Farid Haddad", "team-risk", "sre"),
    ("u-gtanaka", "Goro Tanaka", "team-finance", "engineer"),
    ("u-hoconnor", "Hanna Oconnor", "team-commerce", "manager"),
    ("u-ibergmann", "Ines Bergmann", "team-platform", "manager"),
    ("u-jrossi", "Jacopo Rossi", "team-supply", "engineer"),
    ("u-kpetrova", "Kira Petrova", "team-messaging", "engineer"),
    ("u-lmarchetti", "Luca Marchetti", "team-discovery", "engineer"),
    ("u-mabiodun", "Mara Abiodun", "team-data", "director"),
    ("u-nkowalski", "Nikolaj Kowalski", "team-internal", "engineer"),
    ("u-olindqvist", "Olle Lindqvist", "team-growth", "engineer"),
    # The persona Episode 4 depends on. Deliberately powerless.
    ("u-readonly", "Robin Reed", "team-internal", "readonly"),
]

# role, may_approve_risk, may_approve_tiers, may_execute, max_blast_radius
#
# Read by the policy gate in plain code. The tier and risk ceilings are what make
# "an SRE cannot approve a high-risk change to a Tier 1 service" a rule rather than a suggestion.
ENTITLEMENTS: list[tuple[str, list[str], list[int], bool, int]] = [
    ("engineer", ["low"], [3, 4], True, 3),
    ("sre", ["low", "medium"], [2, 3, 4], True, 8),
    ("manager", ["low", "medium", "high"], [1, 2, 3, 4], False, 15),
    ("director", ["low", "medium", "high"], [1, 2, 3, 4], False, 25),
    ("readonly", [], [], False, 0),
]


def generate_people(_rng: random.Random) -> list[Person]:
    """The Contoso operator roster. Fictional names, always a .example domain."""
    return [
        Person(
            user_id=user_id,
            display_name=display_name,
            team=team,
            role=role,  # type: ignore[arg-type]
            email=f"{user_id.removeprefix('u-')}@{EMAIL_DOMAIN}",
        )
        for user_id, display_name, team, role in PEOPLE
    ]


def generate_entitlements(_rng: random.Random) -> list[Entitlement]:
    """Role capabilities. The policy gate's rulebook."""
    return [
        Entitlement(
            role=role,  # type: ignore[arg-type]
            may_approve_risk=risks,  # type: ignore[arg-type]
            may_approve_tiers=tiers,  # type: ignore[arg-type]
            may_execute=may_execute,
            max_blast_radius=max_blast,
        )
        for role, risks, tiers, may_execute, max_blast in ENTITLEMENTS
    ]
