"""Exportação retomável; um único escritor por diretório de estado compartilhado."""
import hashlib
import io
import json
from contextlib import contextmanager
from datetime import timezone

import httplib2
from filelock import FileLock, Timeout as LockTimeout
from google.oauth2.credentials import Credentials
from google_auth_httplib2 import AuthorizedHttp
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaIoBaseUpload
from pydantic import BaseModel

from app.config import Settings
from app.models import Result
from app.storage import save_json

SCOPES = ["https://www.googleapis.com/auth/drive.file"]
HEADERS = ["consulta_id", "nome", "cpf", "consultado_em", "arquivo_json", "status", "codigo"]


class ExportReceipt(BaseModel):
    consulta_id: str
    arquivo_id: str
    arquivo_nome: str
    arquivo_url: str
    planilha_url: str
    linha: int


class ExportError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


@contextmanager
def google_services(settings: Settings):
    token = settings.google_token_json.get_secret_value() if settings.google_token_json else ""
    credentials = (Credentials.from_authorized_user_info(json.loads(token), SCOPES) if token
                   else Credentials.from_authorized_user_file(str(settings.google_token_file), SCOPES))
    # Serviços construídos por exportação: httplib2 não é compartilhado entre threads.
    drive = build("drive", "v3", http=AuthorizedHttp(credentials, http=httplib2.Http(timeout=30)), cache_discovery=False)
    sheets = build("sheets", "v4", http=AuthorizedHttp(credentials, http=httplib2.Http(timeout=30)), cache_discovery=False)
    try:
        yield drive, sheets
    finally:
        drive.close()
        sheets.close()


def filename(result: Result) -> str:
    stamp = result.consultado_em.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    return f"{result.consulta_id}_{stamp}.json"


