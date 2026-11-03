"""Base local de dados abertos: parsing dos CSVs, rota da API e integração com PostgreSQL."""
import asyncio
import os
import zipfile
from uuid import uuid4

import psycopg
import pytest
from fastapi.testclient import TestClient

from app.api import create_app
from app.config import Settings
from app.dados_abertos import DadosAbertos, masked_cpf, normalize_name
from app.importar_dados import column_map, import_file, parse_row, plain_url
from app.models import Query, Result

HEADER = ["MÊS COMPETÊNCIA", "MÊS REFERÊNCIA", "UF", "CÓDIGO MUNICÍPIO SIAFI", "NOME MUNICÍPIO",
          "CPF FAVORECIDO", "NIS FAVORECIDO", "NOME FAVORECIDO", "VALOR PARCELA"]


def test_parsing_matches_the_official_layout():
    columns = column_map(HEADER)
    row = parse_row(["202501", "202312", "AM", "0255", "MANAUS", "***.085.106-**", "20643890445", "Joana  Conceição", "1.650,00"], columns)
    assert row == {"mes_competencia": "202501", "mes_referencia": "202312", "uf": "AM", "municipio": "MANAUS",
                   "cpf": "***.085.106-**", "nis": "20643890445", "nome": "Joana  Conceição", "valor": "1650.00",
                   "nome_busca": "JOANA CONCEICAO"}
    # NIS zerado vira ausente; titular mascarado (menor de 16 anos) é ignorado.
    assert parse_row(["202501", "", "AM", "", "MANAUS", "", "00000000000", "ANA", "10,00"], columns)["nis"] is None
    assert parse_row(["202501", "", "", "", "", "", "", "*** TITULAR MENOR DE 16 ANOS ***", "1518,00"], columns) is None
    # Auxílio Emergencial usa outros nomes de coluna.
    emergencial = column_map(["MÊS DISPONIBILIZAÇÃO", "UF", "NOME MUNICÍPIO", "NIS BENEFICIÁRIO", "CPF BENEFICIÁRIO",
                              "NOME BENEFICIÁRIO", "VALOR BENEFÍCIO"])
    assert {"mes_competencia", "nis", "cpf", "nome", "valor"} <= emergencial.keys()
    with pytest.raises(ValueError, match="não reconhecido"):
        column_map(["A", "B"])


def test_helpers():
    assert masked_cpf("12345678901") == "***.456.789-**"
    assert normalize_name("  joão   da silva ") == "JOAO DA SILVA"
    assert plain_url("postgresql+psycopg://u:p@h/db") == "postgresql://u:p@h/db"
    assert plain_url("postgres://u:p@h/db") == "postgresql://u:p@h/db"


class StubLocal:
    def __init__(self, settings):
        self.calls = []

    async def start(self):
        pass

    async def close(self):
        pass

    async def query(self, query):
        self.calls.append(query.termo)
        return Result(status="sucesso", modo_execucao="dados_abertos")


class NoHistory(StubLocal):
    async def find(self, query):
        return None

    async def save(self, query, result):
        pass


class ExplodingRobot:
    def __init__(self, settings):
        raise AssertionError("o robô não deve ser criado no modo dados_abertos")


def test_api_uses_local_base_and_never_starts_the_robot():
    settings = Settings(_env_file=None, query_source="dados_abertos", database_url="postgresql://stub")
    with TestClient(create_app(settings, ExplodingRobot, local_factory=StubLocal,
                               history_factory=NoHistory)) as client:
        assert client.post("/consultas", json={"termo": "Maria"}).json()["modo_execucao"] == "dados_abertos"
        assert client.app.state.dados_abertos.calls == ["Maria"]
        assert client.get("/").status_code == 200
    with TestClient(create_app(Settings(_env_file=None, query_source="dados_abertos"), ExplodingRobot)) as client:
        response = client.post("/consultas", json={"termo": "Maria"})
        assert (response.status_code, response.json()["codigo"]) == (503, "BASE_LOCAL_DESATIVADA")


DATABASE_URL = os.environ.get("TEST_DATABASE_URL")


@pytest.mark.skipif(not DATABASE_URL, reason="TEST_DATABASE_URL não definido")
def test_import_and_query_against_postgres(tmp_path):
    marker = uuid4().hex[:6].upper()
    nis = str(uuid4().int)[:11]
    rows = [
        HEADER,
        ["209901", "209812", "AM", "0255", "MANAUS", "***.111.222-**", nis, f"MARIA {marker}", "600,00"],
        ["209901", "209811", "AM", "0255", "MANAUS", "***.111.222-**", nis, f"MARIA {marker}", "650,00"],
        ["209901", "209901", "SP", "7107", "SAO PAULO", "***.333.444-**", "", f"JOSE {marker}", "700,00"],
    ]
    path = tmp_path / "teste.zip"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("teste.csv", "\n".join(";".join(f'"{c}"' for c in row) for row in rows).encode("latin-1"))
    month = f"2099{int(marker[:2], 16) % 12 + 1:02d}"
    assert import_file(DATABASE_URL, "novo-bolsa-familia", month, path, "AM", None) == 2
    # Reimportar substitui em vez de duplicar.
    assert import_file(DATABASE_URL, "novo-bolsa-familia", month, path, "AM", None) == 2

    async def scenario():
        local = DadosAbertos(Settings(_env_file=None, database_url=DATABASE_URL))
        try:
            by_name = await local.query(Query(termo=f"maria {marker.lower()}"))
            assert by_name.status == "sucesso", by_name.mensagem
            assert (by_name.pessoa.nis, by_name.pessoa.localidade) == (nis, "MANAUS/AM")
            assert by_name.beneficios[0].programa == "Novo Bolsa Família"
            assert len(by_name.beneficios[0].paginas[0].tabelas[0]) == 2
            assert by_name.pessoa.panorama.campos["Valor total"] == "R$ 1.250,00"
            assert (await local.query(Query(termo=nis))).pessoa.nome == f"MARIA {marker}"
            # SP ficou fora do --uf AM.
            assert (await local.query(Query(termo=f"JOSE {marker}"))).codigo == "NAO_ENCONTRADO"
        finally:
            await local.close()
    try:
        asyncio.run(scenario())
    finally:
        with psycopg.connect(plain_url(DATABASE_URL)) as conn:
            conn.execute("DELETE FROM pagamentos WHERE programa = %s AND arquivo_mes = %s", ("novo-bolsa-familia", month))
            conn.execute("DELETE FROM importacoes WHERE programa = %s AND arquivo_mes = %s", ("novo-bolsa-familia", month))
