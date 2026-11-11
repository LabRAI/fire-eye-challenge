# Fire-eye weekly forecast challenge

Implementation target: 10 November 2026.

GitHub Pages serves the interface. The existing Supabase account gateway verifies
GitHub sessions and accepts data-only forecast submissions. A dedicated HF Dataset
stores round definitions, submitted forecasts, immutable receipts, observations,
reference outputs and results. GitHub-hosted Actions collect observations and score
the submitted arrays on a schedule. No laboratory CPU/GPU runner is used.

Participants run their own models locally or on infrastructure they control,
including their own HF Space. Fire-eye never executes uploaded models, containers
or notebooks. HF Dataset storage does not supply compute.

## Weekly contract

- A round contains fourteen consecutive 12-hour forecast windows (seven days).
- Submit before the round begins; server/HF commit times determine eligibility.
- Use the native CONUS 5 km EPSG:5070 grid, not the 50 m visual refinement.
- Initial intake is regional: up to 4,096 cells and 512 KiB per JSON submission.
  All fourteen windows must cover the same pinned land mask.
- Forecasts and chosen model labels are public. No model weights are accepted.
- Retrying identical bytes is idempotent; accepted bytes are never overwritten.
- Supabase retains only pseudonymous identity/session and bounded activity data.
  Forecast arrays, model metadata, observations and scores belong in HF.

## Evaluation and fair comparisons

The first operational track is documented wildfire-discovery cell capture in
the top 10% and 20% of submitted land area, with fractional treatment of ties.
WFIGS observations exclude prescribed-fire records and are pinned by checksum.
It is a prospective incident comparison, not a complete satellite-occupancy
benchmark. Unreported cells are not verified negatives: do not report accuracy
or PR-AUC. No observed positive cells means no score.

Observations receive a 72-hour reporting allowance; results refresh through day
seven after each window and are then frozen. Store method version, evidence
hashes and evaluation time. Retain previous results when a source fails.
Compare FireWx-FM only where a matching reference was actually published and
captured before the cutoff. Its 48-hour horizon must not be stretched to seven
days. Different regions, rounds and horizons are separate comparisons; do not
pool them into a misleading overall leaderboard.

## Reliability and operations

- Daily external Supabase health checks exercise the database, independently of
  user visits. A weekly status commit records the real check; no fake user events.
- GitHub Actions uses hosted runners, finite timeouts and a concurrency lock.
- HF commits use optimistic concurrency; receipts contain trusted publication
  times and content hashes. Retry safe operations, never advance a late cutoff.
- The map remains usable when authentication, HF or evaluation is unavailable.
- Health checks improve detection and activity, but are not an uptime guarantee
  or a replacement for the provider's paid availability policy.
- Avatar URLs are returned during OAuth and kept only in browser tab storage.
  They are not stored in Supabase, session tokens, HF receipts or usage records.
