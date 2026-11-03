"""Servidor MCP em /mcp: listagem da tool, X-API-Key, evidência opcional e mesmo fluxo de /consultas."""
from fastapi.testclient import TestClient

from app.api import create_app
from app.config import Settings
from app.models import Evidence, Person, Result, Snapshot

HEADERS = {"Accept": "application/json, text/event-stream", "X-API-Key": "test-key"}


class StubLocal:
    def __init__(self, settings):
        self.calls = []

    async def start(self):
        pass

    async def close(self):
        pass

    async def query(self, query):
        self.calls.append(query.termo)
        if query.termo == "Ninguem Aqui":
            return Result(modo_execucao="dados_abertos", codigo="NAO_ENCONTRADO", mensagem="Nenhuma pessoa")
        return Result(status="sucesso", modo_execucao="dados_abertos", evidencia=Evidence(base64="iVBORw0KGgo="),
                      pessoa=Person(nome=query.termo.upper(), nis="12345678901",
                                    panorama=Snapshot(url="https://exemplo", texto="ok")))


class MemoryHistory:
    def __init__(self, settings):
        self.saved = {}

    async def start(self):
        pass

    async def close(self):
        pass

    async def find(self, query):
        result = self.saved.get(query.termo.lower())
        return result and result.model_copy(update={"origem": "historico"})

    async def save(self, query, result):
        self.saved[query.termo.lower()] = result


def rpc(client, method, params=None, headers=HEADERS, id=1):
    return client.post("/mcp", json={"jsonrpc": "2.0", "id": id, "method": method, "params": params or {}}, headers=headers)


def call(client, **arguments):
    response = rpc(client, "tools/call", {"name": "consultar_pessoa", "arguments": arguments})
    assert response.status_code == 200, response.text
    return response.json()["result"]["structuredContent"]


def make_client(**extra):
    settings = Settings(_env_file=None, api_key="test-key", database_url="postgresql://stub", **extra)
    return TestClient(create_app(settings, local_factory=StubLocal, history_factory=MemoryHistory))


def test_lists_tool_and_requires_api_key():
    with make_client() as client:
        init = rpc(client, "initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                          "clientInfo": {"name": "teste", "version": "1"}})
        assert init.status_code == 200 and init.json()["result"]["serverInfo"]["name"] == "observa"
        tools = rpc(client, "tools/list").json()["result"]["tools"]
        by_name = {tool["name"]: tool for tool in tools}
        assert list(by_name) == ["consultar_pessoa", "listar_base_local", "pesquisar_base_local", "importar_base_local",
                                 "status_importacao", "cancelar_importacao"]
        assert {"termo", "beneficiario_programa_social", "incluir_evidencia", "atualizar"} <= by_name["consultar_pessoa"]["inputSchema"]["properties"].keys()
        assert {"programa", "ano", "mes", "uf", "limite"} <= by_name["importar_base_local"]["inputSchema"]["properties"].keys()
        assert by_name["importar_base_local"]["inputSchema"]["required"] == ["programa", "ano"]
        assert {"termo", "programa", "arquivo_mes", "uf", "municipio", "contem", "pagina"} <= by_name["pesquisar_base_local"]["inputSchema"]["properties"].keys()
        assert "required" not in by_name["pesquisar_base_local"]["inputSchema"] or not by_name["pesquisar_base_local"]["inputSchema"]["required"]
        assert rpc(client, "tools/list", headers={"Accept": HEADERS["Accept"]}).status_code == 401
        assert rpc(client, "tools/list", headers={**HEADERS, "X-API-Key": "errada"}).status_code == 401
        assert "/mcp" not in client.get("/openapi.json").json()["paths"]


def test_tool_uses_local_base_history_and_hides_evidence():
    with make_client() as client:
        local = client.app.state.dados_abertos
        first = call(client, termo="maria da silva")
        assert (first["status"], first["modo_execucao"], first["origem"]) == ("sucesso", "dados_abertos", "portal")
        assert first["pessoa"]["nome"] == "MARIA DA SILVA" and first["evidencia"] is None
        # Mesmo termo: vem do histórico, sem nova consulta à base.
        again = call(client, termo="Maria da Silva", incluir_evidencia=True)
        assert (again["origem"], again["consulta_id"]) == ("historico", first["consulta_id"])
        assert again["evidencia"]["base64"] == "iVBORw0KGgo="
        assert local.calls == ["maria da silva"]
        # Erros preservam status, código e mensagem.
        missing = call(client, termo="Ninguem Aqui")
        assert (missing["status"], missing["codigo"], missing["mensagem"]) == ("erro", "NAO_ENCONTRADO", "Nenhuma pessoa")
        invalid = call(client, termo="123")
        assert (invalid["status"], invalid["codigo"]) == ("erro", "ENTRADA_INVALIDA")
        assert "11 dígitos" in invalid["mensagem"]


def test_mcp_can_be_disabled():
    with make_client(mcp_enabled=False) as client:
        assert rpc(client, "tools/list").status_code in (404, 405)
