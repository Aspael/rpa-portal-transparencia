"""Base local pela API e pelo MCP: pedido por ano/mês, tarefa em segundo plano, progresso e cancelamento."""
import threading
import time
from datetime import date, datetime, timezone

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app import base_local
from app.api import create_app
from app.base_local import BaseLocal, PedidoImportacao
from app.config import Settings
from app.importar_dados import ImportacaoErro, check_cancel

HEADERS = {"X-API-Key": "test-key"}
MCP_HEADERS = {"Accept": "application/json, text/event-stream", **HEADERS}


class Stub:
    def __init__(self, settings):
        pass

    async def start(self):
        pass

    async def close(self):
        pass

    async def find(self, query):
        return None


class FakeImports:
    """Substitui o PostgreSQL e o download: grava em memória o que o runner importou."""

    def __init__(self):
        self.rows: dict[tuple[str, str], dict] = {}
        self.calls: list[str] = []
        self.release = threading.Event()
        self.release.set()

    def runner(self, settings, programa, mes, uf, limit, force, progress, cancel):
        self.calls.append(mes)
        progress("Baixando https://portal/arquivo", 0)
        progress("  50% (10 MB)", 50)
        while not self.release.wait(0.01):
            check_cancel(cancel)
        check_cancel(cancel)
        if mes.endswith("02"):
            raise ImportacaoErro(f"Arquivo não encontrado no portal: {programa} {mes}.")
        progress("Importando...", None)
        self.rows[(programa, mes)] = {"programa": programa, "programa_nome": "Novo Bolsa Família", "arquivo_mes": mes,
                                      "uf": uf, "linhas": 3, "importado_em": datetime.now(timezone.utc)}
        return 3

    def fetch(self, url):
        return sorted(self.rows.values(), key=lambda r: r["arquivo_mes"], reverse=True)

    def remove(self, url, programa, mes):
        return self.rows.pop((programa, mes), None) is not None


@pytest.fixture
def fake(monkeypatch):
    fake = FakeImports()
    monkeypatch.setattr(base_local.importar_dados, "fetch_imports", fake.fetch)
    monkeypatch.setattr(base_local.importar_dados, "remove_import", fake.remove)
    return fake


def make_client(fake, **extra):
    settings = Settings(_env_file=None, api_key="test-key", database_url="postgresql://stub", **extra)
    return TestClient(create_app(settings, local_factory=Stub, history_factory=Stub,
                                 base_local_factory=lambda s: BaseLocal(s, runner=fake.runner)))


def wait_task(client, tarefa_id, timeout=5):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        task = client.get(f"/base-local/tarefas/{tarefa_id}", headers=HEADERS).json()
        if task["status"] not in ("na_fila", "em_andamento"):
            return task
        time.sleep(0.02)
    raise AssertionError(f"tarefa não terminou: {task}")


def test_request_validation():
    assert PedidoImportacao(programa="bpc", ano=2024, mes=3, uf=" am ").meses() == ["202403"]
    assert len(PedidoImportacao(programa="bpc", ano=2024).meses()) == 12
    # Ano atual: só até o mês corrente.
    assert len(PedidoImportacao(programa="bpc", ano=date.today().year).meses()) == date.today().month
    assert PedidoImportacao(programa="bpc", ano=2024, uf="am").uf == "AM"
    for bad in ({"programa": "outro", "ano": 2024}, {"programa": "bpc", "ano": 2010},
                {"programa": "bpc", "ano": 2024, "mes": 13}, {"programa": "bpc", "ano": 2024, "uf": "XX"},
                {"programa": "bpc", "ano": date.today().year + 1}):
        with pytest.raises(ValidationError):
            PedidoImportacao(**bad)


def test_api_imports_month_lists_and_removes(fake):
    with make_client(fake) as client:
        assert client.get("/base-local").status_code == 401
        summary = client.get("/base-local", headers=HEADERS).json()
        assert summary["importados"] == [] and summary["tarefa_atual"] is None
        assert "novo-bolsa-familia" in [p["programa"] for p in summary["programas"]]

        response = client.post("/base-local/importacoes", headers=HEADERS,
                               json={"programa": "novo-bolsa-familia", "ano": 2025, "mes": 1, "uf": "am"})
        assert response.status_code == 202, response.text
        task = wait_task(client, response.json()["tarefa_id"])
        assert (task["status"], task["linhas"], task["uf"]) == ("concluida", 3, "AM")
        assert task["meses"] == [{"mes": "202501", "status": "concluido", "linhas": 3, "mensagem": "3 linhas importadas"}]

        imported = client.get("/base-local", headers=HEADERS).json()["importados"]
        assert [(i["programa"], i["arquivo_mes"]) for i in imported] == [("novo-bolsa-familia", "202501")]
        assert client.delete("/base-local/importacoes/novo-bolsa-familia/202501", headers=HEADERS).json()["removido"]
        assert client.delete("/base-local/importacoes/novo-bolsa-familia/202501", headers=HEADERS).status_code == 404
        assert client.delete("/base-local/importacoes/outro/202501", headers=HEADERS).status_code == 422
        assert client.post("/base-local/importacoes", headers=HEADERS, json={"programa": "x", "ano": 2025}).status_code == 422
        assert "/base-local/importacoes" in client.get("/openapi.json").json()["paths"]


