import json
from contextlib import contextmanager
from unittest.mock import Mock

import httplib2
import pytest
from googleapiclient.errors import HttpError

from app.config import Settings
from app.google_export import ExportError, GoogleExporter
from app.models import Result


def request(action):
    return Mock(execute=Mock(side_effect=lambda **kwargs: action()))


class GoogleMemory:
    """Doubles com efeitos persistentes para reproduzir respostas perdidas."""
    def __init__(self):
        self.files, self.rows = {}, [["consulta_id"]]
        self.created = 0
        self.lose_upload_response = False
        self.lose_append_response = False
        self.drive, self.sheets = Mock(), Mock()
        files, values = self.drive.files.return_value, self.sheets.spreadsheets.return_value.values.return_value
        files.list.side_effect = lambda **kw: request(lambda: {"files": [{"id": key, **value} for key, value in self.files.items()]})
        files.generateIds.side_effect = lambda **kw: request(lambda: {"ids": [f"file-{len(self.files) + 1}"]})
        files.get.side_effect = lambda fileId, **kw: request(lambda: self.get_file(fileId))
        files.create.side_effect = lambda body, media_body, **kw: request(lambda: self.create_file(body, media_body))
        values.get.side_effect = lambda **kw: request(lambda: {"values": [[row[0]] for row in self.rows]})
        values.append.side_effect = lambda body, **kw: request(lambda: self.append(body))
        values.update.side_effect = lambda **kw: request(lambda: {"updatedCells": 7})

    def get_file(self, file_id):
        if file_id not in self.files:
            raise HttpError(httplib2.Response({"status": "404"}), b'{"error":{"message":"not found"}}')
        return self.files[file_id]

    def create_file(self, body, media):
        self.created += 1
        self.files[body["id"]] = body
        self.payload = json.loads(media.getbytes(0, media.size()))
        if self.lose_upload_response:
            self.lose_upload_response = False
            raise OSError("connection lost after commit")
        return {"id": body["id"]}

    def append(self, body):
        self.rows.extend(body["values"])
        if self.lose_append_response:
            self.lose_append_response = False
            raise OSError("connection lost after commit")
        return {"updates": {"updatedRange": f"Consultas!A{len(self.rows)}:G{len(self.rows)}"}}

    @contextmanager
    def services(self, settings):
        yield self.drive, self.sheets


@pytest.fixture
def exporter(tmp_path):
    token = tmp_path / "token.json"
    token.write_text("{}")
    settings = Settings(google_token_file=token, google_drive_folder_id="folder", google_spreadsheet_id="sheet", export_state_dir=tmp_path / "state")
    memory = GoogleMemory()
    return GoogleExporter(settings, memory.services), memory


def test_export_contains_full_json_and_raw_sheet_values(exporter):
    service, memory = exporter
    result = Result(codigo="PORTAL_BLOQUEADO")
    receipt = service.export(result)
    assert memory.payload == result.model_dump(mode="json", exclude={"origem"})
    assert receipt.arquivo_nome.startswith(result.consulta_id + "_")
    assert receipt.arquivo_nome.endswith(".json")
    assert memory.rows[1][0] == result.consulta_id
    assert memory.rows[1][4] == receipt.arquivo_url
    assert memory.rows[1][5] == "erro"
    assert memory.sheets.spreadsheets().values().append.call_args.kwargs["valueInputOption"] == "RAW"


@pytest.mark.parametrize("failure", ["lose_upload_response", "lose_append_response"])
def test_resume_after_remote_commit_does_not_duplicate(exporter, failure):
    service, memory = exporter
    setattr(memory, failure, True)
    result = Result()
    with pytest.raises(OSError):
        service.export(result)
    receipt = service.export(result)
    assert receipt.linha == 2
    assert memory.created == 1
    assert len(memory.rows) == 2


def test_repeated_export_reuses_file_and_row(exporter):
    service, memory = exporter
    result = Result()
    first = service.export(result)
    assert service.export(result) == first
    assert memory.created == 1
    assert len(memory.rows) == 2


def test_same_id_with_changed_payload_is_rejected(exporter):
    service, memory = exporter
    result = Result()
    service.export(result)
    result.mensagem = "different"
    with pytest.raises(ExportError, match="outro conteúdo"):
        service.export(result)
    assert memory.created == 1


def test_recovery_without_local_journal(exporter, tmp_path):
    service, memory = exporter
    result = Result()
    first = service.export(result)
    service.settings.export_state_dir = tmp_path / "new-state"
    assert service.export(result) == first
    assert memory.created == 1


def test_unconfigured_export_is_explicit():
    with pytest.raises(ExportError) as error:
        GoogleExporter(Settings(google_drive_folder_id="", google_spreadsheet_id="")).export(Result())
    assert error.value.code == "GOOGLE_NAO_CONFIGURADO"
