from contextlib import asynccontextmanager
from secrets import compare_digest
from pathlib import Path
from uuid import UUID
import asyncio
import logging

from fastapi import Depends, FastAPI, HTTPException, Query as Param, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.security import APIKeyHeader
from fastapi.staticfiles import StaticFiles

from app.config import Settings
from app.history import History
from app.models import ArchiveRequest, HistoryItem, Query, Result
from app.portal_api import PortalApi
from app.robot import Robot
from app.google_export import ExportError, ExportReceipt, GoogleExporter

log = logging.getLogger(__name__)


def create_app(settings: Settings | None = None, robot_factory=Robot, exporter_factory=GoogleExporter,
               portal_api_factory=PortalApi, history_factory=History) -> FastAPI:
    settings = settings or Settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if settings.require_api_key and not (settings.api_key and settings.api_key.get_secret_value()):
            raise RuntimeError("API_KEY é obrigatória neste ambiente")
        robot = robot_factory(settings)
        await robot.start()
        app.state.robot = robot
        app.state.portal_api = None
        app.state.history = None
        try:
            if settings.portal_api_key and settings.portal_api_key.get_secret_value():
                app.state.portal_api = portal_api_factory(settings)
                await app.state.portal_api.start()
            if settings.database_url and settings.database_url.get_secret_value():
                app.state.history = history_factory(settings)
                await app.state.history.start()
            yield
        finally:
            try:
                if app.state.history:
                    await app.state.history.close()
                if app.state.portal_api:
                    await app.state.portal_api.close()
            finally:
                await robot.close()

    app = FastAPI(title="Portal da Transparência — RPA", version="1.0.0", lifespan=lifespan)
    static = Path(__file__).parent / "static"
    app.mount("/static", StaticFiles(directory=static), name="static")

    @app.get("/", include_in_schema=False)
    async def home():
        return FileResponse(static / "index.html", headers={"Cache-Control": "no-store"})
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
    async def consult(query: Query, request: Request):
        # CPF/NIS com PORTAL_API_KEY vai pela API de Dados oficial, sem CAPTCHA;
        # busca por nome continua no site, que a API não oferece.
        portal_api = request.app.state.portal_api
        source = portal_api if portal_api and query.is_document else request.app.state.robot
        result = await source.query(query)
        if request.app.state.history:
            # Falha no banco não derruba a consulta: o resultado volta ao cliente mesmo assim.
            try:
                await request.app.state.history.save(query, result)
            except Exception:
                log.exception("Não foi possível gravar a consulta %s no histórico", result.consulta_id)
        status = 200
        if result.status == "erro":
            status = {"NAO_ENCONTRADO": 404, "TEMPO_ESGOTADO": 504, "LIMITE_API_EXCEDIDO": 429}.get(result.codigo, 502)
        return JSONResponse(result.model_dump(mode="json"), status_code=status, headers={"Cache-Control": "no-store"})

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

    return app


app = create_app()
