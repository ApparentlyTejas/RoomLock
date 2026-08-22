from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, EmailStr, ConfigDict, Field, field_validator


class UserRegister(BaseModel):
    email: EmailStr
    password: str = Field(min_length=8, max_length=72)

    @field_validator("password")
    @classmethod
    def password_must_be_strong(cls, value: str) -> str:
        missing = []
        if not any(c.isupper() for c in value):
            missing.append("an uppercase letter")
        if not any(c.islower() for c in value):
            missing.append("a lowercase letter")
        if not any(c.isdigit() for c in value):
            missing.append("a number")
        if missing:
            raise ValueError("Password must include " + ", ".join(missing) + ".")
        return value


class UserOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    email: EmailStr
    is_admin: bool
    created_at: datetime


class Token(BaseModel):
    access_token: str
    token_type: str = "bearer"


class ResourceCreate(BaseModel):
    name: str
    description: str | None = None
    capacity: int | None = Field(default=None, gt=0)
    amenities: list[str] = []


class ResourceOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    description: str | None
    capacity: int | None
    amenities: list[str]
    created_at: datetime


class BookingCreate(BaseModel):
    resource_id: int
    start_time: datetime
    end_time: datetime
    idempotency_key: str | None = None


class BookingUpdate(BaseModel):
    start_time: datetime
    end_time: datetime


class BookingOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    resource_id: int
    user_id: int
    start_time: datetime
    end_time: datetime
    idempotency_key: str | None
    series_id: UUID | None
    created_at: datetime


class MyBookingOut(BaseModel):
    id: int
    resource_id: int
    resource_name: str
    start_time: datetime
    end_time: datetime
    series_id: UUID | None
    created_at: datetime


class RecurringBookingCreate(BaseModel):
    resource_id: int
    start_time: datetime
    end_time: datetime
    # Weekly is the only frequency for now — the recurring-bookings story is
    # "book my weekly study group slot," not a general RRULE engine.
    occurrences: int = Field(ge=2, le=52)


class RecurringBookingResult(BaseModel):
    series_id: UUID
    created: list[BookingOut]
    skipped: list[dict]


class AdminUserRoleUpdate(BaseModel):
    is_admin: bool


class AssistantParseRequest(BaseModel):
    message: str = Field(min_length=1, max_length=500)


class AssistantParseResponse(BaseModel):
    needs_clarification: bool
    clarification: str | None = None
    resource_id: int | None = None
    resource_name: str | None = None
    start_time: datetime | None = None
    end_time: datetime | None = None
