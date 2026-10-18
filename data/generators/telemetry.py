"""Pre-computed telemetry windows.

Windows, not a live metrics store, so the same demo prompt yields the same numbers every rehearsal
(CLAUDE.md section 6). Two properties make the data useful rather than decorative:

- Every incident has anomalous windows around it for each affected service, so a triage agent that
  actually looks at telemetry can corroborate a report instead of guessing.
- The canonical incident gets a hand-shaped signature: checkout p95 climbing while payments error
  rate rises, starting at the incident minute. That is the correlation the demo is built to
  surface, and the reason the impact agent can say "checkout is the symptom, payments is the cause".
"""

from __future__ import annotations

import random
from datetime import datetime, timedelta

from atlas_common.models import Incident, Service, TelemetryWindow

from .changes import CANONICAL_SERVICE, canonical_incident_time
from .scenarios import Scenario, scenario_window_offsets

WINDOW_MINUTES = 5
# Windows either side of an incident, so "before" and "after" are both visible.
WINDOWS_BEFORE = 4
WINDOWS_AFTER = 8

SEVERITY_FACTOR = {"Sev1": 1.0, "Sev2": 0.75, "Sev3": 0.45, "Sev4": 0.2}


def _baseline(service: Service, rng: random.Random) -> tuple[float, float, float]:
    """Steady-state request rate, error rate and p50, derived from the service tier."""
    rate = {1: 4200.0, 2: 1500.0, 3: 420.0, 4: 60.0}[service.tier]
    rate *= 0.85 + rng.random() * 0.3
    error = 0.05 + rng.random() * 0.2
    p50 = service.slo.latency_p95_ms * 0.35
    return round(rate, 1), round(error, 3), round(p50, 1)


def _window(
    service: Service,
    start: datetime,
    rng: random.Random,
    *,
    anomaly: bool,
    severity_factor: float = 1.0,
) -> TelemetryWindow:
    rate, error, p50 = _baseline(service, rng)
    if anomaly:
        error = min(99.0, error + 6.0 * severity_factor)
        p95 = service.slo.latency_p95_ms * (2.2 + 1.4 * severity_factor)
        saturation = min(99.0, 55.0 + 30.0 * severity_factor)
        burn = 4.0 + 8.0 * severity_factor
    else:
        p95 = service.slo.latency_p95_ms * (0.55 + rng.random() * 0.25)
        saturation = 20.0 + rng.random() * 25.0
        burn = rng.random() * 0.6
    return TelemetryWindow(
        service_id=service.service_id,
        window_start=start,
        window_minutes=WINDOW_MINUTES,
        request_rate_rpm=rate,
        error_rate_pct=round(error, 3),
        p50_ms=p50,
        p95_ms=round(p95, 1),
        p99_ms=round(p95 * 1.6, 1),
        saturation_pct=round(saturation, 1),
        slo_burn_rate=round(burn, 2),
        anomaly=anomaly,
    )


def generate_telemetry(
    rng: random.Random,
    services: list[Service],
    incidents: list[Incident],
    reference: datetime,
    staged: list[Scenario] | None = None,
    staged_rng: random.Random | None = None,
) -> list[TelemetryWindow]:
    """Build telemetry windows around every incident, plus the canonical demo signature and the
    staged scenarios' signatures (data/generators/scenarios.py)."""
    by_id = {s.service_id: s for s in services}
    windows: dict[tuple[str, datetime], TelemetryWindow] = {}

    for incident in incidents:
        factor = SEVERITY_FACTOR[incident.severity]
        for service_id in incident.affected_services:
            service = by_id.get(service_id)
            if service is None:
                continue
            for offset in range(-WINDOWS_BEFORE, WINDOWS_AFTER):
                start = (incident.opened_at + timedelta(minutes=offset * WINDOW_MINUTES)).replace(
                    second=0, microsecond=0
                )
                elapsed = offset * WINDOW_MINUTES
                anomalous = 0 <= elapsed <= incident.time_to_mitigate_minutes
                key = (service_id, start)
                # Incidents overlap in time. First write wins for ordinary incidents; the canonical
                # signature below deliberately overwrites.
                if key not in windows:
                    windows[key] = _window(
                        service, start, rng, anomaly=anomalous, severity_factor=factor
                    )

    # -- the canonical demo signature ----------------------------------------------------------
    incident_at = canonical_incident_time(reference)
    for service_id, factor in (("svc-checkout-api", 0.9), (CANONICAL_SERVICE, 1.0)):
        service = by_id[service_id]
        for offset in range(-WINDOWS_BEFORE, WINDOWS_AFTER):
            start = (incident_at + timedelta(minutes=offset * WINDOW_MINUTES)).replace(
                second=0, microsecond=0
            )
            # Overwrites unconditionally: the canonical windows must win over any incident-derived
            # window that lands on the same minute, or the demo correlation gets diluted.
            windows[(service_id, start)] = _window(
                service, start, rng, anomaly=offset >= 0, severity_factor=factor
            )

    # -- the staged scenarios, from their own RNG stream ----------------------------------------
    # Last, and never from `rng`: the canonical windows above must stay byte-identical whatever
    # scenarios exist. Overwrites like the canonical signature does, for the same reason.
    srng = staged_rng or random.Random(0)
    for scenario in staged or []:
        for signature in scenario.signatures:
            service = by_id[signature.service_id]
            for start, anomalous in scenario_window_offsets(
                    scenario.incident_at(reference), WINDOW_MINUTES, WINDOWS_BEFORE,
                    WINDOWS_AFTER):
                windows[(signature.service_id, start)] = _window(
                    service, start, srng, anomaly=anomalous,
                    severity_factor=signature.severity_factor)

    return [windows[k] for k in sorted(windows)]
