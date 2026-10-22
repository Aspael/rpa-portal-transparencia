import re
from datetime import datetime, timezone
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, Field, field_validator


class Query(BaseModel):
    termo: str = Field(min_length=2, max_length=160, description="Nome, CPF ou NIS")
    beneficiario_programa_social: bool = False

    @field_validator("termo")
    @classmethod
    def normalize(cls, value: str) -> str:
        value = " ".join(value.split())
        if re.fullmatch(r"[\d.\- /]+", value):
            value = re.sub(r"\D", "", value)
            if len(value) != 11:
                raise ValueError("CPF ou NIS deve conter 11 dígitos")
        elif len(value) < 2 or not any(c.isalpha() for c in value):
            raise ValueError("Informe um nome, CPF ou NIS")
        return value

    @property
    def is_document(self) -> bool:
        return self.termo.isdigit()


class Snapshot(BaseModel):
    url: str
    texto: str
    campos: dict[str, str] = Field(default_factory=dict)
    tabelas: list[list[dict[str, str]]] = Field(default_factory=list)


class Benefit(BaseModel):
    programa: str
    url: str
    paginas: list[Snapshot] = Field(default_factory=list)
    completo: bool = True
    erro: str | None = None


class Evidence(BaseModel):
    mime_type: str = "image/png"
    base64: str


class Person(BaseModel):
    nome: str | None = None
    cpf: str | None = None
    nis: str | None = None
    localidade: str | None = None
    panorama: Snapshot


class Diagnostic(BaseModel):
    url: str
    titulo: str
    evidencia: Evidence | None = None


class Result(BaseModel):
    modo_execucao: Literal["autonomo", "assistido", "api_oficial"] = "autonomo"
    consulta_id: str = Field(default_factory=lambda: str(uuid4()))
    consultado_em: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    status: Literal["sucesso", "parcial", "erro"] = "erro"
    codigo: str | None = None
    mensagem: str | None = None
    pessoa: Person | None = None
    beneficios: list[Benefit] = Field(default_factory=list)
    evidencia: Evidence | None = None
    etapa: str = "iniciando"
    duracao_ms: int = 0
    diagnostico: Diagnostic | None = None


class ArchiveRequest(Result):
    consulta_id: str = Field(pattern=r"^[0-9a-fA-F]{8}-(?:[0-9a-fA-F]{4}-){3}[0-9a-fA-F]{12}$")
    consultado_em: datetime
    status: Literal["sucesso", "parcial", "erro"]
