from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict
from pathlib import Path
from typing import Literal


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    headless: bool = True
    max_concurrent: int = Field(default=3, ge=1, le=20)
    query_timeout_seconds: float = Field(default=120, gt=0)
    navigation_timeout_ms: int = Field(default=30_000, gt=0)
    waf_wait_ms: int = Field(default=8000, ge=0, le=30000)
    # Modo assistido: se o portal pedir verificação humana, a consulta é refeita num
    # Chromium visível para a pessoa resolver; o robô só espera e depois continua.
    # Exige tela (WSLg, desktop Linux, Windows, macOS ou compose.assisted.yaml).
    assisted_mode: bool = False
    assisted_wait_seconds: float = Field(default=180, gt=0, le=900)
    max_detail_pages: int = Field(default=100, ge=1)
    api_key: SecretStr | None = None
    google_token_file: Path = Path("secrets/google-token.json")
    google_token_json: SecretStr | None = None
    require_api_key: bool = False
    # O front servido em "/" usa o API_KEY do .env sem precisar digitar a chave.
    # Em deploy público use false: quem abrir a página receberia a chave.
    front_api_key_from_env: bool = True
    # API de Dados oficial: com chave, consultas por CPF/NIS não usam o site.
    portal_api_key: SecretStr | None = None
    portal_api_url: str = "https://api.portaldatransparencia.gov.br/api-de-dados"
    portal_api_max_rpm: int = Field(default=80, ge=1, le=300)
    # De onde vêm as consultas: "dados_abertos" consulta só a base local no PostgreSQL
    # (arquivos importados com python -m app.importar_dados; não acessa o governo na consulta);
    # "auto" usa a API para CPF/NIS (com chave) e o site para nome; "api" nunca abre o site
    # (sem CAPTCHA, só CPF/NIS); "site" sempre usa o robô.
    query_source: Literal["dados_abertos", "auto", "api", "site"] = "dados_abertos"
    # Servidor MCP em /mcp com a tool consultar_pessoa (mesmo X-API-Key da API).
    mcp_enabled: bool = True
    # Mini chat do front (/chat): modelo local do Ollama + tools MCP + RAG na documentação.
    chat_enabled: bool = True
    ollama_host: str = "http://127.0.0.1:11434"
    ollama_model: str = "qwen2.5:7b"
    chat_timeout_seconds: float = Field(default=180, gt=0)
    # Memória de curto prazo: trocas lembradas por sessão e minutos sem uso até esquecer.
    chat_memory_turns: int = Field(default=6, ge=0, le=30)
    chat_memory_minutes: float = Field(default=30, gt=0)
    # Onde ficam os ZIPs baixados pelo importador de dados abertos.
    dados_abertos_dir: Path = Path("outputs/dados-abertos")
    # Histórico no PostgreSQL: vazio desativa a gravação das consultas.
    database_url: SecretStr | None = None
    # Horas em que um resultado com sucesso do histórico é reaproveitado; 0 sempre consulta o portal.
    history_cache_hours: float = Field(default=24, ge=0)
    google_drive_folder_id: str = ""
    google_spreadsheet_id: str = ""
    google_sheet_tab: str = "Consultas"
    export_state_dir: Path = Path("outputs/export-state")

    @property
    def google_configured(self) -> bool:
        token = self.google_token_json.get_secret_value() if self.google_token_json else ""
        return bool(self.google_drive_folder_id and self.google_spreadsheet_id and (token or self.google_token_file.is_file()))
