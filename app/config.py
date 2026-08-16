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

    class Config:
        env_file = ".env"


settings = Settings()
