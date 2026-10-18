"""Incident history: 500 records over 12 months, with signal planted for later episodes.

Planted deliberately, per DATA-MODEL.md section 2:

- **Severity mix** 4% Sev1, 18% Sev2, 46% Sev3, 32% Sev4, so severity distribution looks real and
  a triage agent that always answers Sev2 scores badly.
- **12 near-duplicate pairs.** Two incidents describing the same failure in different words. The
  knowledge agent must surface the *right* prior incident, which is a retrieval-quality test rather
  than a generation test.
- **3 incidents with outdated resolutions** referencing a runbook step that no longer exists.
  These feed the `stale_knowledge` eval category alongside the stale runbooks.
- **Seasonality.** A spike in the two weeks after each quarterly release, so time-based reasoning
  has something to find.
"""

from __future__ import annotations

import random
from datetime import datetime, timedelta

from atlas_common.models import Incident

from .common import EMAIL_DOMAIN, pick, weighted_pick

# DATA-MODEL.md section 2. Weights, not counts, so the mix survives a change in total.
SEVERITY_MIX: list[tuple[str, float]] = [
    ("Sev1", 4.0),
    ("Sev2", 18.0),
    ("Sev3", 46.0),
    ("Sev4", 32.0),
]

CATEGORIES: list[tuple[str, float]] = [
    ("latency", 22.0),
    ("availability", 20.0),
    ("deployment", 18.0),
    ("capacity", 14.0),
    ("config", 12.0),
    ("data", 9.0),
    ("security", 5.0),
]

# Phrasings for the same underlying failure, used to build near-duplicate pairs.
SYMPTOM_PHRASINGS: dict[str, list[str]] = {
    "latency": [
        "p95 latency climbed above the SLO threshold",
        "requests started taking far longer than usual",
        "customers reported the page hanging before it loaded",
    ],
    "availability": [
        "error rate spiked and requests began failing",
        "the service started returning 5xx to a share of traffic",
        "health checks began failing intermittently",
    ],
    "deployment": [
        "a deploy introduced a regression that had to be rolled back",
        "the new revision failed to serve traffic correctly",
        "a release caused errors that stopped when it was reverted",
    ],
    "capacity": [
        "the connection pool was exhausted under peak load",
        "resource saturation caused queuing and timeouts",
        "the service ran out of headroom during a traffic surge",
    ],
    "config": [
        "a configuration change was applied with the wrong value",
        "a feature flag was enabled for the wrong cohort",
        "an environment variable was missing after a restart",
    ],
    "data": [
        "replica lag caused stale reads for some customers",
        "duplicate messages were processed twice",
        "a batch job wrote records with an incorrect timestamp",
    ],
    "security": [
        "token validation began rejecting valid sessions",
        "a certificate expired on a downstream dependency",
        "an access policy change locked out a service principal",
    ],
}

ROOT_CAUSES = [
    "A change to the upstream client removed the retry budget.",
    "The connection pool ceiling was left at the development default.",
    "A dependency began throttling without a corresponding alert.",
    "Cache entries expired simultaneously, producing a stampede.",
    "The new revision shipped with debug logging enabled.",
    "A schema migration ran ahead of the code that expected it.",
    "A certificate renewal job had been silently failing for weeks.",
]

RESOLUTIONS = [
    "Rolled back the offending change and confirmed recovery.",
    "Scaled out by two replicas and raised the pool ceiling.",
    "Disabled the feature flag and re-enabled it after a fix.",
    "Failed over to the secondary region while the primary recovered.",
    "Purged the affected cache namespace and staggered the TTLs.",
    "Restarted the unhealthy replicas one at a time.",
]

# Reference retired tooling. An agent that repeats these verbatim is actioning stale knowledge.
OUTDATED_RESOLUTIONS = [
    "Restarted the legacy sidecar with `atlasctl sidecar restart` and the errors cleared.",
    "Flushed the v1 edge cache using `atlas-edge-cli flush` to restore normal latency.",
    "Re-registered the service with the old discovery agent to fix routing.",
]

RESOLVERS = [
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
]

# Quarterly releases. Incidents cluster in the fortnight after each.
RELEASE_OFFSETS_DAYS = [30, 120, 210, 300]


