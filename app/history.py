"""Histórico de consultas no PostgreSQL (opcional, ativado por DATABASE_URL)."""
import asyncio
from uuid import UUID

from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool

from app.config import Settings
from app.models import HistoryItem, Query, Result

SCHEMA = """
CREATE TABLE IF NOT EXISTS consultas (
    consulta_id uuid PRIMARY KEY,
    consultado_em timestamptz NOT NULL,
    termo text NOT NULL,
    beneficiario_programa_social boolean NOT NULL DEFAULT false,
    modo_execucao text NOT NULL,
    status text NOT NULL,
    codigo text,
    nome text,
    cpf text,
    nis text,
    duracao_ms integer NOT NULL DEFAULT 0,
    resultado jsonb NOT NULL
);
CREATE INDEX IF NOT EXISTS consultas_consultado_em_idx ON consultas (consultado_em DESC);
"""

INSERT = """
INSERT INTO consultas (consulta_id, consultado_em, termo, beneficiario_programa_social, modo_execucao,
                       status, codigo, nome, cpf, nis, duracao_ms, resultado)
VALUES (%(consulta_id)s, %(consultado_em)s, %(termo)s, %(social)s, %(modo_execucao)s,
        %(status)s, %(codigo)s, %(nome)s, %(cpf)s, %(nis)s, %(duracao_ms)s, %(resultado)s)
ON CONFLICT (consulta_id) DO NOTHING
"""

LIST = """
SELECT consulta_id, consultado_em, termo, beneficiario_programa_social, modo_execucao, status, codigo,
       nome, cpf, nis, duracao_ms, jsonb_array_length(resultado->'beneficios') AS beneficios
FROM consultas
WHERE %(busca)s::text IS NULL OR termo ILIKE %(busca)s OR nome ILIKE %(busca)s
ORDER BY consultado_em DESC
LIMIT %(limite)s
"""


class History:
    """Grava cada resultado e devolve as consultas anteriores."""

    def __init__(self, settings: Settings):
        self.pool = AsyncConnectionPool(settings.database_url.get_secret_value(), min_size=1, max_size=5,
                                        timeout=5, open=False, kwargs={"row_factory": dict_row})
        self._ready = False
        self._lock = asyncio.Lock()

    async def start(self):
        # Não espera o banco: a API sobe mesmo com o Postgres indisponível,
        # e a tabela é criada no primeiro uso bem-sucedido.
        await self.pool.open(wait=False)

    async def close(self):
        await self.pool.close()

    async def _ensure_schema(self):
        if self._ready:
            return
        async with self._lock:
            if not self._ready:
                async with self.pool.connection() as conn:
                    await conn.execute(SCHEMA)
                self._ready = True

    async def save(self, query: Query, result: Result):
        await self._ensure_schema()
        person = result.pessoa
        async with self.pool.connection() as conn:
            await conn.execute(INSERT, {
                "consulta_id": result.consulta_id, "consultado_em": result.consultado_em,
                "termo": query.termo, "social": query.beneficiario_programa_social,
                "modo_execucao": result.modo_execucao, "status": result.status, "codigo": result.codigo,
                "nome": person and person.nome, "cpf": person and person.cpf, "nis": person and person.nis,
                "duracao_ms": result.duracao_ms, "resultado": Jsonb(result.model_dump(mode="json")),
            })

    async def list(self, limit: int = 20, search: str | None = None) -> list[HistoryItem]:
        await self._ensure_schema()
        async with self.pool.connection() as conn:
            cursor = await conn.execute(LIST, {"busca": f"%{search}%" if search else None, "limite": limit})
            return [HistoryItem(**row) for row in await cursor.fetchall()]

    async def get(self, consulta_id: UUID) -> Result | None:
        await self._ensure_schema()
        async with self.pool.connection() as conn:
            cursor = await conn.execute("SELECT resultado FROM consultas WHERE consulta_id = %s", (consulta_id,))
            row = await cursor.fetchone()
        return Result.model_validate(row["resultado"]) if row else None
