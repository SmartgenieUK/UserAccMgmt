from sqlalchemy import String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.db.types import TZ_DATETIME


class StripeEvent(Base):
    __tablename__ = "stripe_events"

    event_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    type: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped = mapped_column(TZ_DATETIME, server_default=func.now(), nullable=False)
