# app.py (root)
# Wrapper για Railway – φορτώνει το σωστό FastAPI app
from app.main import app  # noqa

if __name__ == "__main__":
    import os
    import uvicorn

    port = int(os.getenv("PORT", "8000"))
    uvicorn.run("app.main:app", host="0.0.0.0", port=port)
