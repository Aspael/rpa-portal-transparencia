"""Histórico de consultas no PostgreSQL com SQLAlchemy (ORM), ativado por DATABASE_URL.

Só consultas com sucesso são gravadas; uma nova consulta pelo mesmo termo
reaproveita o resultado gravado enquanto ele estiver dentro de HISTORY_CACHE_HOURS.
"""
import asyncio
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import UUID

from sqlalchemy import Boolean, DateTime, Integer, Text, func, or_, select
from sqlalchemy.dialects.postgresql import JSONB, UUID as PgUUID, insert
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from app.config import Settings
from app.models import HistoryItem, Query, Result


class Base(DeclarativeBase):
    pass


class Consulta(Base):
    """Uma consulta bem-sucedida: colunas para filtro e o JSON completo em `resultado`."""
    __tablename__ = "consultas"

    consulta_id: Mapped[UUID] = mapped_column(PgUUID(as_uuid=True), primary_key=True)
    consultado_em: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    termo: Mapped[str] = mapped_column(Text, index=True)
    beneficiario_programa_social: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    modo_execucao: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text)
    codigo: Mapped[str | None] = mapped_column(Text)
    nome: Mapped[str | None] = mapped_column(Text)
    cpf: Mapped[str | None] = mapped_column(Text)
    nis: Mapped[str | None] = mapped_column(Text)
    duracao_ms: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    resultado: Mapped[dict[str, Any]] = mapped_column(JSONB)


def async_url(url: str) -> str:
    # postgresql://... vira postgresql+psycopg://... (driver psycopg 3, assíncrono).
    for prefix in ("postgresql://", "postgres://"):
        if url.startswith(prefix):
            return "postgresql+psycopg://" + url[len(prefix):]
    return url


class History:
    """Grava resultados com sucesso e devolve as consultas anteriores."""

    def __init__(self, settings: Settings):
        self.cache_hours = settings.history_cache_hours
        self.engine: AsyncEngine = create_async_engine(
            async_url(settings.database_url.get_secret_value()),
            pool_size=5, max_overflow=0, pool_timeout=5, pool_pre_ping=True,
            connect_args={"connect_timeout": 5},
        )
        self.session = async_sessionmaker(self.engine, expire_on_commit=False)
        self._ready = False
        self._lock = asyncio.Lock()

    async def start(self):
        # Não conecta agora: a API sobe mesmo com o Postgres indisponível,
        # e a tabela é criada no primeiro uso bem-sucedido.
        pass

    async def close(self):
        await self.engine.dispose()

    async def _ensure_schema(self):
        if self._ready:
            return
        async with self._lock:
            if not self._ready:
                async with self.engine.begin() as conn:
                    await conn.run_sync(Base.metadata.create_all)
                self._ready = True

    async def save(self, query: Query, result: Result):
        """INSERT da consulta (a API chama só para status "sucesso")."""
        await self._ensure_schema()
        person = result.pessoa
        values = {
            "consulta_id": UUID(result.consulta_id), "consultado_em": result.consultado_em,
            "termo": query.termo, "beneficiario_programa_social": query.beneficiario_programa_social,
            "modo_execucao": result.modo_execucao, "status": result.status, "codigo": result.codigo,
            "nome": person and person.nome, "cpf": person and person.cpf, "nis": person and person.nis,
            "duracao_ms": result.duracao_ms, "resultado": result.model_dump(mode="json", exclude={"origem"}),
        }
        async with self.session.begin() as session:
            # Repetir o mesmo consulta_id não duplica a linha.
            await session.execute(insert(Consulta).values(values).on_conflict_do_nothing(index_elements=["consulta_id"]))

    async def find(self, query: Query) -> Result | None:
        """SELECT do resultado mais recente para o mesmo termo, se ainda estiver no prazo."""
        if self.cache_hours <= 0:
            return None
        await self._ensure_schema()
        since = datetime.now(timezone.utc) - timedelta(hours=self.cache_hours)
        stmt = (select(Consulta.resultado)
                .where(func.lower(Consulta.termo) == query.termo.lower(),
                       Consulta.beneficiario_programa_social == query.beneficiario_programa_social,
                       Consulta.status == "sucesso", Consulta.consultado_em >= since)
                .order_by(Consulta.consultado_em.desc()).limit(1))
        async with self.session() as session:
            data = await session.scalar(stmt)
        return Result.model_validate(data).model_copy(update={"origem": "historico"}) if data else None

    async def list(self, limit: int = 20, search: str | None = None) -> list[HistoryItem]:
        await self._ensure_schema()
        stmt = select(
            Consulta.consulta_id, Consulta.consultado_em, Consulta.termo, Consulta.beneficiario_programa_social,
            Consulta.modo_execucao, Consulta.status, Consulta.codigo, Consulta.nome, Consulta.cpf, Consulta.nis,
            Consulta.duracao_ms,
            func.coalesce(func.jsonb_array_length(Consulta.resultado["beneficios"]), 0).label("beneficios"),
        ).order_by(Consulta.consultado_em.desc()).limit(limit)
        if search:
            pattern = f"%{search}%"
            stmt = stmt.where(or_(Consulta.termo.ilike(pattern), Consulta.nome.ilike(pattern)))
        async with self.session() as session:
            rows = (await session.execute(stmt)).mappings().all()
        return [HistoryItem(**row) for row in rows]

    async def get(self, consulta_id: UUID | str) -> Result | None:
        await self._ensure_schema()
        async with self.session() as session:
            row = await session.get(Consulta, UUID(str(consulta_id)))
        return Result.model_validate(row.resultado) if row else None