def test_whole_year_reports_failed_months(fake):
    with make_client(fake) as client:
        response = client.post("/base-local/importacoes", headers=HEADERS, json={"programa": "bpc", "ano": 2024})
        task = wait_task(client, response.json()["tarefa_id"])
        assert len(fake.calls) == 12 and task["status"] == "parcial"
        failed = [m for m in task["meses"] if m["status"] == "erro"]
        assert [m["mes"] for m in failed] == ["202402"] and "não encontrado" in failed[0]["mensagem"]
        assert task["linhas"] == 33 and task["mensagem"].startswith("11 de 12")


def test_one_import_at_a_time_and_cancel(fake):
    fake.release.clear()
    with make_client(fake) as client:
        first = client.post("/base-local/importacoes", headers=HEADERS, json={"programa": "bpc", "ano": 2024}).json()
        busy = client.post("/base-local/importacoes", headers=HEADERS, json={"programa": "bpc", "ano": 2023, "mes": 1})
        assert busy.status_code == 409 and busy.json()["codigo"] == "IMPORTACAO_EM_ANDAMENTO"
        deadline = time.monotonic() + 5
        while (running := client.get("/base-local", headers=HEADERS).json()["tarefa_atual"])["percentual"] != 50:
            assert time.monotonic() < deadline
            time.sleep(0.01)
        assert running["tarefa_id"] == first["tarefa_id"] and running["mes_atual"] == "202401"
        assert running["percentual"] == 50
        assert client.post(f"/base-local/tarefas/{first['tarefa_id']}/cancelar", headers=HEADERS).status_code == 200
        task = wait_task(client, first["tarefa_id"])
        assert task["status"] == "cancelada" and {m["status"] for m in task["meses"]} == {"cancelado"}
        assert fake.calls == ["202401"] and fake.rows == {}
        assert client.get("/base-local/tarefas/inexistente", headers=HEADERS).status_code == 404


def test_disabled_without_database():
    settings = Settings(_env_file=None, query_source="site", api_key="test-key")

    class Robot(Stub):
        pass

    with TestClient(create_app(settings, Robot)) as client:
        response = client.get("/base-local", headers=HEADERS)
        assert (response.status_code, response.json()["codigo"]) == (503, "BASE_LOCAL_DESATIVADA")


def mcp_call(client, name, **arguments):
    response = client.post("/mcp", headers=MCP_HEADERS, json={"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                                                "params": {"name": name, "arguments": arguments}})
    assert response.status_code == 200, response.text
    return response.json()["result"]["structuredContent"]


def test_mcp_tools_import_and_follow_progress(fake):
    with make_client(fake) as client:
        assert mcp_call(client, "listar_base_local")["importados"] == []
        invalid = mcp_call(client, "importar_base_local", programa="bpc", ano=2024, uf="ZZ")
        assert (invalid["status"], invalid["codigo"]) == ("erro", "ENTRADA_INVALIDA")
        started = mcp_call(client, "importar_base_local", programa="novo-bolsa-familia", ano=2025, mes=1, uf="AM")
        assert started["status"] == "sucesso"
        tarefa_id = started["tarefa"]["tarefa_id"]
        wait_task(client, tarefa_id)
        status = mcp_call(client, "status_importacao", tarefa_id=tarefa_id)
        assert status["tarefa"]["status"] == "concluida"
        # Sem ID: a mais recente.
        assert mcp_call(client, "status_importacao")["tarefa"]["tarefa_id"] == tarefa_id
        listed = mcp_call(client, "listar_base_local")
        assert [i["arquivo_mes"] for i in listed["importados"]] == ["202501"]
        assert mcp_call(client, "cancelar_importacao", tarefa_id="nada")["codigo"] == "TAREFA_NAO_ENCONTRADA"


def test_mcp_base_tools_without_database():
    settings = Settings(_env_file=None, query_source="dados_abertos", api_key="test-key")
    with TestClient(create_app(settings, local_factory=Stub)) as client:
        assert mcp_call(client, "listar_base_local")["codigo"] == "BASE_LOCAL_DESATIVADA"
        assert mcp_call(client, "importar_base_local", programa="bpc", ano=2024)["codigo"] == "BASE_LOCAL_DESATIVADA"
