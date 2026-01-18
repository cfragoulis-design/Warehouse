from datetime import datetime
from sqlalchemy import Integer, String, Boolean, DateTime, ForeignKey, Numeric, func
from sqlalchemy.orm import Mapped, mapped_column

# ... User, Product ήδη εδώ ...


class StockMovement(Base):
    __tablename__ = "stock_movements"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)

    product_id: Mapped[int] = mapped_column(ForeignKey("products.id", ondelete="CASCADE"), index=True, nullable=False)

    # ποσότητα πάντα θετική. Το direction (IN/OUT) καθορίζει το πρόσημο λογιστικά.
    qty: Mapped[float] = mapped_column(Numeric(12, 3), nullable=False)

    # IN / OUT / ADJ
    movement_type: Mapped[str] = mapped_column(String(8), nullable=False, default="IN")

    note: Mapped[str | None] = mapped_column(String(255), nullable=True)

    # ποιος έκανε την κίνηση (optional, αλλά χρήσιμο)
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), index=True, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )
