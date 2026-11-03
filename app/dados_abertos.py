"""Consulta local nos dados abertos do Portal da Transparência, importados no PostgreSQL.

Não acessa site nem API do governo durante a consulta: os arquivos mensais
(CSV dentro de ZIP) são baixados e importados antes, com ``python -m app.importar_dados``.
Nesses arquivos o CPF vem mascarado (***.123.456-**); NIS e nome vêm completos.
"""
import asyncio
import logging
import unicodedata
from collections import defaultdict
from datetime import datetime
from decimal import Decimal
from time import monotonic

from sqlalchemy import BigInteger, DateTime, Identity, Index, Numeric, Text, func, select
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine
from sqlalchemy.orm import Mapped, mapped_column

from app.config import Settings
from app.history import Base, async_url
from app.models import Benefit, Person, Query, Result, Snapshot

log = logging.getLogger(__name__)

DOWNLOAD_PAGE = "https://portaldatransparencia.gov.br/download-de-dados"

# slug usado na URL de download -> nome exibido
PROGRAMAS = {
    "novo-bolsa-familia": "Novo Bolsa Família",
    "bolsa-familia-pagamentos": "Bolsa Família",
    "auxilio-brasil": "Auxílio Brasil",
    "auxilio-emergencial": "Auxílio Emergencial",
    "bpc": "Benefício de Prestação Continuada (BPC)",
}

# Teto de parcelas devolvidas por consulta, para o JSON não crescer sem limite.
MAX_LINHAS = 5000


def download_url(programa: str, mes: str) -> str:
    return f"{DOWNLOAD_PAGE}/{programa}/{mes}"


def normalize_name(value: str) -> str:
    """Maiúsculas, sem acento e com espaços simples: a forma usada na busca por nome."""
    value = " ".join(value.split()).upper()
    if value.isascii():
        return value
    return "".join(c for c in unicodedata.normalize("NFD", value) if unicodedata.category(c) != "Mn")


def masked_cpf(cpf: str) -> str:
    """CPF no formato publicado nos dados abertos: só os 6 dígitos do meio aparecem."""
    return f"***.{cpf[3:6]}.{cpf[6:9]}-**"


def brl(value: Decimal) -> str:
    return "R$ " + f"{value:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")


