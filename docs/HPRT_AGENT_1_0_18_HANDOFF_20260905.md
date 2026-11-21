# EFET / HPRT Agent 1.0.18 — local release candidate

RAW LOGIC. REAL SYSTEMS.  
Created by Christos Fragoulis

## Status and source

Prepared and tested locally on 5 September 2026. **Not pushed, published, deployed, installed, or physically printed.** No live Windows task, printer, queue, token, product data or database was changed.

- Branch: `codex/efet-agent-history-layout-v1018-20260905`
- Worktree: `C:\Users\Onroid\Documents\Sklavounos Operations\tmp\efet-agent-history-layout-v1018-20260905`
- Base: `dd35ab2ed0cb2930e9a827069264527d210da67b`
- Committed package source: `4bd0e3c2159abbc94829ecaa1996608d253840d7`
- Production-targeted ZIP: `app/static/downloads/SKLAVOUNOS-WAREHOUSE-HPRT-AGENT-V1.0.18.zip`
- ZIP SHA-256: `92fe492e94e911b6eae0897024b20c0323c891bb8d9db02c51326bcc42c768ab`
- ZIP size: 1,074,767 bytes. No agent token included.
- Existing 1.0.17 packages retained.

## What changed

1. History uses actual UTC instants, not formatted `dd/MM/yyyy` text. This fixes August 31 incorrectly sorting above September 5. Newest entries are selected after sorting.
2. Last print uses the newest valid successful-print evidence from status or history. Failed events do not become successful prints; missing history is not fabricated.
3. Installed package version is visible in the main titlebar, header and About window. The installer copies the package manifest. Missing/invalid metadata displays unavailable, never an invented version.
4. The odd final nutrition cell spans the full centered table width. Paired rows retain their existing geometry. The corresponding browser label-designer preview was updated too.
5. Canonical CF and the complete secondary creator signature are present in the status footer and About. No creator credit was added to regulated labels.

The leased print protocol, stock/lot logic, agent polling, journal, print-history writer, approved company label logo and product nutrition values are unchanged.

## Verification

One final focused release run: **67 passed in 63.26 seconds**:

```powershell
$env:DATABASE_URL = 'sqlite+pysqlite:///:memory:'
& 'C:\Users\Onroid\Documents\Sklavounos Operations\Warehouse\.venv\Scripts\python.exe' -m pytest tests/test_hprt_dynamic_label_agent.py tests/test_hprt_status_history.py tests/test_hprt_nutrition_alignment.py -q -p no:cacheprovider
```

Coverage includes Windows PowerShell 5.1/BOM compatibility, exact package-to-source parity, package hashes and target environment, no packaged token, schemas 3–7, chronological month/year/timezone transitions, invalid timestamps, newest last-print evidence and installed version metadata. Nutrition tests execute the real dry-run renderer for 1–8 entries; even rows match the previous raster, and odd-row placement agrees with the browser preview.

Ruff and `node --check` passed. Real WinForms construction was rendered offline at default size with synthetic state, without showing windows or reading live providers. Main and About version/signature controls were visually checked and do not overlap. QA images are under local `tmp/qa-status-v1018-20260905/` (not runtime/package dependencies).

## Installation and physical acceptance — only when approved

1. On the Workshop PC, wait until no active print job is running and briefly avoid new submissions. The existing SETUP restarts the agent.
2. Extract the **production-targeted** 1.0.18 ZIP into a fresh folder. Close any old status window. Run `SETUP.cmd` as administrator under the same Windows account used for the existing installation.
3. The existing same-origin DPAPI token is reused if readable. History and duplicate-print journal are preserved. Do not delete or replace these data files, and do not paste tokens into a chat.
4. Open `EFET Print Agent - Status`: confirm **1.0.18**, correct recent last-print time and chronological history.
5. With permission, submit one normal label using a product with a single nutrition entry. Confirm centered full-width cell, one new history entry and updated last print. Confirm no duplicate print.
6. If actual jobs are still absent, inspect Workshop `print-history.jsonl`, `agent-status.json` and diagnostics for `HISTORY_FAILED`. The local sort reproduction does not prove every historical record exists on that PC.

If rollback is needed, wait for idle and reinstall the retained 1.0.17 package. Preserve the current live history/journal; do not restore an older journal over newer print activity.

## Boundaries / remaining work

- The agent fixes do not require a database migration or server restart.
- Download links and the browser preview are updated only in this local candidate; they need a separately approved Warehouse deployment to appear on the live website. Do not deploy this entire branch casually or overwrite concurrent Warehouse work.
- The user's label photo appears to contain descriptive nutrition text without numerical values. This is separate product content, not corrected or invented here.
- Physical installation/printing remains unverified. No claim that the Workshop now runs 1.0.18.
