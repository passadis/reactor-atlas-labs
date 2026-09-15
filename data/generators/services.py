"""The Contoso service catalogue: 25 services forming a deliberately shaped DAG.

The topology is hand-authored rather than randomly generated. DATA-MODEL.md imposes specific graph
properties - depth >= 4, one diamond, no cycles, `svc-checkout-api` blast radius >= 8,
`svc-identity` fanning out to >= 11 - and a random graph that happens to satisfy them is both
fragile and unexplainable. This one is readable on a projector and the shape can be pointed at.

`depends_on` means "I need this to work". Blast radius runs the other way; see
`atlas_common.graph`.

The spine that matters for the canonical demo:

    checkout-api -> payments -> fraud -> ledger -> identity

An engineer reports checkout timing out; a high-risk payments deploy landed six minutes earlier.
The agent is supposed to pull that thread.
"""

from __future__ import annotations

import random

from atlas_common.models import SLO, Service

from .common import EMAIL_DOMAIN, pick

# service_id -> (name, tier, owner_team, depends_on)
# Ordered foundation-first so the layering is visible.
CATALOGUE: list[tuple[str, str, int, str, list[str]]] = [
    # -- foundation: no dependencies -----------------------------------------------------------
    ("svc-identity", "Identity Service", 1, "team-platform", []),
    ("svc-audit-log", "Audit Log", 3, "team-platform", []),
    ("svc-feature-flags", "Feature Flags", 4, "team-platform", []),
    # -- layer 1: platform consumers -----------------------------------------------------------
    ("svc-ledger", "Ledger", 1, "team-finance", ["svc-identity", "svc-audit-log"]),
    ("svc-fraud", "Fraud Detection", 2, "team-risk", ["svc-identity", "svc-ledger"]),
    ("svc-pricing", "Pricing Engine", 2, "team-commerce", ["svc-identity", "svc-feature-flags"]),
    ("svc-inventory", "Inventory", 2, "team-supply", ["svc-identity"]),
    ("svc-tax", "Tax Calculation", 3, "team-finance", ["svc-identity"]),
    ("svc-email-gateway", "Email Gateway", 3, "team-messaging", ["svc-identity"]),
    ("svc-sms-gateway", "SMS Gateway", 3, "team-messaging", ["svc-identity"]),
    # -- layer 2 -------------------------------------------------------------------------------
    # payments -> fraud -> ledger AND payments -> ledger is the required diamond.
    ("svc-payments", "Payments", 1, "team-payments", ["svc-fraud", "svc-ledger", "svc-identity"]),
    (
        "svc-notifications",
        "Notifications",
        2,
        "team-messaging",
        ["svc-email-gateway", "svc-sms-gateway", "svc-identity"],
    ),
    (
        "svc-catalog",
        "Product Catalog",
        2,
        "team-commerce",
        ["svc-pricing", "svc-inventory", "svc-identity"],
    ),
    ("svc-shipping", "Shipping", 3, "team-supply", ["svc-inventory", "svc-identity"]),
    # -- layer 3 -------------------------------------------------------------------------------
    (
        "svc-cart",
        "Shopping Cart",
        2,
        "team-commerce",
        ["svc-catalog", "svc-pricing", "svc-identity"],
    ),
    (
        "svc-orders",
        "Order Management",
        2,
        "team-commerce",
        ["svc-payments", "svc-inventory", "svc-shipping", "svc-tax"],
    ),
    # -- layer 4: the front door ---------------------------------------------------------------
    (
        "svc-checkout-api",
        "Checkout API",
        1,
        "team-commerce",
        [
            "svc-payments",
            "svc-cart",
            "svc-orders",
            "svc-tax",
            "svc-identity",
            "svc-notifications",
        ],
    ),
    # -- layer 5: checkout's dependents, which give it a blast radius ---------------------------
    ("svc-loyalty", "Loyalty Program", 3, "team-growth", ["svc-checkout-api", "svc-identity"]),
    ("svc-reviews", "Product Reviews", 3, "team-growth", ["svc-checkout-api", "svc-identity"]),
    (
        "svc-search",
        "Search",
        3,
        "team-discovery",
        ["svc-catalog", "svc-checkout-api", "svc-identity"],
    ),
    (
        "svc-recommendations",
        "Recommendations",
        3,
        "team-discovery",
        ["svc-checkout-api", "svc-catalog", "svc-identity"],
    ),
    (
        "svc-reporting",
        "Reporting",
        4,
        "team-data",
        ["svc-checkout-api", "svc-orders", "svc-ledger"],
    ),
    (
        "svc-admin-portal",
        "Admin Portal",
        4,
        "team-internal",
        ["svc-checkout-api", "svc-orders", "svc-identity"],
    ),
    (
        "svc-status-page",
        "Status Page",
        4,
        "team-internal",
        ["svc-checkout-api", "svc-notifications"],
    ),
    # depth-2 dependent of checkout, reached via reporting / recommendations / search
    (
        "svc-analytics",
        "Analytics Pipeline",
        4,
        "team-data",
        ["svc-reporting", "svc-recommendations", "svc-search"],
    ),
]

# Exactly three services are frozen. The policy gate BLOCKS destructive plans against these, which
# is what makes the Episode 2 refusal a real rule rather than a scripted moment.
CHANGE_FROZEN = {"svc-ledger", "svc-payments", "svc-identity"}

REGIONS = [
    ["eastus", "westeurope"],
    ["eastus"],
    ["westeurope", "northeurope"],
    ["eastus", "westeurope", "southeastasia"],
]

# Tier drives the SLO. Tier 1 is revenue-critical and gets the tightest targets.
SLO_BY_TIER: dict[int, tuple[float, int, int]] = {
    1: (0.9995, 250, 21),
    2: (0.999, 400, 43),
    3: (0.995, 800, 216),
    4: (0.99, 2000, 432),
}


def generate_services(
    rng: random.Random, runbooks_by_service: dict[str, list[str]]
) -> list[Service]:
    """Build the 25-service catalogue.

    Topology is fixed; only cosmetic fields (region spread, rotation naming) use the RNG, so the
    graph assertions in `generate_all` can never be broken by a seed change.
    """
    services: list[Service] = []
    for service_id, name, tier, owner_team, depends_on in CATALOGUE:
        availability, latency_p95, budget = SLO_BY_TIER[tier]
        services.append(
            Service(
                service_id=service_id,
                name=name,
                tier=tier,  # type: ignore[arg-type]
                owner_team=owner_team,
                on_call_rotation=f"rot-{owner_team.removeprefix('team-')}-primary",
                # Tier 4 is internal tooling and runs in staging; everything else is production.
                environment="staging" if tier == 4 else "prod",
                region=pick(rng, REGIONS),
                depends_on=sorted(depends_on),
                slo=SLO(
                    availability_target=availability,
                    latency_p95_ms=latency_p95,
                    error_budget_minutes_month=budget,
                ),
                runbook_ids=sorted(runbooks_by_service.get(service_id, [])),
                change_freeze=service_id in CHANGE_FROZEN,
                repository=f"contoso/{service_id.removeprefix('svc-')}",
            )
        )
    return services


def team_emails(services: list[Service]) -> dict[str, str]:
    """Owner team -> distribution list. Always a .example domain."""
    return {s.owner_team: f"{s.owner_team}@{EMAIL_DOMAIN}" for s in services}