def _seasonal_offset(rng: random.Random, n: int) -> int:
    """Days before the reference date, biased to the two weeks after a quarterly release."""
    if n % 5 == 0:  # roughly a fifth of incidents cluster
        base = RELEASE_OFFSETS_DAYS[n % len(RELEASE_OFFSETS_DAYS)]
        return max(1, base - rng.randrange(0, 14))
    return 1 + (n * 271) % 364


def generate_incidents(
    rng: random.Random,
    service_ids: list[str],
    change_ids_by_service: dict[str, list[str]],
    reference: datetime,
    count: int = 500,
) -> list[Incident]:
    """Generate `count` historical incidents."""
    incidents: list[Incident] = []

    # 12 near-duplicate pairs at fixed slots, so the set never drifts with a seed change. The
    # second member of each pair mirrors the first's service and category but describes it in
    # different words - that is what makes retrieval, rather than string matching, the thing under
    # test.
    duplicate_partner: dict[int, int] = {7 + i * 17 + 1: 7 + i * 17 for i in range(12)}
    outdated_slots = {113, 254, 401}

    # Category is derived from the slot rather than drawn, so a duplicate can share its partner's
    # category without needing to observe the RNG draw that produced it.
    def category_for(slot: int) -> str:
        return weighted_pick(random.Random(slot), CATEGORIES)

    for n in range(1, count + 1):
        severity = weighted_pick(rng, SEVERITY_MIX)
        partner = duplicate_partner.get(n)
        source_slot = partner if partner is not None else n
        category = category_for(source_slot)
        service_id = service_ids[(source_slot * 3) % len(service_ids)]

        phrasings = SYMPTOM_PHRASINGS[category]
        # The duplicate takes a different phrasing of the same symptom.
        symptom = phrasings[(1 if partner is not None else 0) % len(phrasings)]

        days_back = _seasonal_offset(rng, n)
        opened_at = reference - timedelta(days=days_back, minutes=(n * 37) % 1440)
        ttd = 2 + (n % 25)
        ttm = ttd + 5 + (n % 90)
        closed_at = opened_at + timedelta(minutes=ttm)

        is_outdated = n in outdated_slots
        resolution = (
            OUTDATED_RESOLUTIONS[sorted(outdated_slots).index(n)]
            if is_outdated
            else pick(rng, RESOLUTIONS)
        )

        service_name = service_id.removeprefix("svc-").replace("-", " ")
        if partner is not None:
            # Mirror the partner's affected set exactly. Only the wording differs, which is what
            # makes this a retrieval test: a searcher must distinguish two genuinely similar
            # incidents rather than two incidents that merely share a word.
            #
            # `incidents` is still in slot order here (the sort happens at the end), so the
            # partner is at index partner-1.
            affected = list(incidents[partner - 1].affected_services)
        else:
            affected = [service_id]
            if severity in {"Sev1", "Sev2"} and rng.random() < 0.6:
                affected.append(service_ids[(n * 5 + 1) % len(service_ids)])

        linked = change_ids_by_service.get(service_id, [])
        linked_change_ids = [linked[n % len(linked)]] if linked and rng.random() < 0.45 else []

        incidents.append(
            Incident(
                incident_id=f"INC-2026-{n:04d}",
                opened_at=opened_at,
                closed_at=closed_at,
                severity=severity,  # type: ignore[arg-type]
                category=category,  # type: ignore[arg-type]
                title=f"{service_name.title()} {category} degradation",
                description=(
                    f"On {opened_at.date().isoformat()}, {symptom} on `{service_id}`. "
                    f"Impact was observed for approximately {ttm} minutes before mitigation."
                ),
                affected_services=sorted(set(affected)),
                root_cause=pick(rng, ROOT_CAUSES),
                resolution=resolution,
                resolved_by=f"{pick(rng, RESOLVERS)}@{EMAIL_DOMAIN}",
                time_to_detect_minutes=ttd,
                time_to_mitigate_minutes=ttm,
                linked_change_ids=linked_change_ids,
                postmortem_url=(
                    f"https://wiki.{EMAIL_DOMAIN}/postmortems/INC-2026-{n:04d}"
                    if severity in {"Sev1", "Sev2"}
                    else None
                ),
                tags=sorted({category, severity.lower(), service_id}),
            )
        )

    incidents.sort(key=lambda i: (i.opened_at, i.incident_id))
    return incidents
