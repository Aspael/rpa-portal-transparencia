"""Integração com PostgreSQL real; roda quando TEST_DATABASE_URL está definido (CI)."""
import asyncio
import os
from uuid import uuid4

import pytest

from app.config import Settings
from app.history import History
from app.models import Benefit, Person, Query, Result, Snapshot

DATABASE_URL = os.environ.get("TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not DATABASE_URL, reason="TEST_DATABASE_URL não definido")


def test_save_list_and_get_roundtrip():
    async def scenario():
        history = History(Settings(database_url=DATABASE_URL))
        await history.start()
        try:
            marker = uuid4().hex[:8]
            person = Person(nome=f"Pessoa {marker}", cpf="***.456.789-**", panorama=Snapshot(url="https://exemplo", texto="ok"))
            result = Result(status="sucesso", modo_execucao="api_oficial", pessoa=person,
                            beneficios=[Benefit(programa="Bolsa Família", url="https://exemplo")])
            await history.save(Query(termo="123.456.789-01"), result)
            await history.save(Query(termo="123.456.789-01"), result)  # repetido não duplica

            items = await history.list(10, marker)
            assert [(item.consulta_id, item.termo, item.nome, item.beneficios) for item in items] == \
                [(result.consulta_id, "12345678901", person.nome, 1)]
            assert await history.list(10, f"inexistente-{marker}") == []
            assert (await history.get(result.consulta_id)) == result
            assert await history.get(uuid4()) is None
        finally:
            await history.close()
    asyncio.run(scenario())
