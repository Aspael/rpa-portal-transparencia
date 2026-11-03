"""Servidor MCP em /mcp (Streamable HTTP, sem sessão).

Tools:
- ``consultar_pessoa``: mesmo fluxo de ``POST /consultas`` (histórico no PostgreSQL
  primeiro, depois a fonte configurada em QUERY_SOURCE).
- ``listar_base_local``, ``importar_base_local``, ``status_importacao`` e
  ``cancelar_importacao``: mesmas operações de ``/base-local`` (baixar um mês ou um
  ano dos dados abertos e importar no PostgreSQL, em segundo plano).

Roda no mesmo processo da API e é protegido pelo mesmo X-API-Key.
"""
from secrets import compare_digest
from typing import Annotated, Any, Awaitable, Callable

from mcp.server.mcpserver import MCPServer
from mcp.server.streamable_http_manager import StreamableHTTPASGIApp
from mcp.server.transport_security import TransportSecuritySettings
from pydantic import Field, ValidationError
from starlette.responses import JSONResponse
from fastapi.encoders import jsonable_encoder

from app.base_local import BaseLocal, ImportacaoEmAndamento, PedidoImportacao
from app.config import Settings
from app.dados_abertos import PROGRAMAS
from app.models import Query, Result

INSTRUCTIONS = (
    "Consulta pagamentos do Bolsa Família e de programas sociais relacionados (Novo Bolsa Família, "
    "Auxílio Brasil, Auxílio Emergencial, BPC) publicados pelo Portal da Transparência do governo federal. "
    "Busque por nome completo ou NIS; o CPF é publicado mascarado. Responda com base no JSON devolvido "
    "e informe quando status for 'erro', citando a mensagem. "
    "A consulta usa a base local: se ela devolver BASE_VAZIA ou não achar a pessoa, veja os meses com "
    "listar_base_local e, se o usuário pedir, importe um mês com importar_base_local (de preferência com UF) "
    "e acompanhe com status_importacao até terminar."
)

PROGRAMAS_DESC = ", ".join(f"{k} ({v})" for k, v in PROGRAMAS.items())


def _invalid(exc: ValidationError) -> str:
    return "; ".join(error["msg"].removeprefix("Value error, ") for error in exc.errors())


