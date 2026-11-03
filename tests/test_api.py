from fastapi.testclient import TestClient

from app.api import create_app
from app.config import Settings
from app.models import HistoryItem, Result
import pytest
from uuid import uuid4


class StubRobot:
    def __init__(self, settings):
        pass

    async def start(self):
        pass

    async def close(self):
        pass

    async def query(self, query):
        return Result(codigo="NAO_ENCONTRADO", mensagem="Nenhum registro")


def test_validation_auth_and_status():
    with TestClient(create_app(Settings(api_key="test-key"), StubRobot)) as client:
        assert client.get("/health").status_code == 200
        assert client.post("/consultas", json={"termo": "Teste"}).status_code == 401
        headers = {"X-API-Key": "test-key"}
        assert client.post("/consultas", json={"termo": ""}, headers=headers).status_code == 422
        response = client.post("/consultas", json={"termo": "Teste"}, headers=headers)
        assert response.status_code == 404
        assert response.json()["codigo"] == "NAO_ENCONTRADO"
        assert response.headers["Cache-Control"] == "no-store"
        assert "/consultas" in client.get("/openapi.json").json()["paths"]
        assert client.get("/").status_code == 200
        assert "observa" in client.get("/").text
        assert client.get("/static/app.js").status_code == 200
        assert client.get("/integracoes/google/status", headers=headers).json() == {"configurado": False}
        assert client.post("/integracoes/google/arquivar", json=Result().model_dump(mode="json")).status_code == 401
        response = client.post("/integracoes/google/arquivar", json=Result().model_dump(mode="json"), headers=headers)
        assert response.status_code == 503
        assert response.json()["codigo"] == "GOOGLE_NAO_CONFIGURADO"
        assert client.post("/integracoes/google/arquivar", json={}, headers=headers).status_code == 422


def test_front_uses_env_api_key_unless_disabled():
    with TestClient(create_app(Settings(api_key="env-key"), StubRobot)) as client:
        assert '<meta name="api-key" content="env-key">' in client.get("/").text
    with TestClient(create_app(Settings(api_key="env-key", front_api_key_from_env=False), StubRobot)) as client:
        page = client.get("/").text
        assert '<meta name="api-key" content="">' in page and "env-key" not in page


def test_deployment_cannot_start_without_required_key():
    with pytest.raises(RuntimeError, match="API_KEY"):
        with TestClient(create_app(Settings(require_api_key=True, api_key=None), StubRobot)):
            pass


class StubHistory:
    def __init__(self, settings):
        self.saved = []

    async def start(self):
        pass

    async def close(self):
        pass

    async def save(self, query, result):
        self.saved.append((query, result))

    async def find(self, query):
        return next((result.model_copy(update={"origem": "historico"}) for saved, result in reversed(self.saved)
                     if (saved.termo.lower(), saved.beneficiario_programa_social) == (query.termo.lower(), query.beneficiario_programa_social)),
                    None)

    async def list(self, limit, search=None):
        return [HistoryItem(consulta_id=result.consulta_id, consultado_em=result.consultado_em, termo=query.termo,
                            modo_execucao=result.modo_execucao, status=result.status, codigo=result.codigo)
                for query, result in reversed(self.saved) if not search or search.lower() in query.termo.lower()][:limit]

    async def get(self, consulta_id):
        return next((result for _, result in self.saved if result.consulta_id == str(consulta_id)), None)


class SuccessRobot(StubRobot):
    """Sucesso para qualquer termo, exceto "Inexistente"; conta as idas ao portal."""
    def __init__(self, settings):
        self.calls = []

    async def query(self, query):
        self.calls.append(query.termo)
        if query.termo == "Inexistente":
            return Result(codigo="NAO_ENCONTRADO", mensagem="Nenhum registro")
        return Result(status="sucesso", mensagem=f"Dados de {query.termo}")


def test_history_saves_lists_and_reopens_queries():
    settings = Settings(api_key="test-key", database_url="postgresql://stub")
    with TestClient(create_app(settings, SuccessRobot, history_factory=StubHistory)) as client:
        headers = {"X-API-Key": "test-key"}
        assert client.get("/consultas").status_code == 401
        consulta_id = client.post("/consultas", json={"termo": "Teste"}, headers=headers).json()["consulta_id"]
        client.post("/consultas", json={"termo": "Outra Pessoa"}, headers=headers)
        items = client.get("/consultas", headers=headers).json()
        assert [item["termo"] for item in items] == ["Outra Pessoa", "Teste"]
        assert [item["termo"] for item in client.get("/consultas?busca=tes", headers=headers).json()] == ["Teste"]
        assert client.get("/consultas?limite=0", headers=headers).status_code == 422
        response = client.get(f"/consultas/{consulta_id}", headers=headers)
        assert response.status_code == 200
        assert response.json()["mensagem"] == "Dados de Teste"
        assert client.get(f"/consultas/{uuid4()}", headers=headers).status_code == 404
        assert client.get("/consultas/nao-e-uuid", headers=headers).status_code == 422


def test_success_is_inserted_and_reused_from_history():
    settings = Settings(database_url="postgresql://stub")
    with TestClient(create_app(settings, SuccessRobot, history_factory=StubHistory)) as client:
        robot, store = client.app.state.robot, client.app.state.history
        first = client.post("/consultas", json={"termo": "Maria"}).json()
        assert first["origem"] == "portal"

        # Mesmo termo: SELECT no histórico, sem nova ida ao portal.
        again = client.post("/consultas", json={"termo": "  maria "}).json()
        assert (again["origem"], again["consulta_id"]) == ("historico", first["consulta_id"])
        assert robot.calls == ["Maria"]

        # Filtro diferente ou "atualizar" vão ao portal e gravam um novo resultado.
        client.post("/consultas", json={"termo": "Maria", "beneficiario_programa_social": True})
        refreshed = client.post("/consultas", json={"termo": "Maria", "atualizar": True}).json()
        assert refreshed["origem"] == "portal" and refreshed["consulta_id"] != first["consulta_id"]
        assert robot.calls == ["Maria"] * 3

        # Falha não é gravada, então a próxima tentativa volta ao portal.
        assert client.post("/consultas", json={"termo": "Inexistente"}).status_code == 404
        assert client.post("/consultas", json={"termo": "Inexistente"}).status_code == 404
        assert robot.calls[-2:] == ["Inexistente", "Inexistente"]
        assert all(result.status == "sucesso" for _, result in store.saved) and len(store.saved) == 3


def test_history_disabled_and_database_failure():
    with TestClient(create_app(Settings(database_url=None), StubRobot)) as client:
        response = client.get("/consultas")
        assert response.status_code == 503
        assert response.json()["codigo"] == "HISTORICO_DESATIVADO"

    class BrokenHistory(StubHistory):
        async def find(self, query):
            raise ConnectionError("banco fora do ar")

        async def save(self, query, result):
            raise ConnectionError("banco fora do ar")

        async def list(self, limit, search=None):
            raise ConnectionError("banco fora do ar")

    with TestClient(create_app(Settings(database_url="postgresql://stub"), StubRobot, history_factory=BrokenHistory)) as client:
        # A consulta continua respondendo mesmo sem banco.
        assert client.post("/consultas", json={"termo": "Teste"}).json()["codigo"] == "NAO_ENCONTRADO"
        assert client.get("/consultas").json()["codigo"] == "HISTORICO_INDISPONIVEL"
