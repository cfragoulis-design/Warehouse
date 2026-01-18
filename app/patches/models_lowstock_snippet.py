# app/models.py (Product model) - add this import if missing:
from sqlalchemy import Integer

# Inside class Product(Base):
min_stock = Column(Integer, nullable=False, default=0)
