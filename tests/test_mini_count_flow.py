"""Mini-count through the app: confirmation for big variances, no negatives,
attribution from the worker session, tile placement on the kiosk home."""

from __future__ import annotations

from decimal import Decimal

import pytest

from app.models import ConsumableMovement, ConsumableStock
from tests.fix_support import AppHarness, login

AJAX = {"X-Requested-With": "XMLHttpRequest", "Accept": "application/json"}


@pytest.fixture
def harness(monkeypatch):
    h = AppHarness(monkeypatch)
    yield h
    h.close()


def _kiosk(harness, ids):
    client = harness.client()
    login(client, "warehouse", "1357")
    client.post("/consumables/take/session", data={"worker_id": str(ids["worker"]), "pin": "4321"}, headers=AJAX)
    return client


def test_large_variance_needs_confirmation_and_negatives_are_refused(harness) -> None:
    ids = harness.seed()  # 50 in stock, packs of 10
    client = _kiosk(harness, ids)
    url = f"/consumables/{ids['item']}/count"
    neg = client.post(url, data={"packs": "-1", "units": "0", "reason": "φύρα"}, headers=AJAX)
    assert neg.status_code == 400 and "αρνητική" in neg.json()["message"]
    big = client.post(url, data={"packs": "2", "units": "0", "reason": "φύρα"}, headers=AJAX)
    assert big.status_code == 409 and big.json()["code"] == "confirm_variance"
    db = harness.db()
    assert db.query(ConsumableStock).one().qty == Decimal("50")
    db.close()
    ok = client.post(url, data={"packs": "2", "units": "0", "reason": "φύρα", "confirm": "1"}, headers=AJAX)
    assert ok.status_code == 200 and ok.json()["counted"] == "20"
    small = client.post(url, data={"packs": "2", "units": "4", "reason": "λάθος"}, headers=AJAX)
    assert small.status_code == 200
    db = harness.db()
    rows = db.query(ConsumableMovement).order_by(ConsumableMovement.id).all()
    assert [m.qty for m in rows] == [Decimal("-30"), Decimal("4")]
    assert all(m.worker_id == ids["worker"] for m in rows)
    db.close()


def test_count_tile_comes_after_all_and_uses_the_tile_layout(harness, monkeypatch) -> None:
    ids = harness.seed()
    import app.mini_count as mini

    monkeypatch.setattr(mini, "count_is_due", lambda db, now: True)
    client = _kiosk(harness, ids)
    html = client.get("/consumables/take?kiosk=1").text
    home = html[html.index('id="kioskHome"'):html.index("</section>", html.index('id="kioskHome"'))]
    assert home.index("homeAll") < home.index('id="openCount"')
    tile = home[home.index('id="openCount"') - 60:]
    assert 'class="homeCard countTile"' in tile
    assert '<span class="homeName">Πρωινή καταμέτρηση</span>' in tile
    assert "data-cat" not in tile.split("</button>")[0]
    assert 'name="packs"' in html and 'name="units"' in html
    assert "querySelectorAll('.homeCard[data-cat]')" in html
