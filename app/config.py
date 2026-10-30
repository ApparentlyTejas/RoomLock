from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    database_url: str
    jwt_secret_key: str
    jwt_algorithm: str = "HS256"
    jwt_access_token_expire_minutes: int = 60

    # Optional: Google Calendar OAuth. Left blank, the connect flow just
    # returns 503 rather than failing app startup — lets the app run before
    # these are configured.
    google_client_id: str = ""
    google_client_secret: str = ""
    google_redirect_uri: str = "http://localhost:8000/auth/google/callback"

    # Fernet key encrypting google_refresh_token at rest (a leaked DB
    # shouldn't hand out live Google Calendar access). Generate with:
    #   python3 -c "import base64,os;print(base64.urlsafe_b64encode(os.urandom(32)).decode())"
    token_encryption_key: str = ""

    # Optional: Groq API key powering the natural-language booking
    # assistant (GPT OSS 120B, open-weight, served via Groq's free tier).
    # Get one at console.groq.com/keys. Left blank, the assistant endpoint
    # just returns 503 rather than failing app startup.
    groq_api_key: str = ""

    # Set per-container in docker-compose.scale-demo.yml so responses can
    # identify which replica served them (X-Replica-Id header) — proves
    # requests are actually being distributed across instances, not just
    # asserted. Meaningless (and harmless) outside that demo.
    replica_id: str = "unknown"

    # Defaults are sized for a single uvicorn worker process (the normal
    # deployment — see Dockerfile's WEB_CONCURRENCY default of 1). The
    # scale-demo stack runs multiple worker processes per container and
    # overrides these lower per-process so total connections across every
    # process on every replica stay under Postgres's max_connections —
    # see docker-compose.scale-demo.yml for the actual math.
    db_pool_size: int = 20
    db_max_overflow: int = 20

    # Concurrency-control strategy for create_booking(): "exclusion_constraint"
    # (default) relies solely on the Postgres EXCLUDE constraint; "redis" also
    # serializes attempts on the same resource through a Redis lock. See
    # app/booking_lock.py. redis_url is only read when the strategy is "redis".
    booking_lock_strategy: str = "exclusion_constraint"
    redis_url: str = "redis://redis:6379/0"

    class Config:
        env_file = ".env"


# pydantic-settings populates required fields from the environment/.env file
# at runtime, not from constructor arguments — mypy can't see that, since it
# only looks at Settings.__init__'s declared signature.
settings = Settings()  # type: ignore[call-arg]
