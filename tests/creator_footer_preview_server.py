"""Local-only footer QA using actual templates, synthetic context and no database.

RAW LOGIC. REAL SYSTEMS.
Created by Christos Fragoulis
"""
from pathlib import Path
from types import SimpleNamespace

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from jinja2 import Environment, FileSystemLoader
import uvicorn


ROOT = Path(__file__).resolve().parents[1]
app = FastAPI()
app.mount("/static", StaticFiles(directory=ROOT / "app/static"))
templates = Environment(loader=FileSystemLoader(ROOT / "app/templates"), autoescape=True)
PAGES = {
    "login", "access_denied", "dashboard", "products_list",
    "labels_center", "label_designer",
}


@app.get("/{page}", response_class=HTMLResponse)
def preview(page: str) -> HTMLResponse:
    if page not in PAGES:
        raise HTTPException(status_code=404)
    return HTMLResponse(templates.get_template(f"{page}.html").render(
        user=SimpleNamespace(role="workshop", username="preview"),
        products=[], product_samples=[], print_jobs=[], categories=[],
        business_label_ready=True,
    ))


if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=8769, log_level="warning")
