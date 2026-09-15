# PHASE 02 — Synthetic data and seeding

> Depends on: Phase 01.

---

Implement `_labpack/DATA-MODEL.md` exactly. Deterministic generation, then load into Cosmos DB and
Azure AI Search.

## Tasks

1. **Models** — `src/atlas_common/models/`: Pydantic v2 models for `Service`, `SLO`, `Incident`,
   `Change`, `TelemetryWindow`, `Person`, `Entitlement`, plus the runbook front-matter model.
   These are the single source of truth; generators, tools, agents and the ops API all import them.

2. **Generators** — `data/generators/`:
   - `generate_all.py --seed --out` orchestrates the rest.
   - Use `random.Random(seed)` and `faker` seeded from the same value. No wall-clock time anywhere:
     take a `--reference-date` (default `2026-09-01T00:00:00Z`) and derive all timestamps from it.
   - Build the service dependency DAG first, assert acyclicity, assert depth ≥ 4 and that
     `svc-checkout-api` has a blast radius ≥ 8 and `svc-identity` fans out to ≥ 11.
   - Generate incidents with the planted signal described in the data model (near-duplicate pairs,
     outdated resolutions, severity mix, post-release spikes).
   - Generate runbooks as markdown with YAML front matter. Include the 5 stale runbooks and the
     2 `injection_canary: true` runbooks with clearly-fenced injection payloads.
   - Generate changes, ensuring the canonical `CHG` lands 6 minutes before the canonical demo
     incident on `svc-payments` with `risk: high`.
   - Generate telemetry windows consistent with the incidents (an incident at T must have anomalous
     windows around T for its affected services).
   - Generate people and entitlements, including the `readonly` persona.

3. **Determinism test** — `tests/test_determinism.py`: generate twice into temp dirs, assert
   byte-identical output. Wire into CI.

4. **Seeder** — `scripts/seed.py`:
   - Cosmos: create containers if missing, bulk upsert with deterministic ids. Use
     `azure-cosmos` with `DefaultAzureCredential`.
   - AI Search: create/update `idx-atlas-knowledge` with fields
     `id, doc_type, doc_id, title, chunk, services (Collection filterable), risk, last_reviewed,
     owner_team, source_path, content_vector (3072 dims, HNSW), injection_canary (bool)`.
     Add a semantic configuration over `title` + `chunk`.
   - Chunk runbooks at ~800 tokens with 100-token overlap, preserving heading context in each chunk.
   - Embed with the `embed-main` deployment through the Foundry project endpoint.
   - Also index incidents (title + description + resolution) and postmortems as `doc_type` variants.
   - Idempotent: re-running updates in place, never duplicates.
   - Print a summary table of counts per container/index.

5. **Read helpers** — `src/atlas_common/repositories/`: thin typed repositories
   (`ServiceRepository`, `IncidentRepository`, `ChangeRepository`, `TelemetryRepository`,
   `EntitlementRepository`, `KnowledgeSearch`). All async, all with explicit timeouts, all
   returning Pydantic models. These are what the Functions ops API and the agent tools will call —
   write them once, here.

6. **Blast radius** — `src/atlas_common/graph.py`: `blast_radius(service_id, depth) -> BlastRadius`
   walking `depends_on` in reverse (who depends on me). Pure function over the service catalogue,
   fully unit tested. The impact agent uses this; the model does not compute it.

7. **Golden fixtures** — `tests/fixtures/`: freeze the canonical demo incident, the expected blast
   radius, and the top-3 expected knowledge hits. Episode 4's regression tests reference these.

## Verification — `scripts/verify_phase02.sh`

1. Cosmos counts match generated counts exactly.
2. AI Search document count matches expected chunk count; a known query
   (`"checkout latency after payments deploy"`) returns `RB-` runbook for `svc-checkout-api` in the
   top 3 with semantic ranking on.
3. `blast_radius("svc-checkout-api")` matches the frozen fixture.
4. The 2 injection canaries are present and findable by filter `injection_canary eq true`.
5. Re-running `scripts/seed.py` changes nothing (compare `_ts`-independent doc hashes before/after).

## Acceptance criteria

- [ ] `uv run python -m data.generators.generate_all --seed 20260914 --out data/seed` is reproducible.
- [ ] `make seed` loads everything and `scripts/verify_phase02.sh` is green.
- [ ] `uv run pytest tests/test_determinism.py tests/test_graph.py` passes.
- [ ] No generated content references a real company, person, domain, or product. Domains are
      `*.contoso.example`, people are obviously fictional.

## Report back

Counts per entity, the top-3 search results for the canonical query, and the frozen blast radius.