def create_mcp(run_query: Callable[[Query], Awaitable[Result]],
               get_base_local: Callable[[], BaseLocal | None] = lambda: None) -> MCPServer:
    mcp = MCPServer("observa", title="Observa · Bolsa Família", instructions=INSTRUCTIONS, version="1.1.0")

    @mcp.tool(
        name="consultar_pessoa",
        description=(
            "Consulta uma pessoa nos dados do Bolsa Família e programas relacionados do Portal da Transparência. "
            "Devolve o mesmo JSON de POST /consultas: pessoa (nome, CPF mascarado, NIS, localidade), "
            "panorama (programas, parcelas, valor total) e as parcelas por programa e mês."
        ),
    )
    async def consultar_pessoa(
        termo: Annotated[str, Field(description="Nome completo ou NIS (11 dígitos). CPF só funciona se não for ambíguo.")],
        beneficiario_programa_social: Annotated[bool, Field(description="Filtra beneficiários de programa social (modos robô/API)")] = False,
        incluir_evidencia: Annotated[bool, Field(description="Inclui o screenshot em Base64, quando houver (ocupa muito contexto)")] = False,
        atualizar: Annotated[bool, Field(description="Ignora o histórico e consulta a fonte de novo")] = False,
    ) -> dict[str, Any]:
        try:
            query = Query(termo=termo, beneficiario_programa_social=beneficiario_programa_social, atualizar=atualizar)
        except ValidationError as exc:
            return Result(codigo="ENTRADA_INVALIDA", mensagem=_invalid(exc)).model_dump(mode="json")
        data = (await run_query(query)).model_dump(mode="json")
        if not incluir_evidencia:
            # O Base64 do screenshot lotaria o contexto de um modelo local.
            data["evidencia"] = None
            if data.get("diagnostico"):
                data["diagnostico"]["evidencia"] = None
        return data

    def manager_or_error() -> tuple[BaseLocal | None, dict | None]:
        manager = get_base_local()
        if not manager:
            return None, {"status": "erro", "codigo": "BASE_LOCAL_DESATIVADA",
                          "mensagem": "Base local desativada: o servidor precisa de DATABASE_URL (PostgreSQL)."}
        return manager, None

    @mcp.tool(
        name="listar_base_local",
        description=(
            "Mostra o que está na base local de dados abertos: programas disponíveis, meses já importados "
            "(programa, AAAAMM, UF, linhas, data da importação) e a importação em andamento, se houver."
        ),
    )
    async def listar_base_local() -> dict[str, Any]:
        manager, error = manager_or_error()
        if error:
            return error
        try:
            summary = await manager.resumo()
        except Exception:
            return {"status": "erro", "codigo": "BASE_LOCAL_INDISPONIVEL",
                    "mensagem": "Não foi possível acessar a base local no PostgreSQL."}
        summary["tarefas"] = summary["tarefas"][:5]
        return {"status": "sucesso", **jsonable_encoder(summary)}

    @mcp.tool(
        name="importar_base_local",
        description=(
            "Baixa do Portal da Transparência o arquivo mensal de um programa e importa na base local, em "
            "segundo plano. Informe ano e mês, ou só o ano para importar o ano inteiro (até o mês atual). "
            "Um mês sem UF tem milhões de linhas e centenas de MB: prefira filtrar por UF. Devolve a tarefa; "
            "acompanhe com status_importacao. Só uma importação roda por vez."
        ),
    )
    async def importar_base_local(
        programa: Annotated[str, Field(description=f"Programa: {PROGRAMAS_DESC}")],
        ano: Annotated[int, Field(description="Ano do arquivo, ex.: 2025")],
        mes: Annotated[int | None, Field(description="Mês de 1 a 12; omita para o ano inteiro")] = None,
        uf: Annotated[str | None, Field(description="Sigla da UF, ex.: AM (recomendado)")] = None,
        limite: Annotated[int | None, Field(description="Máximo de linhas por mês, para testes")] = None,
    ) -> dict[str, Any]:
        manager, error = manager_or_error()
        if error:
            return error
        try:
            pedido = PedidoImportacao(programa=programa, ano=ano, mes=mes, uf=uf, limite=limite)
        except ValidationError as exc:
            return {"status": "erro", "codigo": "ENTRADA_INVALIDA", "mensagem": _invalid(exc)}
        try:
            tarefa = manager.importar(pedido)
        except ImportacaoEmAndamento as exc:
            return {"status": "erro", "codigo": "IMPORTACAO_EM_ANDAMENTO", "mensagem": str(exc),
                    "tarefa": exc.tarefa.model_dump(mode="json")}
        return {"status": "sucesso", "mensagem": "Importação iniciada; acompanhe com status_importacao.",
                "tarefa": tarefa.model_dump(mode="json")}

    @mcp.tool(
        name="status_importacao",
        description=("Progresso de uma importação da base local: status geral, mês atual, percentual do download "
                     "e o resultado de cada mês. Sem tarefa_id, mostra a importação atual ou a mais recente."),
    )
    async def status_importacao(
        tarefa_id: Annotated[str | None, Field(description="ID devolvido por importar_base_local")] = None,
    ) -> dict[str, Any]:
        manager, error = manager_or_error()
        if error:
            return error
        tarefa = manager.tarefa(tarefa_id)
        if not tarefa:
            return {"status": "erro", "codigo": "TAREFA_NAO_ENCONTRADA",
                    "mensagem": "Nenhuma importação com esse ID desde que o servidor iniciou."}
        return {"status": "sucesso", "tarefa": tarefa.model_dump(mode="json")}

    @mcp.tool(
        name="cancelar_importacao",
        description="Cancela uma importação em andamento. O mês em curso é desfeito; meses já concluídos ficam na base.",
    )
    async def cancelar_importacao(
        tarefa_id: Annotated[str, Field(description="ID devolvido por importar_base_local")],
    ) -> dict[str, Any]:
        manager, error = manager_or_error()
        if error:
            return error
        tarefa = manager.cancelar(tarefa_id)
        if not tarefa:
            return {"status": "erro", "codigo": "TAREFA_NAO_ENCONTRADA", "mensagem": "Tarefa não encontrada."}
        return {"status": "sucesso", "tarefa": tarefa.model_dump(mode="json")}

    return mcp


class McpEndpoint:
    """ASGI de /mcp: confere o X-API-Key e repassa ao transporte Streamable HTTP do MCP."""

    def __init__(self, mcp: MCPServer, settings: Settings):
        self.settings = settings
        # A segurança vem do X-API-Key; a proteção de DNS rebinding do SDK só aceitaria
        # localhost e bloquearia o domínio público e o host "api" da rede Docker.
        mcp.streamable_http_app(stateless_http=True, json_response=True,
                                transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False))
        self.session_manager = mcp.session_manager
        self.transport = StreamableHTTPASGIApp(self.session_manager)

    async def __call__(self, scope, receive, send):
        expected = self.settings.api_key.get_secret_value() if self.settings.api_key else ""
        if expected:
            headers = dict(scope.get("headers") or [])
            key = headers.get(b"x-api-key", b"")
            if not key or not compare_digest(key, expected.encode()):
                await JSONResponse({"detail": "Chave de API inválida"}, status_code=401)(scope, receive, send)
                return
        await self.transport(scope, receive, send)
