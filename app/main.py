from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from sqlalchemy import text

from app.database import SessionLocal
from app.rate_limit import limiter
from app.routers import admin, assistant, auth, bookings, google_calendar, resources

app = FastAPI(title="Booking API")
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

app.include_router(auth.router)
app.include_router(resources.router)
app.include_router(bookings.router)
app.include_router(google_calendar.router)
app.include_router(admin.router)
app.include_router(assistant.router)
app.mount("/ui", StaticFiles(directory="app/static", html=True), name="ui")


@app.get("/", include_in_schema=False)
def home():
    return FileResponse("app/static/home.html")


@app.get("/health")
def health():
    db = SessionLocal()
    try:
        db.execute(text("SELECT 1"))
        return {"status": "ok", "db": "ok"}
    finally:
        db.close()
