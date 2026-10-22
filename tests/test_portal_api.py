import asyncio

import httpx
from fastapi.testclient import TestClient

from app.api import create_app
from app.config import Settings
from app.models import Query, Result
from app.portal_api import PortalApi, flatten

CPF = "12345678901"
PERSON = {"nome": "MARIA DA SILVA", "cpf": "***.456.789-**", "nis": "10987654321",
          "favorecidoBpc": True, "auxilioEmergencial": True, "favorecidoPeti": False}
BPC_ROW = {"id": 1, "valor": 1412.0, "municipio": {"nomeIBGE": "MANAUS", "uf": {"sigla": "AM"}}}


def settings(**extra):
    return Settings(_env_file=None, portal_api_key="gov-key", **extra)


def run(handler, query=CPF, **extra):
    calls = []

    def record(request):
        calls.append(request)
        return handler(request)

    async def go():
        api = PortalApi(settings(**extra), transport=httpx.MockTransport(record))
        api.RETRY_DELAYS = (0, 0)
        await api.start()
        try:
            return await api.query(Query(termo=query))
        finally:
            await api.close()

    return asyncio.run(go()), calls


def portal(request):
    path, params = request.url.path.rsplit("/", 1)[-1], request.url.params
    if path == "pessoa-fisica":
        return httpx.Response(200, json=PERSON if params.get("cpf") == CPF else {})
    if path == "bpc-por-cpf-ou-nis":
        return httpx.Response(200, json=[BPC_ROW] if params["pagina"] == "1" else [])
    if path == "auxilio-emergencial-por-cpf-ou-nis":
        assert params["codigoBeneficiario"] == CPF
        return httpx.Response(200, json=[])
    raise AssertionError(f"endpoint inesperado: {path}")


def test_collects_person_and_flagged_benefits():
    result, calls = run(portal)
    assert result.status == "sucesso" and result.modo_execucao == "api_oficial"
    assert result.pessoa.nome == "MARIA DA SILVA" and result.pessoa.nis == "10987654321"
    assert [b.programa for b in result.beneficios] == ["Auxílio Emergencial", "Benefício de Prestação Continuada (BPC)"]
    bpc = result.beneficios[1]
    assert bpc.completo and len(bpc.paginas) == 1
    assert bpc.paginas[0].tabelas[0][0]["municipio.uf.sigla"] == "AM"
    # A chave vai só no header e nunca aparece nas URLs do resultado.
    assert all(call.headers["chave-api-dados"] == "gov-key" for call in calls)
    assert "gov-key" not in result.model_dump_json()


def test_tries_nis_when_cpf_has_no_record():
    result, calls = run(portal, query="10987654321")
    assert result.codigo == "NAO_ENCONTRADO" and result.status == "erro"
    assert [dict(c.url.params) for c in calls] == [{"cpf": "10987654321"}, {"nis": "10987654321"}]


def test_retries_429_then_reports_limit():
    result, calls = run(lambda request: httpx.Response(429))
    assert result.codigo == "LIMITE_API_EXCEDIDO" and result.status == "erro"
    assert len(calls) == 3


def test_limit_during_benefits_keeps_partial_result():
    def handler(request):
        if request.url.path.endswith("pessoa-fisica"):
            return httpx.Response(200, json=PERSON)
        return httpx.Response(429)

    result, _ = run(handler)
    assert result.status == "parcial" and result.codigo == "LIMITE_API_EXCEDIDO"
    assert not result.beneficios[0].completo and result.beneficios[0].erro


def test_rejected_key():
    result, _ = run(lambda request: httpx.Response(401))
    assert result.codigo == "CHAVE_PORTAL_INVALIDA"


def test_page_limit_marks_benefit_incomplete():
    def handler(request):
        if request.url.path.endswith("pessoa-fisica"):
            return httpx.Response(200, json={"nome": "X", "favorecidoBpc": True})
        return httpx.Response(200, json=[BPC_ROW])

    result, _ = run(handler, max_detail_pages=2)
    assert result.status == "sucesso"
    assert len(result.beneficios[0].paginas) == 2 and not result.beneficios[0].completo


def test_flatten():
    assert flatten({"a": {"b": 1}, "c": None, "d": [1, 2]}) == {"a.b": "1", "c": "", "d": "1; 2"}


class StubRobot:
    def __init__(self, settings):
        pass

    async def start(self):
        pass

    async def close(self):
        pass

    async def query(self, query):
        return Result(codigo="PORTAL_BLOQUEADO", mensagem="robô")


class StubPortalApi(StubRobot):
    async def query(self, query):
        return Result(modo_execucao="api_oficial", codigo="LIMITE_API_EXCEDIDO", mensagem="limite")


def test_api_routes_documents_to_official_api():
    with TestClient(create_app(settings(), StubRobot, portal_api_factory=StubPortalApi)) as client:
        response = client.post("/consultas", json={"termo": "123.456.789-01"})
        assert response.status_code == 429 and response.json()["modo_execucao"] == "api_oficial"
        response = client.post("/consultas", json={"termo": "Maria"})
        assert response.status_code == 502 and response.json()["codigo"] == "PORTAL_BLOQUEADO"


def test_without_key_documents_use_robot():
    with TestClient(create_app(Settings(_env_file=None), StubRobot, portal_api_factory=StubPortalApi)) as client:
        assert client.post("/consultas", json={"termo": CPF}).json()["codigo"] == "PORTAL_BLOQUEADO"
