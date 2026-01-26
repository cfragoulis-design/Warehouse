
# PATCH 1: supplier dropdown support
from fastapi import APIRouter, Request, Depends
from sqlalchemy.orm import Session
from app.db import get_db
from app.models import Supplier, Consumable

router = APIRouter()

@router.get("/consumables")
def list_consumables(request: Request, db: Session = Depends(get_db)):
    suppliers = db.query(Supplier).filter(Supplier.is_active == True).all()
    consumables = db.query(Consumable).all()
    return templates.TemplateResponse(
        "consumables_list.html",
        {"request": request, "suppliers": suppliers, "consumables": consumables}
    )
