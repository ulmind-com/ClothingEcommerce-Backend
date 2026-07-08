from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    APP_NAME: str = "ClothingEcommerce"
    ENV: str = "development"

    MONGO_URI: str = "mongodb://localhost:27017"
    MONGO_DB: str = "clothing_ecommerce"

    JWT_SECRET: str = "change-me"
    JWT_ALGORITHM: str = "HS256"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 10080  # 7 days

    CORS_ORIGINS: str = "*"

    RAZORPAY_KEY_ID: str = ""
    RAZORPAY_KEY_SECRET: str = ""

    CLOUDINARY_CLOUD_NAME: str = ""
    CLOUDINARY_API_KEY: str = ""
    CLOUDINARY_API_SECRET: str = ""

    FIREBASE_CREDENTIALS: str = ""

    # Google OAuth: the Web client ID (aud) the mobile ID token is verified
    # against. Same value the app passes as webClientId to Google Sign-In.
    GOOGLE_CLIENT_ID: str = ""

    # Optional shared secret so an external cron can trigger the scheduled-
    # notification sweeper (belt-and-suspenders alongside the in-process loop).
    CRON_SECRET: str = ""

    # Public base URL of this backend — used to build absolute URLs for push
    # notification images (FCM needs a reachable URL, not a local asset).
    PUBLIC_BASE_URL: str = "https://clothingecommerce-backend.onrender.com"

    # AI recommendations (Groq — OpenAI-compatible). Empty key => heuristic only.
    GROQ_API_KEY: str = ""
    GROQ_MODEL: str = "llama-3.3-70b-versatile"
    GROQ_TIMEOUT: float = 8.0
    RECS_USE_LLM: bool = True

    # Separate Groq key for the customer-support chat agent (falls back to the
    # recommendations key if unset).
    GROQ_AGENT_API_KEY: str = ""

    @property
    def cors_list(self) -> list[str]:
        return [o.strip() for o in self.CORS_ORIGINS.split(",") if o.strip()]


settings = Settings()
