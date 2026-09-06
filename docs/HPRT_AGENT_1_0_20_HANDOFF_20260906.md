# EFET / HPRT Agent 1.0.20 - Full/Simple Designer

RAW LOGIC. REAL SYSTEMS.

Created by Christos Fragoulis

## Status and scope

Local implementation and release package, 6 September 2026. This task has not
pushed, deployed, installed, migrated a database or physically printed anything.
The user requested a complete update without further physical trial cycles.
Automated and visual checks were performed locally with synthetic data.

- Branch: `codex/efet-designer-profiles-v1020-20260906`
- Worktree: `C:\Users\Onroid\Documents\Sklavounos Operations\tmp\efet-designer-profiles-v1020-20260906`
- Base: `2cce827`, the accepted 1.0.19 nutrition-row candidate.
- Production-targeted ZIP: `app/static/downloads/SKLAVOUNOS-WAREHOUSE-HPRT-AGENT-V1.0.20.zip`
- Exact source commit and SHA256 are recorded by the deterministic builder in
  `app/static/downloads/HPRT-AGENT-PRODUCTION-RELEASE-MANIFEST.json`.
- Existing 1.0.19 ZIPs are retained unchanged for compatibility evidence/rollback.

## Implemented

- Designer has separate Full/Simple controls, real-product preview, larger bounded
  fonts/heights, profile defaults and an Auto-fit action. Both profiles and shared
  legal content are saved as one immutable version. Editing one profile does not
  modify the other. Old versions retain their original flat layout semantics.
- Simple requires explicit plain-traceability and nutrition-exemption flags plus
  empty ingredients, allergens and nutrition. The unit alone is never enough.
  Existing product-readiness and valid-unit rules are not relaxed.
- Auto-fit fits the actual visible sections above pixel449; hidden settings and
  the fixed legal footer are retained. The Agent measures the complete text,
  reduces fonts to their existing minimums if necessary, and rejects rather than
  clips text that still cannot fit. There is no unlimited safe font size.
- Selectable company logo: NONE, existing SKLAVOUNOS_MARK, or the PDF-derived
  SKLAVOUNOS_ENGLISH. New layouts put it top-centre; size is per-profile. HPRT is
  monochrome. The supplied PDF is unchanged; provenance/hash is in
  `LABEL_LOGO_SKLAVOUNOS_ENGLISH.md`. Legal identity and approval oval stay separate.
- Schema8 carries layout contract2 `{full, simple}`, each with34 integer fields.
  The hash covers the entire bundle; legal content comes from the same version.
- The Agent declares `x-label-schema-max: 8`. Older agents cannot lease schema8;
  compatible older jobs remain claimable behind newer ones. Legacy unleased
  delivery/ack routes cannot deliver or acknowledge schema8 jobs. Schemas3-7
  retain their established behavior and produce identical raster bytes to1.0.19.
- Status window/About continue to read the installed package version. History,
  same-origin DPAPI token reuse and duplicate-print journal are preserved.
- Creator mark/signature is on Designer/Agent authorship surfaces only, never
  added to the food label.

## Verification evidence

Focused runs, not a full repository regression or Production certification:

- Backend layout/version, readiness/content, migration catalog and queue tests;
  including atomic bundles, disabled rollout gate, legacy clients, future schema9
  exclusion, and compatible-job progress behind65unsupported jobs.
- Native PowerShell renderer tests for Full/Simple, both bundled logos, hash
  matching, no clipping, fixed footer and exact schema3-7 raster parity against
  the retained1.0.19 package. All executions use dry-run TSPL/PNG files, no spooler.
- Designer Node VM tests cover independent edits/Auto-fit,7nutrition rows, empty
  sections, overflow prevention, disabled activation and historical v1 preview.
- Actual local browser: Full7-row Auto-fit reached449px and enabled saving;
  Simple enlarged title to48px with95px logo and filled the available body.
  Desktop visual review and820px tablet layout check passed without horizontal
  overflow. Browser tabs and the read-only local server were closed afterward.
- `test_hprt_designer_autofit.py` renders the actual Chromium-computed Full
  settings through Windows GDI+ successfully, guarding cross-renderer differences.
- Additional Agent/status/content check:33passed,31deselected.
- Ruff, JavaScript syntax, Git whitespace and final package-to-source checks.

PostgreSQL migration execution was NOT performed; only the migration file,
catalog/model alignment and focused SQLite/mock coverage were checked. No live
product values, lots, quantities, nutritional claims or queues were altered.

## Remaining approved-release boundary

The ZIP alone can update the Agent but cannot add buttons to the live Warehouse.
The new Designer requires a separately authorized Warehouse deployment. Do not
deploy this older-base branch wholesale over concurrent Warehouse changes.

After explicit authorization of the live update:

1. Integrate this candidate with the actual current Warehouse source; preserve
   concurrent work. Take the usual verified database backup and record the active
   v1 layout ID and current release before changing the server.
2. Run the existing guarded one-shot migration workflow including
   `20260906_001_label_layout_profiles.sql`. This only relaxes the layout-version
   CHECK to allow1or2; it changes no saved layout/queue rows or active pointer.
   Validate it in PostgreSQL as part of the approved release procedure.
3. Deploy the matching web/backend with `WAREHOUSE_LABEL_PROFILES_SCHEMA8_ENABLED`
   unset/false. Existing schema6/7 flags and active layout remain unchanged.
4. Install Agent1.0.20 on the Workshop PC only while idle, from the extracted
   production-targeted ZIP, using SETUP.cmd with the existing Windows account.
   Existing same-origin token/history/journal are retained. Do not copy an older
   journal over current printing activity.
5. Explicitly enable schema8, verify readiness, then save/activate the desired
   Full/Simple version from Designer. Choose the logo and use Auto-fit on the
   relevant product preview. Installation never activates a new server layout.

Rollback: use the new application to reactivate the recordedv1layout FIRST,
then turn schema8 off. Already-queued schema8 snapshots remain immutable and
still require Agent1.0.20; keep it installed until those jobs are handled. Do not
downgrade the Agent while it has schema8 jobs. The relaxed CHECK can remain in
place; there is no need to rewrite/delete historical versions.
