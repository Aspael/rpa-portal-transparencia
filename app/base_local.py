"""Gestão da base local pela API e pelo MCP: baixar e importar meses em segundo plano.

Um pedido escolhe programa, ano e mês (ou o ano inteiro) e, opcionalmente, uma UF.
Cada pedido vira uma tarefa que roda numa thread, um mês por vez, com progresso
legível e cancelamento. Só uma tarefa roda por vez: cada mês pode ter centenas de MB.
"""
import asyncio
import logging
import re
from datetime import date, datetime, timezone
from threading import Event
from typing import Callable, Literal
from uuid import uuid4

from pydantic import BaseModel, Field, field_validator, model_validator

from app.config import Settings
from app.dados_abertos import PROGRAMAS
from app import importar_dados

log = logging.getLogger(__name__)

PRIMEIRO_ANO = 2013
UFS = ("AC", "AL", "AM", "AP", "BA", "CE", "DF", "ES", "GO", "MA", "MG", "MS", "MT", "PA", "PB", "PE",
       "PI", "PR", "RJ", "RN", "RO", "RR", "RS", "SC", "SE", "SP", "TO")
# Quantas tarefas terminadas ficam na memória para consulta.
HISTORICO_TAREFAS = 20


class PedidoImportacao(BaseModel):
    programa: str = Field(description=f"Programa: {', '.join(PROGRAMAS)}")
    ano: int = Field(ge=PRIMEIRO_ANO, description="Ano do arquivo, ex.: 2025")
    mes: int | None = Field(None, ge=1, le=12, description="Mês (1–12). Vazio importa o ano inteiro, até o mês atual")
    uf: str | None = Field(None, description="Sigla da UF para importar só um estado (recomendado)")
    limite: int | None = Field(None, ge=1, description="Para depois de N linhas por mês (para testes)")
    baixar_de_novo: bool = Field(False, description="Ignora o ZIP já baixado e baixa de novo")

    @field_validator("programa")
    @classmethod
    def known_program(cls, value: str) -> str:
        if value not in PROGRAMAS:
            raise ValueError(f"Programa desconhecido. Use um destes: {', '.join(PROGRAMAS)}")
        return value

    @field_validator("uf")
    @classmethod
    def known_state(cls, value: str | None) -> str | None:
        value = (value or "").strip().upper() or None
        if value and value not in UFS:
            raise ValueError("UF inválida: use a sigla do estado, ex.: AM")
        return value

    @model_validator(mode="after")
    def not_in_future(self):
        if not self.meses():
            raise ValueError("Ano ou mês no futuro: o portal ainda não publicou esse arquivo")
        return self

    def meses(self) -> list[str]:
        today = date.today()
        months = [self.mes] if self.mes else range(1, 13)
        return [f"{self.ano}{m:02d}" for m in months if (self.ano, m) <= (today.year, today.month)]


class MesTarefa(BaseModel):
    mes: str
    status: Literal["pendente", "baixando", "importando", "concluido", "erro", "cancelado"] = "pendente"
    linhas: int | None = None
    mensagem: str | None = None


class Tarefa(BaseModel):
    tarefa_id: str = Field(default_factory=lambda: uuid4().hex)
    programa: str
    programa_nome: str
    uf: str | None = None
    limite: int | None = None
    status: Literal["na_fila", "em_andamento", "concluida", "parcial", "erro", "cancelada"] = "na_fila"
    mes_atual: str | None = None
    percentual: int | None = Field(None, description="Progresso do download do mês atual, quando o portal informa o tamanho")
    mensagem: str | None = None
    meses: list[MesTarefa]
    linhas: int = 0
    criada_em: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    terminada_em: datetime | None = None

    @property
    def ativa(self) -> bool:
        return self.status in ("na_fila", "em_andamento")


class ImportacaoEmAndamento(Exception):
    def __init__(self, tarefa: Tarefa):
        super().__init__(f"Já existe uma importação em andamento ({tarefa.programa_nome}, "
                         f"tarefa {tarefa.tarefa_id}). Aguarde ou cancele antes de pedir outra.")
        self.tarefa = tarefa


# runner(settings, programa, mes, uf, limite, baixar_de_novo, progress, cancel) -> linhas
Runner = Callable[..., int]


def default_runner(settings, programa, mes, uf, limit, force, progress, cancel) -> int:
    return importar_dados.download_and_import(settings, programa, mes, uf, limit, force, progress, cancel)


