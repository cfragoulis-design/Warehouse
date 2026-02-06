# === Central Ready routes ===
# Add imports near the top of app.py
from app.services import get_central_ready, set_central_ready, clear_central_ready
from starlette.responses import RedirectResponse

# === Add these routes anywhere after app is defined ===

@app.post("/central/ready")
def central_ready(request):
    # admin only (use your existing guard if present)
    with db.connect() as conn:
        set_central_ready(conn)
        conn.commit()
    return RedirectResponse("/stock", status_code=303)

@app.post("/central/ready/clear")
def central_ready_clear(request):
    with db.connect() as conn:
        clear_central_ready(conn)
        conn.commit()
    return RedirectResponse("/stock", status_code=303)

# === In your /stock route BEFORE TemplateResponse ===
# state = get_central_ready(conn)
# context.update(state)