class GoogleExporter:
    def __init__(self, settings: Settings, services=google_services):
        self.settings, self.services = settings, services

    def export(self, result: Result) -> ExportReceipt:
        if not self.settings.google_configured:
            raise ExportError("GOOGLE_NAO_CONFIGURADO", "Configure o OAuth Google, a pasta e a planilha antes de exportar")
        directory = self.settings.export_state_dir
        directory.mkdir(parents=True, exist_ok=True)
        try:
            with FileLock(str(directory / "google.lock"), timeout=5):
                with self.services(self.settings) as (drive, sheets):
                    return self._export(result, drive, sheets)
        except LockTimeout as exc:
            raise ExportError("EXPORTACAO_OCUPADA", "Há outra exportação em andamento; repita com o mesmo JSON") from exc
        except HttpError as exc:
            raise ExportError("GOOGLE_API_ERRO", f"Google respondeu HTTP {exc.resp.status}; repita a exportação com o mesmo JSON após corrigir o acesso") from exc

    def _export(self, result, drive, sheets):
        settings = self.settings
        # "origem" muda quando o resultado vem do histórico; o JSON arquivado é o mesmo.
        payload = result.model_dump_json(indent=2, exclude={"origem"}).encode("utf-8")
        digest = hashlib.sha256(payload).hexdigest()
        state_key = hashlib.sha256((settings.google_drive_folder_id + settings.google_spreadsheet_id + settings.google_sheet_tab + result.consulta_id).encode()).hexdigest()
        state_path = settings.export_state_dir / f"{state_key}.json"
        state = json.loads(state_path.read_text()) if state_path.exists() else {}
        if state.get("sha256") and state["sha256"] != digest:
            raise ExportError("CONFLITO_CONSULTA", "Este consulta_id já está associado a outro conteúdo")
        state["sha256"] = digest
        file_id = state.get("file_id")
        if not file_id:
            # Recuperação do checkpoint a partir do Drive após perda de estado local.
            escape = lambda text: text.replace("\\", "\\\\").replace("'", "\\'")
            query = (f"'{escape(settings.google_drive_folder_id)}' in parents and trashed = false "
                     f"and appProperties has {{ key='consulta_id' and value='{escape(result.consulta_id)}' }}")
            existing = drive.files().list(q=query, fields="files(id,appProperties)", pageSize=2).execute(num_retries=2)["files"]
            if len(existing) > 1:
                raise ExportError("ARQUIVOS_DUPLICADOS", "Mais de um arquivo encontrado para esta consulta; confira o Drive")
            if existing:
                if existing[0].get("appProperties", {}).get("sha256") != digest:
                    raise ExportError("CONFLITO_CONSULTA", "O arquivo no Drive contém outro conteúdo para este consulta_id")
                file_id = existing[0]["id"]
            else:
                file_id = drive.files().generateIds(count=1, space="drive").execute(num_retries=2)["ids"][0]
            state["file_id"] = file_id
            # Persiste ANTES do upload. Repetir criação usa o mesmo ID.
            save_json(state_path, state)
        try:
            metadata = drive.files().get(fileId=file_id, fields="id,appProperties,trashed").execute(num_retries=2)
        except HttpError as exc:
            if exc.resp.status != 404:
                raise
            metadata = None
        if metadata:
            if metadata.get("trashed") or metadata.get("appProperties", {}).get("sha256") != digest:
                raise ExportError("CONFLITO_ARQUIVO", "O arquivo existente foi alterado ou enviado à lixeira")
        else:
            drive.files().create(body={
                "id": file_id, "name": filename(result), "parents": [settings.google_drive_folder_id],
                "mimeType": "application/json", "appProperties": {"consulta_id": result.consulta_id, "sha256": digest},
            }, media_body=MediaIoBaseUpload(io.BytesIO(payload), mimetype="application/json", resumable=True), fields="id").execute(num_retries=0)
        file_url = f"https://drive.google.com/file/d/{file_id}/view"
        tab = "'" + settings.google_sheet_tab.replace("'", "''") + "'"
        values_api = sheets.spreadsheets().values()
        ids = values_api.get(spreadsheetId=settings.google_spreadsheet_id, range=f"{tab}!A:A").execute(num_retries=2).get("values", [])
        if not ids or ids[0] != [HEADERS[0]]:
            raise ExportError("CABECALHO_INVALIDO", "A planilha precisa ter consulta_id na célula A1; execute o setup ou corrija o cabeçalho")
        matches = [i + 1 for i, row in enumerate(ids) if row and row[0] == result.consulta_id]
        if len(matches) > 1:
            raise ExportError("LINHAS_DUPLICADAS", "Há múltiplas linhas para esta consulta; confira a planilha")
        person = result.pessoa
        row = [result.consulta_id, person.nome if person else "", person.cpf if person else "",
               result.consultado_em.isoformat(), file_url, result.status, result.codigo or ""]
        if matches:
            line = matches[0]
            values_api.update(spreadsheetId=settings.google_spreadsheet_id, range=f"{tab}!A{line}:G{line}", valueInputOption="RAW", body={"values": [row]}).execute(num_retries=2)
        else:
            # Sem retry automático no append: após resposta incerta, uma nova
            # exportação pesquisa o consulta_id antes de inserir novamente.
            response = values_api.append(spreadsheetId=settings.google_spreadsheet_id, range=f"{tab}!A:G", valueInputOption="RAW", insertDataOption="INSERT_ROWS", body={"values": [row]}).execute(num_retries=0)
            import re
            line = int(re.search(r"!A(\d+)", response["updates"]["updatedRange"]).group(1))
        receipt = ExportReceipt(consulta_id=result.consulta_id, arquivo_id=file_id, arquivo_nome=filename(result), arquivo_url=file_url,
                                planilha_url=f"https://docs.google.com/spreadsheets/d/{settings.google_spreadsheet_id}/edit", linha=line)
        state["receipt"] = receipt.model_dump()
        save_json(state_path, state)
        return receipt
