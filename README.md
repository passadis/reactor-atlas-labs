# Project ATLAS

Contoso Cloud Operations incident copilot. One deployed system, four Microsoft Reactor episodes.

- Build instructions and ground rules: `CLAUDE.md`
- Architecture and data model: `_labpack/ARCHITECTURE.md`, `_labpack/DATA-MODEL.md`
- Verified API baseline and deviations: `docs/API-NOTES.md`
- On-stage runbooks: `_labpack/runbook/EPISODE-1.md` .. `EPISODE-4.md`

## Quick start

```bash
make install          # uv sync
make lint             # ruff + mypy
make test             # pytest
bash scripts/preflight.sh
bash scripts/check_region.sh eastus2
```
