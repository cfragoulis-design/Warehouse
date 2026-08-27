# Lot Passport Scan Mode

## Goal

Give Warehouse staff one fast, read-first screen after scanning a QR code or
barcode. The scan resolves a single `ProductLot`; it must not create or alter
stock by itself.

The passport shows:

- lot code, product and label profile;
- declared origin and supplier/source reference when available;
- production, receipt and expiry dates;
- current quantity by location, with the last movement time;
- chronological movement history, including transfer pairs and operator;
- print status and safe label reprint action;
- recall impact: affected locations, remaining quantity and downstream
  movements that need investigation.

## Small first slice

1. Encode a stable internal URL containing the immutable lot identifier and a
   human-readable lot code on new labels. Do not put sensitive business data in
   the QR payload.
2. Add an authenticated, mobile-friendly passport page. Resolve by ID, verify
   the lot code, and return an explicit not-found/retired result.
3. Read the existing lot, stock-location and movement-ledger data. Mark fields
   as `Not recorded` rather than guessing provenance or dates.
4. Allow reprint through the existing leased print queue. Record that it is a
   reprint and preserve the original lot identity; scanning never prints
   directly.

## Recall behavior

The first release is an impact view, not an automated recall engine. A user can
flag a lot for review and see every location and movement affected. Blocking
stock, customer notification, disposal and external communication remain
separate, explicitly authorized workflows. Reprints must visibly carry the same
lot identity and must not clear a recall/review flag.

## Phased rollout

- **Phase 0 — data check:** sample recent lots and measure missing origin,
  supplier, date and movement links. Fix only release-blocking gaps.
- **Phase 1 — read-only pilot:** QR on internal labels, passport page, location
  quantities and history for one station and a small product set.
- **Phase 2 — controlled actions:** leased-queue reprint plus recall-impact flag,
  with audit entries and role checks.
- **Phase 3 — broader traceability:** supplier document links, downstream order
  references and recall exports after operational validation.

## Release guardrails

Ship the passport behind a feature flag and keep existing labels and print
agents compatible. No stock schema rewrite is required for Phase 1. Validate
scan success, lookup latency, missing-data rate and quantity reconciliation in
staging before enabling one pilot station. A passport discrepancy never changes
the ledger automatically; staff use the existing correction workflow with a
meaningful reason.
