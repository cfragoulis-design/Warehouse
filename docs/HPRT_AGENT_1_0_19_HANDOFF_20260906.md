# EFET / HPRT Agent 1.0.19 — separate nutrition rows

RAW LOGIC. REAL SYSTEMS.

Created by Christos Fragoulis

## Status

Local candidate prepared and verified on 6 September 2026. **Not pushed, deployed, installed or physically printed by this task.** The user confirmed that 1.0.18 is installed and working, including centering. This candidate addresses the follow-up: multiple nutrition entries supplied as one glued string.

- Worktree: `C:\Users\Onroid\Documents\Sklavounos Operations\tmp\efet-agent-nutrition-rows-v1019-20260906`
- Branch: `codex/efet-agent-nutrition-rows-v1019-20260906`
- Base: `149aecd` (verified 1.0.18 candidate)
- Package source commit: `8d25f81888f01600e2441c480dafec6552bcf185`
- Production-targeted artifact: `app/static/downloads/SKLAVOUNOS-WAREHOUSE-HPRT-AGENT-V1.0.19.zip`
- Size: 1,075,831 bytes
- SHA-256: `1c48975796a27fcbddd2b4607453b9dc53acdc377f14678c964fbb6c0b46b217`

## Changes and boundaries

- Each nutrition entry has one full-width, centered row.
- CR/LF, Unicode line breaks, semicolons, pipes and letter-followed commas are understood. Recognized Greek/English nutrient names can be adjacent to the preceding unit, as in `kcalΠρωτεΐνη` and `gΛιπαρά`.
- Only recognized duplicate per-100g headings are removed. Supplied names, values, decimal separators, ranges, units and order are preserved; missing nutrients are not invented.
- The photographed input is now five entries: Energy, Protein, Fat, Carbohydrates, Salt. The test fixture is transcribed from the user's screenshot, not read from or written to the database.
- Requested row height acts as the maximum. Actual available body space accounts for dates, LOT, optional source lot, storage, origin and optional usage text. The row-height floor is 14 pixels; existing font limits remain unchanged.
- Too many entries (>8), header-only content and insufficient room fail before printer output. Nothing is silently truncated to eight rows.
- Browser label-designer parsing and row geometry match the renderer. These browser changes and download-link updates are local only and require a separately approved Warehouse deployment.
- Layout v1 field names/defaults/hash contract and backend snapshot validation are untouched to avoid invalidating existing immutable layouts. Actual rendered content still must fit above the fixed footer boundary.
- Version visibility, status history fixes, creator signature, installer/token reuse, leased print protocol and duplicate-print journal are preserved from 1.0.18. No database migration, stock/lot modification, product-content update, queue operation or printer intervention occurred.

## Verification

**92 distinct focused tests passed across bounded runs**:

- 31 existing agent/renderer/label-center compatibility tests before packaging.
- 30 nutrition geometry/content tests: counts 1–8, actual optional-tail budgeting, centered full-width rows, unchanged header/footer rasters, preview geometry, insufficient-space rejection and UTF-8 BOM/font limits.
- 11 parser/preview tests: exact screenshot text, Greek/English names, glued units, decimal commas, ranges, multiline input and overflow/no-truncation.
- Final package/status gate: both exact package-to-source/hash/environment checks and all 18 status/history/version tests passed (21 selected tests including one repeated existing UI test).

Ruff, Node syntax and Git whitespace checks passed. All renderer executions used dry-run files; no spooler or live provider was used. The five-row PNG was visually inspected with no missing text or footer overlap:

`tmp/qa-nutrition-v1019-20260906/screenshot-nutrition.png`

The preview uses a clearly synthetic product/business/LOT fixture with the transcribed nutrition text. It is not a production label or nutritional/legal certification.

## Physical acceptance after approval

1. Wait until the Workshop agent has no active print job; briefly avoid new submissions.
2. Extract the production-targeted 1.0.19 ZIP into a fresh folder. Close the old status window and run `SETUP.cmd` as administrator with the same Windows account as the existing installation. The same-origin token is reused when readable; history/journal are retained.
3. Open the status shortcut and confirm **1.0.19**.
4. With permission, print one normal Ρολό Κοτόπουλο label. Confirm five separate rows with the unchanged stored values/ranges, centered table, correct dates/LOT, one history entry and no duplicate print.
5. If rollback is needed, reinstall retained 1.0.18 only when idle. Do not restore an older journal over current print activity.

No server restart or Warehouse database migration is required for the local Agent fix. Do not deploy the entire branch or overwrite concurrent Warehouse work without coordination and approval.
