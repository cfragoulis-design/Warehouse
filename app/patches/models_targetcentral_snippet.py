# app/models.py (Product model)
# Add import if missing:
from sqlalchemy import Integer

# Inside class Product(Base):
target_central = Column(Integer, nullable=False, default=0)
