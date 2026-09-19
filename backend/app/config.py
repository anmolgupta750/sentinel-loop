from pathlib import Path

from dotenv import load_dotenv
from pydantic_settings import BaseSettings, SettingsConfigDict

BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env")


class Settings(BaseSettings):
	app_name: str = "Sentinel Loop"
	groq_api_key: str = ""
	groq_model: str = "openai/gpt-oss-20b"
	database_url: str = "postgresql://postgres:postgres@localhost:5432/sentinel"
	chroma_path: str = str(BASE_DIR / "chroma_data")
	audit_db_path: str = str(BASE_DIR / "data" / "audit.sqlite3")

	model_config = SettingsConfigDict(env_file=BASE_DIR / ".env", extra="ignore")


settings = Settings()
