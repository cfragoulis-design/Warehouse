from __future__ import annotations

from pathlib import Path

from app import services


ROOT = Path(__file__).resolve().parents[1]


def test_label_center_bootstrap_json_escapes_stored_markup() -> None:
    malicious = "</script><img src=x onerror=alert(1)>"
    template = services.templates.env.get_template("labels_center.html")

    rendered = template.render(
        request=None,
        user={"role": "admin"},
        products=[{"id": 1, "name": malicious}],
        print_jobs=[{"id": 2, "product_name": malicious}],
        hprt_agent_download_url="/agent.zip",
        hprt_agent_download_label="Agent",
        business_label_ready=True,
    )

    assert malicious not in rendered
    assert "\\u003c/script\\u003e" in rendered
    assert "products_json | safe" not in rendered


def test_label_center_never_interpolates_product_or_job_fields_into_inner_html() -> None:
    source = (ROOT / "app/templates/labels_center.html").read_text(encoding="utf-8")

    assert "products_json | safe" not in source
    assert "print_jobs_json | safe" not in source
    assert ".innerHTML = `" not in source
    assert ".innerHTML = \"" not in source
    assert "insertAdjacentHTML" not in source
    assert "textContent = value" in source
    assert "toast.textContent = message" in source
