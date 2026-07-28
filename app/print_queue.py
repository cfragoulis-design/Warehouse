from __future__ import annotations

import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import text
from sqlalchemy.orm import Session


@dataclass(frozen=True)
class ProductLotPrintClaim:
    lot_id: int
    lease_token: str
    lease_expires_at: datetime


def claim_next_product_lot(
    session: Session,
    *,
    station: str,
    now: datetime,
    lease_seconds: int,
) -> ProductLotPrintClaim | None:
    lease_token = secrets.token_urlsafe(32)
    lease_expires_at = now + timedelta(seconds=max(1, lease_seconds))
    parameters = {
        "station": station,
        "now": now,
        "lease_token": lease_token,
        "lease_expires_at": lease_expires_at,
    }

    if session.bind is not None and session.bind.dialect.name == "postgresql":
        row = session.execute(
            text(
                """
                WITH candidate AS (
                    SELECT id
                    FROM product_lots
                    WHERE station = :station
                      AND (
                          status = 'QUEUED'
                          OR (
                              status = 'PROCESSING'
                              AND lease_expires_at <= :now
                          )
                      )
                    ORDER BY created_at ASC, id ASC
                    FOR UPDATE SKIP LOCKED
                    LIMIT 1
                )
                UPDATE product_lots AS lots
                SET status = 'PROCESSING',
                    lease_token = :lease_token,
                    claim_started_at = :now,
                    lease_expires_at = :lease_expires_at
                FROM candidate
                WHERE lots.id = candidate.id
                RETURNING lots.id
                """
            ),
            parameters,
        ).first()
        session.commit()
        if row is None:
            return None
        return ProductLotPrintClaim(
            lot_id=int(row[0]),
            lease_token=lease_token,
            lease_expires_at=lease_expires_at,
        )

    for _attempt in range(3):
        candidate = session.execute(
            text(
                """
                SELECT id
                FROM product_lots
                WHERE station = :station
                  AND (
                      status = 'QUEUED'
                      OR (
                          status = 'PROCESSING'
                          AND lease_expires_at <= :now
                      )
                  )
                ORDER BY created_at ASC, id ASC
                LIMIT 1
                """
            ),
            parameters,
        ).first()
        if candidate is None:
            session.commit()
            return None
        lot_id = int(candidate[0])
        result = session.execute(
            text(
                """
                UPDATE product_lots
                SET status = 'PROCESSING',
                    lease_token = :lease_token,
                    claim_started_at = :now,
                    lease_expires_at = :lease_expires_at
                WHERE id = :lot_id
                  AND station = :station
                  AND (
                      status = 'QUEUED'
                      OR (
                          status = 'PROCESSING'
                          AND lease_expires_at <= :now
                      )
                  )
                """
            ),
            {**parameters, "lot_id": lot_id},
        )
        if result.rowcount == 1:
            session.commit()
            return ProductLotPrintClaim(
                lot_id=lot_id,
                lease_token=lease_token,
                lease_expires_at=lease_expires_at,
            )
        session.rollback()
    return None


def finish_claimed_product_lot(
    session: Session,
    *,
    lot_id: int,
    station: str,
    lease_token: str,
    status: str,
    now: datetime,
) -> bool:
    if status not in {"PRINTED", "ERROR"}:
        raise ValueError("unsupported terminal print status")
    result = session.execute(
        text(
            """
            UPDATE product_lots
            SET status = :status,
                lease_token = '',
                claim_started_at = NULL,
                lease_expires_at = NULL
            WHERE id = :lot_id
              AND station = :station
              AND status = 'PROCESSING'
              AND lease_token = :lease_token
              AND lease_expires_at > :now
            """
        ),
        {
            "lot_id": int(lot_id),
            "station": station,
            "lease_token": lease_token,
            "status": status,
            "now": now,
        },
    )
    if result.rowcount != 1:
        session.rollback()
        return False
    session.commit()
    return True
