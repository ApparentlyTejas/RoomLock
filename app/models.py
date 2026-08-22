from sqlalchemy import Boolean, Column, ForeignKey, Integer, String, DateTime
from sqlalchemy.dialects.postgresql import ARRAY, UUID
from sqlalchemy.sql import func

from app.database import Base


class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True)
    email = Column(String, unique=True, nullable=False, index=True)
    # Nullable: accounts created via "Sign in with Google" have no password
    # at all (there's nothing to hash) — login.py must check for this
    # before calling verify_password, not just let it fail.
    hashed_password = Column(String, nullable=True)
    google_refresh_token = Column(String, nullable=True)
    is_admin = Column(Boolean, nullable=False, default=False, server_default="false")
    calendar_feed_token = Column(String, nullable=True, unique=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())


class Resource(Base):
    __tablename__ = "resources"

    id = Column(Integer, primary_key=True)
    name = Column(String, nullable=False)
    description = Column(String, nullable=True)
    capacity = Column(Integer, nullable=True)
    amenities = Column(ARRAY(String), nullable=False, default=list, server_default="{}")
    created_at = Column(DateTime(timezone=True), server_default=func.now())


class Booking(Base):
    __tablename__ = "bookings"

    id = Column(Integer, primary_key=True)
    resource_id = Column(Integer, ForeignKey("resources.id"), nullable=False, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    start_time = Column(DateTime(timezone=True), nullable=False)
    end_time = Column(DateTime(timezone=True), nullable=False)
    idempotency_key = Column(String, nullable=True)
    google_event_id = Column(String, nullable=True)
    series_id = Column(UUID(as_uuid=True), nullable=True, index=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
