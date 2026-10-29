from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict
from pathlib import Path


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    headless: bool = True
    max_concurrent: int = Field(default=3, ge=1, le=20)
    query_timeout_seconds: float = Field(default=120, gt=0)
    navigation_timeout_ms: int = Field(default=30_000, gt=0)
    waf_wait_ms: int = Field(default=8000, ge=0, le=30000)
    max_detail_pages: int = Field(default=100, ge=1)
    api_key: SecretStr | None = None
    google_token_file: Path = Path("secrets/google-token.json")
    google_token_json: SecretStr | None = None
    require_api_key: bool = False
    # API de Dados oficial: com chave, consultas por CPF/NIS não usam o site.
    portal_api_key: SecretStr | None = None
    portal_api_url: str = "https://api.portaldatransparencia.gov.br/api-de-dados"
    portal_api_max_rpm: int = Field(default=80, ge=1, le=300)
    # Histórico no PostgreSQL: vazio desativa a gravação das consultas.
    database_url: SecretStr | None = None
    google_drive_folder_id: str = ""
    google_spreadsheet_id: str = ""
    google_sheet_tab: str = "Consultas"
    export_state_dir: Path = Path("outputs/export-state")

    @property
    def google_configured(self) -> bool:
        token = self.google_token_json.get_secret_value() if self.google_token_json else ""
        return bool(self.google_drive_folder_id and self.google_spreadsheet_id and (token or self.google_token_file.is_file()))
