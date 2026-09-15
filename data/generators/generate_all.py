"""Generate the complete ATLAS synthetic dataset, deterministically.

    uv run python -m data.generators.generate_all --seed 20260914 --out data/seed

Re-running with the same seed produces byte-identical files. `tests/test_determinism.py` asserts
it and CI enforces it, because every rehearsal and every demo depends on the same prompt giving the
same shape of answer.

The generator also ASSERTS the graph properties DATA-MODEL.md requires rather than hoping for them.
A seed change that broke the dependency shape would otherwise surface three phases later as a
confusing eval failure.
"""

from __future__ import annotations

import argparse
import random
import sys
from datetime import datetime
from pathlib import Path

from atlas_common.graph import blast_radius, build_reverse_index, dependency_depth, detect_cycles
from atlas_common.models import Service

from .changes import (
    CANONICAL_CHANGE_ID,
    CANONICAL_SERVICE,
    canonical_change_time,
    canonical_incident_time,
    generate_changes,
)
from .common import (
    DEFAULT_REFERENCE_DATE,
    DEFAULT_SEED,
    iso,
    parse_reference_date,
    write_json,
    write_jsonl,
    write_text,
)
from .incidents import generate_incidents
from .people import generate_entitlements, generate_people
from .runbooks import CANONICAL_RUNBOOK_ID, generate_runbooks, runbooks_by_service
from .services import generate_services
from .telemetry import generate_telemetry

# Requirements from DATA-MODEL.md, asserted rather than assumed.
MIN_DEPENDENCY_DEPTH = 4
MIN_CHECKOUT_BLAST_RADIUS = 8
MIN_IDENTITY_FANOUT = 11
EXPECTED_SERVICES = 25
EXPECTED_TIER_MIX = {1: 4, 2: 7, 3: 9, 4: 5}
EXPECTED_RUNBOOKS = 60
EXPECTED_STALE_RUNBOOKS = 5
EXPECTED_CANARIES = 2
EXPECTED_INCIDENTS = 500
EXPECTED_CHANGES = 300


class GenerationError(RuntimeError):
    """Raised when generated data violates a documented invariant."""


def _assert_graph(services: list[Service]) -> None:
    """Fail loudly if the catalogue no longer has the shape the demos rely on."""
    if len(services) != EXPECTED_SERVICES:
        raise GenerationError(f"expected {EXPECTED_SERVICES} services, got {len(services)}")

    tiers: dict[int, int] = {}
    for service in services:
        tiers[service.tier] = tiers.get(service.tier, 0) + 1
    if tiers != EXPECTED_TIER_MIX:
        raise GenerationError(f"tier mix {tiers} != {EXPECTED_TIER_MIX}")

    cycles = detect_cycles(services)
    if cycles:
        raise GenerationError(f"dependency graph is not a DAG: {cycles}")

    depth = dependency_depth(services)
    if depth < MIN_DEPENDENCY_DEPTH:
        raise GenerationError(f"dependency depth {depth} < {MIN_DEPENDENCY_DEPTH}")

    checkout = blast_radius("svc-checkout-api", services, depth=3)
    if checkout.affected_count < MIN_CHECKOUT_BLAST_RADIUS:
        raise GenerationError(
            f"svc-checkout-api blast radius {checkout.affected_count} < {MIN_CHECKOUT_BLAST_RADIUS}"
        )

    fanout = len(build_reverse_index(services).get("svc-identity", []))
    if fanout < MIN_IDENTITY_FANOUT:
        raise GenerationError(f"svc-identity fans out to {fanout} < {MIN_IDENTITY_FANOUT}")

    # The canonical narrative: payments breaking must actually affect checkout.
    payments = blast_radius(CANONICAL_SERVICE, services, depth=3)
    if "svc-checkout-api" not in payments.affected_service_ids:
        raise GenerationError("svc-payments blast radius no longer reaches svc-checkout-api")


