
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse

router = APIRouter()

@router.get("/admin/labels", response_class=HTMLResponse)
async def labels_page(request: Request):
    return HTMLResponse("<h1>Label Center Ready</h1>")

@router.post("/admin/labels/print")
async def print_labels(payload: dict):
    return JSONResponse({"status": "queued", "data": payload})
