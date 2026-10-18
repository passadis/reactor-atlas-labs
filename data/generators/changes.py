"""Change (deployment) generation: 300 records, one of which is the thread the agent must pull.

The canonical change is the whole point of the Episode 1 demo. The engineer says "checkout is
timing out since 14:05, right after the payments deploy"; a competent agent finds a high-risk
change to `svc-payments` that landed six minutes earlier and is rollback-capable, and says so with
the change id. A model without tools invents a change id instead - which is exactly what
`scripts/demo_raw_model.py` is there to show.

Everything is anchored to `--reference-date`, never to the wall clock, so the relationship between
the change and the incident is identical in every rehearsal.
"""

from __future__ import annotations

import hashlib
import random
from datetime import datetime, timedelta

from atlas_common.models import Change

from .common import pick, weighted_pick

CANONICAL_CHANGE_ID = "CHG-2026-0873"
CANONICAL_SERVICE = "svc-payments"
# The incident the demo opens with. 14:05 on the reference date.
CANONICAL_INCIDENT_HOUR = 14
CANONICAL_INCIDENT_MINUTE = 5
# DATA-MODEL.md: the change lands exactly six minutes before the incident.
CANONICAL_LEAD_MINUTES = 6

AUTHORS = [
    "a.novak",
    "b.oyelaran",
    "c.dubois",
    "d.svensson",
    "e.moreau",
    "f.haddad",
    "g.tanaka",
    "h.oconnor",
    "i.bergmann",
    "j.rossi",
    "k.petrova",
    "l.marchetti",
    "m.abiodun",
    "n.kowalski",
    "o.lindqvist",
]

SUMMARIES = [
    "Bump connection pool defaults",
    "Add retry with jitter to the upstream client",
    "Enable the new pricing cohort behind a flag",
    "Upgrade the platform SDK to 4.2",
    "Tighten request validation on the public surface",
    "Reduce cache TTL for hot keys",
    "Split the write path into a background worker",
    "Add structured logging to the request pipeline",
    "Migrate the read replica to the new instance class",
    "Introduce a circuit breaker on the dependency call",
    "Increase batch size for the nightly reconciliation",
    "Replace the legacy serialiser",
]

CANONICAL_SUMMARY = "Switch payment authorisation to the new provider SDK with connection pooling"


def canonical_incident_time(reference: datetime) -> datetime:
    """The moment the demo incident begins. Fixed relative to the reference date."""
    return reference.replace(
        hour=CANONICAL_INCIDENT_HOUR, minute=CANONICAL_INCIDENT_MINUTE, second=0, microsecond=0
    )


def canonical_change_time(reference: datetime) -> datetime:
    return canonical_incident_time(reference) - timedelta(minutes=CANONICAL_LEAD_MINUTES)


def _sha(rng: random.Random) -> str:
    """A deterministic fake commit sha derived from the seeded RNG."""
    return hashlib.sha256(str(rng.random()).encode()).hexdigest()[:12]


def generate_changes(
    rng: random.Random,
    service_ids: list[str],
    reference: datetime,
    count: int = 300,
) -> list[Change]:
    """Generate `count` changes spread over the 12 months before the reference date."""
    changes: list[Change] = []

    # The canonical change first, so its id is stable regardless of how many others exist.
    changes.append(
        Change(
            change_id=CANONICAL_CHANGE_ID,
            service_id=CANONICAL_SERVICE,
            deployed_at=canonical_change_time(reference),
            author="d.svensson",
            summary=CANONICAL_SUMMARY,
            risk="high",
            rollback_available=True,
            commit_sha="9f2c41ab77de",
            status="succeeded",
        )
    )

    for n in range(1, count):
        # Spread across 365 days before the reference date, deterministically.
        minutes_back = 60 + (n * 1699) % (365 * 24 * 60)
        deployed_at = reference - timedelta(minutes=minutes_back)
        service_id = service_ids[(n * 7) % len(service_ids)]
        risk = weighted_pick(rng, [("low", 5.0), ("medium", 3.0), ("high", 1.5)])
        status = weighted_pick(rng, [("succeeded", 8.0), ("rolled_back", 1.2), ("partial", 0.8)])
        changes.append(
            Change(
                change_id=f"CHG-2026-{n:04d}",
                service_id=service_id,
                deployed_at=deployed_at,
                author=pick(rng, AUTHORS),
                summary=pick(rng, SUMMARIES),
                risk=risk,
                # High-risk changes almost always ship with a rollback path; that is what makes
                # "roll back CHG-2026-0873" a legitimate plan step.
                rollback_available=risk == "high" or rng.random() < 0.75,
                commit_sha=_sha(rng),
                status=status,
            )
        )

    changes.sort(key=lambda c: (c.deployed_at, c.change_id))
    return changes