def generate(seed: int, out_dir: Path, reference: datetime) -> dict[str, int]:
    """Generate every artefact and write it under `out_dir`. Returns a count per entity."""
    rng = random.Random(seed)

    # Runbooks first: the catalogue carries runbook_ids, so the ids must exist before services.
    # A placeholder catalogue supplies the service ids and teams the runbooks reference.
    placeholder = generate_services(random.Random(seed), {})
    service_ids = [s.service_id for s in placeholder]
    service_teams = {s.service_id: s.owner_team for s in placeholder}

    runbooks = generate_runbooks(rng, service_ids, service_teams, reference)
    services = generate_services(random.Random(seed), runbooks_by_service(runbooks))
    _assert_graph(services)

    changes = generate_changes(rng, service_ids, reference)
    changes_by_service: dict[str, list[str]] = {}
    for change in changes:
        changes_by_service.setdefault(change.service_id, []).append(change.change_id)

    incidents = generate_incidents(rng, service_ids, changes_by_service, reference)
    telemetry = generate_telemetry(rng, services, incidents, reference)
    people = generate_people(rng)
    entitlements = generate_entitlements(rng)

    # -- content assertions --------------------------------------------------------------------
    if len(runbooks) != EXPECTED_RUNBOOKS:
        raise GenerationError(f"expected {EXPECTED_RUNBOOKS} runbooks, got {len(runbooks)}")
    canaries = [fm.runbook_id for fm, _ in runbooks if fm.injection_canary]
    if len(canaries) != EXPECTED_CANARIES:
        raise GenerationError(f"expected {EXPECTED_CANARIES} injection canaries, got {canaries}")
    if not any(fm.runbook_id == CANONICAL_RUNBOOK_ID for fm, _ in runbooks):
        raise GenerationError(f"{CANONICAL_RUNBOOK_ID} missing; Episode 4 asserts on it")
    if len(incidents) != EXPECTED_INCIDENTS:
        raise GenerationError(f"expected {EXPECTED_INCIDENTS} incidents, got {len(incidents)}")
    if len(changes) != EXPECTED_CHANGES:
        raise GenerationError(f"expected {EXPECTED_CHANGES} changes, got {len(changes)}")
    if not any(c.change_id == CANONICAL_CHANGE_ID for c in changes):
        raise GenerationError(f"{CANONICAL_CHANGE_ID} missing; the demo depends on it")
    if not any(p.role == "readonly" for p in people):
        raise GenerationError("readonly persona missing; Episode 4 unauthorized evals need it")

    # -- write -----------------------------------------------------------------------------------
    out_dir.mkdir(parents=True, exist_ok=True)
    write_json(out_dir / "services.json", [s.model_dump(mode="json") for s in services])
    write_jsonl(out_dir / "incidents.jsonl", incidents)
    write_jsonl(out_dir / "changes.jsonl", changes)
    write_jsonl(out_dir / "telemetry.jsonl", telemetry)
    write_json(out_dir / "people.json", [p.model_dump(mode="json") for p in people])
    write_json(out_dir / "entitlements.json", [e.model_dump(mode="json") for e in entitlements])

    for front_matter, body in runbooks:
        write_text(out_dir / "runbooks" / f"{front_matter.runbook_id}.md", body)

    # A manifest so the seeder and the verification script can assert counts without re-generating.
    stale_ids = sorted(
        fm.runbook_id
        for fm, _ in runbooks
        if (reference.date() - fm.last_reviewed).days > 545  # ~18 months
    )
    if len(stale_ids) != EXPECTED_STALE_RUNBOOKS:
        raise GenerationError(
            f"expected {EXPECTED_STALE_RUNBOOKS} stale runbooks, got {len(stale_ids)}: "
            f"{stale_ids}. The stale_knowledge eval category depends on this count."
        )

    manifest = {
        "seed": seed,
        "reference_date": iso(reference),
        "counts": {
            "services": len(services),
            "runbooks": len(runbooks),
            "incidents": len(incidents),
            "changes": len(changes),
            "telemetry": len(telemetry),
            "people": len(people),
            "entitlements": len(entitlements),
        },
        "canonical": {
            "change_id": CANONICAL_CHANGE_ID,
            "change_service": CANONICAL_SERVICE,
            "change_at": iso(canonical_change_time(reference)),
            "incident_at": iso(canonical_incident_time(reference)),
            "runbook_id": CANONICAL_RUNBOOK_ID,
        },
        "injection_canaries": sorted(canaries),
        "stale_runbooks": stale_ids,
    }
    write_json(out_dir / "manifest.json", manifest)

    counts = dict(manifest["counts"])  # type: ignore[arg-type]
    return counts


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED)
    parser.add_argument("--out", type=Path, default=Path("data/seed"))
    parser.add_argument(
        "--reference-date",
        default=DEFAULT_REFERENCE_DATE,
        help="Anchor for every timestamp. Never the wall clock.",
    )
    args = parser.parse_args(argv)

    reference = parse_reference_date(args.reference_date)
    try:
        counts = generate(args.seed, args.out, reference)
    except GenerationError as exc:
        print(f"generation failed: {exc}", file=sys.stderr)
        return 1

    print(f"ATLAS seed data  (seed={args.seed}, reference={iso(reference)})")
    print(f"written to {args.out}\n")
    width = max(len(k) for k in counts)
    for name, count in counts.items():
        print(f"  {name:<{width}}  {count}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