class BaseLocal:
    """Lista, importa e remove meses da base local de dados abertos."""

    def __init__(self, settings: Settings, runner: Runner = default_runner):
        self.settings = settings
        self.database_url = settings.database_url.get_secret_value()
        self.runner = runner
        self.tarefas: dict[str, Tarefa] = {}
        self._cancel: dict[str, Event] = {}
        self._jobs: set[asyncio.Task] = set()

    async def start(self):
        pass

    async def close(self):
        # Pede para parar e espera: a thread sai no próximo bloco e o PostgreSQL desfaz a transação.
        for event in self._cancel.values():
            event.set()
        if self._jobs:
            await asyncio.gather(*self._jobs, return_exceptions=True)

    # ---- consulta ----
    async def importados(self) -> list[dict]:
        return await asyncio.to_thread(importar_dados.fetch_imports, self.database_url)

    def atual(self) -> Tarefa | None:
        return next((t for t in self.tarefas.values() if t.ativa), None)

    def recentes(self) -> list[Tarefa]:
        return sorted(self.tarefas.values(), key=lambda t: t.criada_em, reverse=True)

    def tarefa(self, tarefa_id: str | None = None) -> Tarefa | None:
        if tarefa_id:
            return self.tarefas.get(tarefa_id)
        return self.atual() or next(iter(self.recentes()), None)

    async def resumo(self) -> dict:
        return {
            "programas": [{"programa": k, "nome": v} for k, v in PROGRAMAS.items()],
            "importados": await self.importados(),
            "tarefa_atual": (t := self.atual()) and t.model_dump(mode="json"),
            "tarefas": [t.model_dump(mode="json") for t in self.recentes()],
        }

    # ---- ações ----
    def importar(self, pedido: PedidoImportacao) -> Tarefa:
        if running := self.atual():
            raise ImportacaoEmAndamento(running)
        tarefa = Tarefa(programa=pedido.programa, programa_nome=PROGRAMAS[pedido.programa], uf=pedido.uf,
                        limite=pedido.limite, meses=[MesTarefa(mes=m) for m in pedido.meses()])
        self.tarefas[tarefa.tarefa_id] = tarefa
        self._cancel[tarefa.tarefa_id] = Event()
        job = asyncio.create_task(self._run(tarefa, pedido.baixar_de_novo))
        self._jobs.add(job)
        job.add_done_callback(self._jobs.discard)
        self._prune()
        return tarefa

    def cancelar(self, tarefa_id: str) -> Tarefa | None:
        tarefa = self.tarefas.get(tarefa_id)
        if tarefa and tarefa.ativa:
            self._cancel[tarefa_id].set()
            tarefa.mensagem = "Cancelando: a importação para no próximo bloco e o mês em curso é desfeito."
        return tarefa

    async def remover(self, programa: str, mes: str) -> bool:
        if running := self.atual():
            if running.programa == programa and running.mes_atual == mes:
                raise ImportacaoEmAndamento(running)
        return await asyncio.to_thread(importar_dados.remove_import, self.database_url, programa, mes)

    # ---- execução ----
    async def _run(self, tarefa: Tarefa, force: bool):
        cancel = self._cancel[tarefa.tarefa_id]
        tarefa.status = "em_andamento"
        try:
            for item in tarefa.meses:
                if cancel.is_set():
                    item.status = "cancelado"
                    continue
                tarefa.mes_atual, tarefa.percentual = item.mes, None
                item.status = "baixando"

                def progress(text: str, percent: int | None = None, item=item):
                    # Chamado pela thread da importação; só atribuições simples.
                    tarefa.mensagem = text.strip()
                    if text.lstrip().startswith("Importando"):
                        item.status = "importando"
                        tarefa.percentual = None
                    elif percent is not None:
                        tarefa.percentual = percent

                try:
                    item.linhas = await asyncio.to_thread(
                        self.runner, self.settings, tarefa.programa, item.mes, tarefa.uf, tarefa.limite,
                        force, progress, cancel)
                    item.status = "concluido"
                    item.mensagem = f"{item.linhas:,} linhas importadas".replace(",", ".")
                    tarefa.linhas += item.linhas
                except importar_dados.ImportacaoCancelada:
                    item.status, item.mensagem = "cancelado", "Cancelado; nada foi alterado neste mês"
                except importar_dados.ImportacaoErro as exc:
                    item.status, item.mensagem = "erro", str(exc)
                except Exception as exc:
                    log.exception("Falha ao importar %s %s", tarefa.programa, item.mes)
                    item.status = "erro"
                    item.mensagem = ("Não foi possível gravar no PostgreSQL" if _is_db_error(exc)
                                     else f"Falha inesperada: {type(exc).__name__}")
        finally:
            statuses = {m.status for m in tarefa.meses}
            if cancel.is_set():
                tarefa.status = "cancelada"
            elif statuses == {"concluido"}:
                tarefa.status = "concluida"
            elif "concluido" in statuses:
                tarefa.status = "parcial"
            else:
                tarefa.status = "erro"
            done = sum(m.status == "concluido" for m in tarefa.meses)
            tarefa.mensagem = (f"{done} de {len(tarefa.meses)} mês(es) importado(s), "
                               f"{tarefa.linhas:,} linhas.".replace(",", "."))
            tarefa.mes_atual, tarefa.percentual = None, None
            tarefa.terminada_em = datetime.now(timezone.utc)

    def _prune(self):
        finished = [t for t in self.recentes() if not t.ativa]
        for old in finished[HISTORICO_TAREFAS:]:
            self.tarefas.pop(old.tarefa_id, None)
            self._cancel.pop(old.tarefa_id, None)


def _is_db_error(exc: Exception) -> bool:
    return type(exc).__module__.startswith(("psycopg", "sqlalchemy"))


def valid_month(value: str) -> bool:
    return bool(re.fullmatch(r"20\d{2}(0[1-9]|1[0-2])", value))
