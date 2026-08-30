from __future__ import annotations

import json

import pytest
from fastapi import HTTPException, Request

from app import label_designer_surface
from app.label_layout import LabelLayoutConflictError, canonical_layout_defaults
from app.models import User


def _request(*, content_type: str = "application/json") -> Request:
    headers = [(b"host", b"warehouse.example")]
    if content_type:
        headers.append((b"content-type", content_type.encode("ascii")))
    return Request(
        {
            "type": "http",
            "method": "POST",
            "scheme": "https",
            "path": "/admin/labels/layouts",
            "raw_path": b"/admin/labels/layouts",
            "query_string": b"",
            "headers": headers,
            "server": ("warehouse.example", 443),
            "client": ("127.0.0.1", 12345),
            "session": {},
        }
    )


def _user(role: str) -> User:
    return User(username=f"designer-{role}", role=role, pin_hash="not-used")


def test_label_designer_routes_are_isolated_under_admin_labels() -> None:
    routes = {
        (route.path, frozenset(route.methods or set()))
        for route in label_designer_surface.router.routes
    }
    assert ("/admin/labels/designer", frozenset({"GET"})) in routes
    assert ("/admin/labels/layouts", frozenset({"GET"})) in routes
    assert ("/admin/labels/layouts", frozenset({"POST"})) in routes
    assert (
        "/admin/labels/layouts/{version_id}/activate",
        frozenset({"POST"}),
    ) in routes
    assert ("/admin/labels/layouts/reset", frozenset({"POST"})) in routes


@pytest.mark.parametrize("role", ["workshop", "user", ""])
def test_label_designer_admin_dependency_fails_closed(role: str) -> None:
    with pytest.raises(HTTPException) as forbidden:
        label_designer_surface.require_designer_admin(_user(role))
    assert forbidden.value.status_code == 403


def test_label_designer_admin_dependency_accepts_admin() -> None:
    admin = _user("admin")
    assert label_designer_surface.require_designer_admin(admin) is admin


def test_save_draft_route_passes_only_authenticated_actor_and_version_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}
    settings = canonical_layout_defaults()
    admin = _user("admin")

    def fake_save(db, **kwargs):
        captured.update(kwargs)
        return {"id": 2, "version": 2, "settings": settings}

    monkeypatch.setattr(label_designer_surface, "save_layout_draft", fake_save)
    monkeypatch.setattr(
        label_designer_surface,
        "layout_state",
        lambda db: {"version_token": 1, "defaults": settings, "bounds": {}},
    )

    response = label_designer_surface.label_layouts_save_draft(
        request=_request(),
        payload={
            "settings": settings,
            "reason": "Μεγαλύτερο LOT",
            "expected_version": 1,
        },
        user=admin,
        db=object(),
    )

    assert response.status_code == 201
    assert json.loads(response.body)["version"]["id"] == 2
    assert captured["actor"] is admin
    assert captured["expected_version"] == 1
    assert captured["reason"] == "Μεγαλύτερο LOT"


def test_mutation_routes_require_application_json() -> None:
    with pytest.raises(HTTPException) as unsupported:
        label_designer_surface.label_layouts_reset(
            request=_request(content_type="text/plain"),
            payload={"reason": "Reset", "expected_version": 1},
            user=_user("admin"),
            db=object(),
        )
    assert unsupported.value.status_code == 415


def test_layout_conflict_maps_to_http_409(monkeypatch: pytest.MonkeyPatch) -> None:
    def conflict(*args, **kwargs):
        raise LabelLayoutConflictError("reload")

    monkeypatch.setattr(label_designer_surface, "reset_layout", conflict)
    with pytest.raises(HTTPException) as stale:
        label_designer_surface.label_layouts_reset(
            request=_request(),
            payload={"reason": "Canonical reset", "expected_version": 1},
            user=_user("admin"),
            db=object(),
        )
    assert stale.value.status_code == 409