class Pagamento(Base):
    """Uma parcela paga a um beneficiário, como está no arquivo mensal."""
    __tablename__ = "pagamentos"
    __table_args__ = (
        Index("ix_pagamentos_nis", "nis"),
        Index("ix_pagamentos_cpf", "cpf"),
        Index("ix_pagamentos_nome_busca", "nome_busca"),
        Index("ix_pagamentos_arquivo", "programa", "arquivo_mes"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    programa: Mapped[str] = mapped_column(Text)
    arquivo_mes: Mapped[str] = mapped_column(Text)
    mes_competencia: Mapped[str | None] = mapped_column(Text)
    mes_referencia: Mapped[str | None] = mapped_column(Text)
    uf: Mapped[str | None] = mapped_column(Text)
    municipio: Mapped[str | None] = mapped_column(Text)
    cpf: Mapped[str | None] = mapped_column(Text)
    nis: Mapped[str | None] = mapped_column(Text)
    nome: Mapped[str] = mapped_column(Text)
    nome_busca: Mapped[str] = mapped_column(Text)
    valor: Mapped[Decimal | None] = mapped_column(Numeric(14, 2))


class Importacao(Base):
    """Arquivo mensal já importado; reimportar o mesmo programa/mês substitui as linhas."""
    __tablename__ = "importacoes"

    programa: Mapped[str] = mapped_column(Text, primary_key=True)
    arquivo_mes: Mapped[str] = mapped_column(Text, primary_key=True)
    uf: Mapped[str | None] = mapped_column(Text)
    linhas: Mapped[int] = mapped_column(BigInteger)
    importado_em: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class LocalError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


class DadosAbertos:
    """Fonte de consultas com a mesma interface do robô: ``query(Query) -> Result``."""

    def __init__(self, settings: Settings):
        self.settings = settings
        self.engine: AsyncEngine = create_async_engine(
            async_url(settings.database_url.get_secret_value()),
            pool_size=5, max_overflow=0, pool_timeout=5, pool_pre_ping=True,
            connect_args={"connect_timeout": 5},
        )
        self.session = async_sessionmaker(self.engine, expire_on_commit=False)
        self._ready = False
        self._lock = asyncio.Lock()

    async def start(self):
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

    async def query(self, query: Query) -> Result:
        result = Result(modo_execucao="dados_abertos")
        started = monotonic()
        try:
            async with asyncio.timeout(self.settings.query_timeout_seconds):
                await self._ensure_schema()
                async with self.session() as session:
                    await self._collect(session, query, result)
        except LocalError as exc:
            result.codigo, result.mensagem = exc.code, str(exc)
        except TimeoutError:
            result.codigo, result.mensagem = "TEMPO_ESGOTADO", "A consulta na base local passou do tempo limite"
        except Exception:
            log.exception("Falha ao consultar a base local de dados abertos")
            result.codigo, result.mensagem = "BASE_LOCAL_INDISPONIVEL", "Não foi possível acessar a base local no PostgreSQL"
        finally:
            result.duracao_ms = round((monotonic() - started) * 1000)
        if result.codigo:
            result.status = "erro"
        return result

    async def _collect(self, session, query: Query, result: Result):
        result.etapa = "consultando_base_local"
        imports = (await session.execute(select(Importacao).order_by(Importacao.programa, Importacao.arquivo_mes))).scalars().all()
        if not imports:
            raise LocalError("BASE_VAZIA", "A base local está vazia. Importe um mês antes de consultar, ex.: "
                             "python -m app.importar_dados novo-bolsa-familia 202501 --uf AM")
        coverage = ", ".join(f"{PROGRAMAS.get(i.programa, i.programa)} {i.arquivo_mes}{f' ({i.uf})' if i.uf else ''}"
                             for i in imports)

        person_filter, note = await self._identify(session, query)
        rows = (await session.execute(
            select(Pagamento).where(*person_filter)
            .order_by(Pagamento.programa, Pagamento.arquivo_mes.desc(), Pagamento.mes_referencia.desc())
            .limit(MAX_LINHAS + 1)
        )).scalars().all()
        truncated = len(rows) > MAX_LINHAS
        rows = rows[:MAX_LINHAS]

        latest = max(rows, key=lambda r: r.arquivo_mes)
        total = sum((r.valor or Decimal(0) for r in rows), Decimal(0))
        by_program: dict[str, dict[str, list[Pagamento]]] = defaultdict(lambda: defaultdict(list))
        for row in rows:
            by_program[row.programa][row.arquivo_mes].append(row)
        names = [PROGRAMAS.get(p, p) for p in by_program]

        result.pessoa = Person(
            nome=latest.nome, cpf=latest.cpf, nis=latest.nis,
            localidade="/".join(filter(None, (latest.municipio, latest.uf))) or None,
            panorama=Snapshot(
                url=DOWNLOAD_PAGE,
                texto=f"{latest.nome}: {', '.join(names)}. {len(rows)} parcela(s), total {brl(total)}.",
                campos={"Programas": ", ".join(names), "Parcelas": str(len(rows)), "Valor total": brl(total),
                        "Fonte": "Dados abertos do Portal da Transparência (base local)",
                        "Meses na base local": coverage},
            ),
        )

        result.etapa = "montando_beneficios"
        for programa, months in by_program.items():
            label = PROGRAMAS.get(programa, programa)
            benefit = Benefit(programa=label, url=f"{DOWNLOAD_PAGE}/{programa}")
            for mes, items in months.items():
                subtotal = sum((r.valor or Decimal(0) for r in items), Decimal(0))
                benefit.paginas.append(Snapshot(
                    url=download_url(programa, mes),
                    texto=f"{label} — arquivo {mes}: {len(items)} parcela(s), {brl(subtotal)}",
                    tabelas=[[{
                        "Mês competência": r.mes_competencia or "", "Mês referência": r.mes_referencia or "",
                        "UF": r.uf or "", "Município": r.municipio or "",
                        "Valor": brl(r.valor) if r.valor is not None else "",
                    } for r in items]],
                ))
            result.beneficios.append(benefit)

        messages = [m for m in (note, truncated and f"Mostrando as primeiras {MAX_LINHAS} parcelas.") if m]
        result.mensagem = " ".join(messages) or None
        result.etapa = "concluida"
        result.status = "sucesso"

    async def _identify(self, session, query: Query):
        """Escolhe a pessoa e devolve o filtro das parcelas dela e um aviso opcional."""
        if query.is_document:
            if await session.scalar(select(Pagamento.id).where(Pagamento.nis == query.termo).limit(1)):
                return (Pagamento.nis == query.termo,), None
            mask = masked_cpf(query.termo)
            people = (await session.execute(
                select(Pagamento.nome_busca, Pagamento.nis).where(Pagamento.cpf == mask).distinct().limit(2)
            )).all()
            if not people:
                raise LocalError("NAO_ENCONTRADO", "Nenhum NIS ou CPF correspondente nos meses importados")
            if len(people) > 1:
                raise LocalError("CPF_AMBIGUO", "Nos dados abertos o CPF é publicado mascarado (***.123.456-**) e "
                                 "esse trecho pertence a mais de uma pessoa. Busque pelo NIS ou pelo nome completo.")
            nome_busca, nis = people[0]
            return ((Pagamento.nis == nis,) if nis else (Pagamento.cpf == mask, Pagamento.nome_busca == nome_busca)), None

        name = normalize_name(query.termo)
        people = (await session.execute(
            select(Pagamento.nis, Pagamento.cpf, func.max(Pagamento.arquivo_mes).label("ultimo"))
            .where(Pagamento.nome_busca == name)
            .group_by(Pagamento.nis, Pagamento.cpf).order_by(func.max(Pagamento.arquivo_mes).desc()).limit(50)
        )).all()
        if not people:
            raise LocalError("NAO_ENCONTRADO", "Nenhuma pessoa com esse nome completo nos meses importados")
        nis, cpf = people[0].nis, people[0].cpf
        note = None
        if len(people) > 1:
            note = (f"{len(people)}{'+' if len(people) == 50 else ''} pessoas com esse nome na base; "
                    "mostrando a mais recente. Busque pelo NIS para escolher outra.")
        if nis:
            return (Pagamento.nis == nis,), note
        return (Pagamento.nome_busca == name, Pagamento.cpf == cpf if cpf else Pagamento.cpf.is_(None)), note
