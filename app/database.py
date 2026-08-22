from sqlalchemy import create_engine
from sqlalchemy.orm import declarative_base, sessionmaker

from app.config import settings

# SQLAlchemy's un-configured default (pool_size=5, max_overflow=10 — 15
# connections max per process) is undersized for any real concurrent load;
# found this the hard way load-testing at a few hundred concurrent
# requests, where it manifested as client-side read timeouts waiting on a
# free connection, not as a Postgres or app error. Sizing is per-process
# (see app/config.py's db_pool_size/db_max_overflow docstring for how this
# interacts with multiple worker processes and Postgres's max_connections).
# pool_pre_ping avoids handing out a connection that died while idle in
# the pool (e.g. a host-level network blip) instead of failing the request.
engine = create_engine(
    settings.database_url,
    pool_size=settings.db_pool_size,
    max_overflow=settings.db_max_overflow,
    pool_pre_ping=True,
)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

Base = declarative_base()


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
