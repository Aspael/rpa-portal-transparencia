"""Servidor MCP em /mcp (Streamable HTTP, sem sessão) com a tool ``consultar_pessoa``.

Roda no mesmo processo da API e chama o mesmo fluxo de ``POST /consultas``:
histórico no PostgreSQL primeiro, depois a fonte configurada em QUERY_SOURCE
(por padrão, a base local de dados abertos). Protegido pelo mesmo X-API-Key.
"""
from secrets import compare_digest
from typing import Annotated, Any, Awaitable, Callable

from mcp.server.mcpserver import MCPServer
from mcp.server.streamable_http_manager import StreamableHTTPASGIApp
from mcp.server.transport_security import TransportSecuritySettings
from pydantic import Field, ValidationError
from starlette.responses import JSONResponse

from app.config import Settings
from app.models import Query, Result

INSTRUCTIONS = (
    "Consulta pagamentos do Bolsa Família e de programas sociais relacionados (Novo Bolsa Família, "
    "Auxílio Brasil, Auxílio Emergencial, BPC) publicados pelo Portal da Transparência do governo federal. "
    "Busque por nome completo ou NIS; o CPF é publicado mascarado. Responda com base no JSON devolvido "
    "e informe quando status for 'erro', citando a mensagem."
)


def create_mcp(run_query: Callable[[Query], Awaitable[Result]]) -> MCPServer:
    mcp = MCPServer("observa", title="Observa · Bolsa Família", instructions=INSTRUCTIONS, version="1.0.0")

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
            message = "; ".join(error["msg"].removeprefix("Value error, ") for error in exc.errors())
            return Result(codigo="ENTRADA_INVALIDA", mensagem=message).model_dump(mode="json")
        data = (await run_query(query)).model_dump(mode="json")
        if not incluir_evidencia:
            # O Base64 do screenshot lotaria o contexto de um modelo local.
            data["evidencia"] = None
            if data.get("diagnostico"):
                data["diagnostico"]["evidencia"] = None
        return data

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
