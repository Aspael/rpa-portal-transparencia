from contextlib import asynccontextmanager, nullcontext
from secrets import compare_digest
from pathlib import Path
from uuid import UUID
import asyncio
import html
import logging

from fastapi import Depends, FastAPI, HTTPException, Query as Param, Request
from fastapi.encoders import jsonable_encoder as jsonable
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.security import APIKeyHeader
from fastapi.staticfiles import StaticFiles
from starlette.routing import Route

from app.base_local import BaseLocal, ImportacaoEmAndamento, PedidoImportacao, Tarefa, valid_month
from app.config import Settings
from app.dados_abertos import PROGRAMAS, DadosAbertos
from app.history import History
from app.mcp_server import McpEndpoint, create_mcp
from app.models import ArchiveRequest, HistoryItem, Query, Result
from app.portal_api import PortalApi
from app.robot import Robot
from app.google_export import ExportError, ExportReceipt, GoogleExporter

log = logging.getLogger(__name__)


def create_app(settings: Settings | None = None, robot_factory=Robot, exporter_factory=GoogleExporter,
               portal_api_factory=PortalApi, history_factory=History, local_factory=DadosAbertos,
               base_local_factory=BaseLocal) -> FastAPI:
    settings = settings or Settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if settings.require_api_key and not (settings.api_key and settings.api_key.get_secret_value()):
            raise RuntimeError("API_KEY é obrigatória neste ambiente")
        local_mode = settings.query_source == "dados_abertos"
        has_database = bool(settings.database_url and settings.database_url.get_secret_value())
        # Com dados abertos o navegador nunca é usado, então nem é iniciado.
        robot = None if local_mode else robot_factory(settings)
        if robot:
            await robot.start()
        app.state.robot = robot
        app.state.portal_api = None
        app.state.history = None
        app.state.dados_abertos = None
        app.state.base_local = None
        try:
            if not local_mode and settings.portal_api_key and settings.portal_api_key.get_secret_value():
                app.state.portal_api = portal_api_factory(settings)
                await app.state.portal_api.start()
            if local_mode and has_database:
                app.state.dados_abertos = local_factory(settings)
                await app.state.dados_abertos.start()
            if has_database:
                app.state.history = history_factory(settings)
                await app.state.history.start()
                # Baixar/importar meses pela API e pelo MCP; vale em qualquer QUERY_SOURCE.
                app.state.base_local = base_local_factory(settings)
                await app.state.base_local.start()
            # O transporte do MCP precisa do seu gerenciador rodando enquanto a API estiver de pé.
            async with mcp_endpoint.session_manager.run() if mcp_endpoint else nullcontext():
                yield
        finally:
            try:
                if app.state.base_local:
                    await app.state.base_local.close()
                if app.state.history:
                    await app.state.history.close()
                if app.state.dados_abertos:
                    await app.state.dados_abertos.close()
                if app.state.portal_api:
                    await app.state.portal_api.close()
            finally:
                if robot:
                    await robot.close()

    app = FastAPI(title="Observa — Bolsa Família (Portal da Transparência)", version="1.0.0", lifespan=lifespan)
    mcp_endpoint = (McpEndpoint(create_mcp(lambda query: run_query(query), lambda: app.state.base_local), settings)
                    if settings.mcp_enabled else None)
    static = Path(__file__).parent / "static"
    app.mount("/static", StaticFiles(directory=static), name="static")

    @app.get("/", include_in_schema=False)
    async def home(request: Request):
        page = (static / "index.html").read_text(encoding="utf-8")
        key = settings.api_key.get_secret_value() if settings.api_key else ""
        if key and settings.front_api_key_from_env:
            # Chave do .env embutida na página; o campo do front, se preenchido, tem prioridade.
            page = page.replace('<meta name="api-key" content="">',
                                f'<meta name="api-key" content="{html.escape(key, quote=True)}">', 1)
        # Robôs de teste podem não ter o atributo; sem ele, o modo assistido fica desligado.
        if getattr(request.app.state.robot, "assisted_available", False):
            page = page.replace('<meta name="assisted-mode" content="false">', '<meta name="assisted-mode" content="true">', 1)
        return HTMLResponse(page, headers={"Cache-Control": "no-store"})
    api_key = APIKeyHeader(name="X-API-Key", auto_error=False)

    async def authorize(key: str | None = Depends(api_key)):
        expected = settings.api_key.get_secret_value() if settings.api_key else ""
        if expected and (not key or not compare_digest(key.encode(), expected.encode())):
            raise HTTPException(401, "Chave de API inválida")

    @app.get("/health", tags=["Infraestrutura"])
    async def health():
        return {"status": "ok"}

    @app.get("/integracoes/google/status", dependencies=[Depends(authorize)], tags=["Google"])
    async def google_status():
        return {"configurado": settings.google_configured}

    @app.post("/integracoes/google/arquivar", response_model=ExportReceipt,
              dependencies=[Depends(authorize)], tags=["Google"])
    async def archive(result: ArchiveRequest):
        try:
            receipt = await asyncio.to_thread(exporter_factory(settings).export, result)
            return JSONResponse(receipt.model_dump(), headers={"Cache-Control": "no-store"})
        except ExportError as exc:
            status = {"GOOGLE_NAO_CONFIGURADO": 503, "EXPORTACAO_OCUPADA": 503, "CONFLITO_CONSULTA": 409}.get(exc.code, 502)
            return JSONResponse({"codigo": exc.code, "mensagem": str(exc)}, status_code=status, headers={"Cache-Control": "no-store"})
        except Exception:
            return JSONResponse({"codigo": "FALHA_EXPORTACAO", "mensagem": "Não foi possível concluir a exportação. Confira OAuth, conectividade e permissões; repita com o mesmo JSON."}, status_code=502)

    @app.post("/consultas", response_model=Result, dependencies=[Depends(authorize)],
              responses={404: {"model": Result}, 429: {"model": Result}, 502: {"model": Result}, 504: {"model": Result}},
              tags=["Consultas"])
    async def consult(query: Query):
        result = await run_query(query)
        status = 200
        if result.status == "erro":
            status = {"NAO_ENCONTRADO": 404, "TEMPO_ESGOTADO": 504, "LIMITE_API_EXCEDIDO": 429,
                      "BUSCA_POR_NOME_SEM_API": 422, "CHAVE_PORTAL_AUSENTE": 503, "CPF_AMBIGUO": 422,
                      "BASE_VAZIA": 503, "BASE_LOCAL_DESATIVADA": 503,
                      "BASE_LOCAL_INDISPONIVEL": 503}.get(result.codigo, 502)
        return JSONResponse(result.model_dump(mode="json"), status_code=status, headers={"Cache-Control": "no-store"})

    class Refusal:
        """Fonte que só devolve um erro: usada quando QUERY_SOURCE=api não pode atender sem abrir o site."""
        def __init__(self, code: str, message: str):
            self.code, self.message = code, message

        async def query(self, query: Query) -> Result:
            mode = "dados_abertos" if settings.query_source == "dados_abertos" else "api_oficial"
            return Result(modo_execucao=mode, codigo=self.code, mensagem=self.message)

    async def run_query(query: Query) -> Result:
        """Fluxo comum de POST /consultas e da tool MCP: histórico, fonte configurada e gravação."""
        store = app.state.history
        # SELECT primeiro: termo já consultado com sucesso volta do PostgreSQL, sem nova consulta.
        if store and not query.atualizar:
            try:
                cached = await store.find(query)
            except Exception:
                log.exception("Não foi possível ler o histórico para o termo consultado")
                cached = None
            if cached:
                return cached
        result = await pick_source(query).query(query)
        if store and result.status == "sucesso":
            # INSERT só de consultas com sucesso. Falha no banco não derruba a consulta.
            try:
                await store.save(query, result)
            except Exception:
                log.exception("Não foi possível gravar a consulta %s no histórico", result.consulta_id)
        return result

    def pick_source(query: Query):
        if settings.query_source == "dados_abertos":
            return app.state.dados_abertos or Refusal(
                "BASE_LOCAL_DESATIVADA", "QUERY_SOURCE=dados_abertos exige DATABASE_URL (PostgreSQL com os dados importados).")
        # CPF/NIS com PORTAL_API_KEY vai pela API de Dados oficial, sem CAPTCHA.
        # A API não busca por nome: em "auto" o nome vai ao site; em "api", é recusado.
        portal_api, robot = app.state.portal_api, app.state.robot
        if settings.query_source == "site":
            return robot
        if settings.query_source == "auto":
            return portal_api if portal_api and query.is_document else robot
        if not query.is_document:
            return Refusal("BUSCA_POR_NOME_SEM_API", "A API oficial do governo só consulta por CPF ou NIS. "
                           "Informe o documento ou use QUERY_SOURCE=auto para buscar nomes pelo site.")
        if not portal_api:
            return Refusal("CHAVE_PORTAL_AUSENTE", "QUERY_SOURCE=api exige PORTAL_API_KEY no .env "
                           "(cadastro gratuito em portaldatransparencia.gov.br/api-de-dados).")
        return portal_api

    def history_unavailable(code: str, message: str):
        return JSONResponse({"codigo": code, "mensagem": message}, status_code=503, headers={"Cache-Control": "no-store"})

    @app.get("/consultas", response_model=list[HistoryItem], dependencies=[Depends(authorize)], tags=["Histórico"])
    async def history(request: Request, limite: int = Param(20, ge=1, le=100),
                      busca: str | None = Param(None, max_length=160, description="Trecho do termo ou do nome")):
        store = request.app.state.history
        if not store:
            return history_unavailable("HISTORICO_DESATIVADO", "Histórico desativado: configure DATABASE_URL.")
        try:
            items = await store.list(limite, " ".join(busca.split()) if busca else None)
        except Exception:
            log.exception("Falha ao listar o histórico")
            return history_unavailable("HISTORICO_INDISPONIVEL", "Não foi possível acessar o banco de dados do histórico.")
        return JSONResponse([item.model_dump(mode="json") for item in items], headers={"Cache-Control": "no-store"})

    @app.get("/consultas/{consulta_id}", response_model=Result, dependencies=[Depends(authorize)], tags=["Histórico"])
    async def history_item(consulta_id: UUID, request: Request):
        store = request.app.state.history
        if not store:
            return history_unavailable("HISTORICO_DESATIVADO", "Histórico desativado: configure DATABASE_URL.")
        try:
            result = await store.get(consulta_id)
        except Exception:
            log.exception("Falha ao ler a consulta %s do histórico", consulta_id)
            return history_unavailable("HISTORICO_INDISPONIVEL", "Não foi possível acessar o banco de dados do histórico.")
        if not result:
            raise HTTPException(404, "Consulta não encontrada no histórico")
        return JSONResponse(result.model_dump(mode="json"), headers={"Cache-Control": "no-store"})

    def base_local_or_error(request: Request):
        manager = request.app.state.base_local
        if not manager:
            return None, history_unavailable("BASE_LOCAL_DESATIVADA",
                                             "Base local desativada: configure DATABASE_URL (PostgreSQL).")
        return manager, None

    def no_store(data, status_code: int = 200):
        return JSONResponse(data, status_code=status_code, headers={"Cache-Control": "no-store"})

    @app.get("/base-local", dependencies=[Depends(authorize)], tags=["Base local"],
             summary="Programas, meses importados e tarefas de importação")
    async def base_local_summary(request: Request):
        manager, error = base_local_or_error(request)
        if error:
            return error
        try:
            return no_store(jsonable(await manager.resumo()))
        except Exception:
            log.exception("Falha ao listar a base local")
            return history_unavailable("BASE_LOCAL_INDISPONIVEL", "Não foi possível acessar a base local no PostgreSQL.")

    @app.post("/base-local/importacoes", response_model=Tarefa, status_code=202, dependencies=[Depends(authorize)],
              tags=["Base local"], summary="Baixa e importa um mês (ou o ano inteiro) em segundo plano")
    async def base_local_import(pedido: PedidoImportacao, request: Request):
        manager, error = base_local_or_error(request)
        if error:
            return error
        try:
            tarefa = manager.importar(pedido)
        except ImportacaoEmAndamento as exc:
            return no_store({"codigo": "IMPORTACAO_EM_ANDAMENTO", "mensagem": str(exc),
                             "tarefa": exc.tarefa.model_dump(mode="json")}, 409)
        return no_store(tarefa.model_dump(mode="json"), 202)

    @app.get("/base-local/tarefas/{tarefa_id}", response_model=Tarefa, dependencies=[Depends(authorize)],
             tags=["Base local"], summary="Progresso de uma importação")
    async def base_local_task(tarefa_id: str, request: Request):
        manager, error = base_local_or_error(request)
        if error:
            return error
        tarefa = manager.tarefa(tarefa_id)
        if not tarefa:
            raise HTTPException(404, "Tarefa não encontrada (as tarefas ficam só na memória do processo)")
        return no_store(tarefa.model_dump(mode="json"))

    @app.post("/base-local/tarefas/{tarefa_id}/cancelar", response_model=Tarefa, dependencies=[Depends(authorize)],
              tags=["Base local"], summary="Cancela uma importação; o mês em curso é desfeito")
    async def base_local_cancel(tarefa_id: str, request: Request):
        manager, error = base_local_or_error(request)
        if error:
            return error
        tarefa = manager.cancelar(tarefa_id)
        if not tarefa:
            raise HTTPException(404, "Tarefa não encontrada")
        return no_store(tarefa.model_dump(mode="json"))

    @app.delete("/base-local/importacoes/{programa}/{arquivo_mes}", dependencies=[Depends(authorize)],
                tags=["Base local"], summary="Remove da base local as parcelas de um programa/mês")
    async def base_local_remove(programa: str, arquivo_mes: str, request: Request):
        if programa not in PROGRAMAS or not valid_month(arquivo_mes):
            raise HTTPException(422, "Programa ou mês (AAAAMM) inválido")
        manager, error = base_local_or_error(request)
        if error:
            return error
        try:
            removed = await manager.remover(programa, arquivo_mes)
        except ImportacaoEmAndamento:
            return no_store({"codigo": "IMPORTACAO_EM_ANDAMENTO", "mensagem": "Esse mês está sendo importado agora."}, 409)
        except Exception:
            log.exception("Falha ao remover %s %s da base local", programa, arquivo_mes)
            return history_unavailable("BASE_LOCAL_INDISPONIVEL", "Não foi possível acessar a base local no PostgreSQL.")
        if not removed:
            raise HTTPException(404, "Esse programa/mês não está na base local")
        return no_store({"programa": programa, "arquivo_mes": arquivo_mes, "removido": True})

    if mcp_endpoint:
        # Rota exata /mcp (sem redirecionar para /mcp/), aceitando os métodos do transporte.
        app.router.routes.append(Route("/mcp", endpoint=mcp_endpoint, include_in_schema=False))
    return app


app = create_app()
