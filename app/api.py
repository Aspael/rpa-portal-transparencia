from contextlib import asynccontextmanager
from secrets import compare_digest
from pathlib import Path
import asyncio

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.security import APIKeyHeader
from fastapi.staticfiles import StaticFiles

from app.config import Settings
from app.models import ArchiveRequest, Query, Result
from app.portal_api import PortalApi
from app.robot import Robot
from app.google_export import ExportError, ExportReceipt, GoogleExporter


def create_app(settings: Settings | None = None, robot_factory=Robot, exporter_factory=GoogleExporter,
               portal_api_factory=PortalApi) -> FastAPI:
    settings = settings or Settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if settings.require_api_key and not (settings.api_key and settings.api_key.get_secret_value()):
            raise RuntimeError("API_KEY é obrigatória neste ambiente")
        robot = robot_factory(settings)
        await robot.start()
        app.state.robot = robot
        app.state.portal_api = None
        try:
            if settings.portal_api_key and settings.portal_api_key.get_secret_value():
                app.state.portal_api = portal_api_factory(settings)
                await app.state.portal_api.start()
            yield
        finally:
            try:
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
        status = 200
        if result.status == "erro":
            status = {"NAO_ENCONTRADO": 404, "TEMPO_ESGOTADO": 504, "LIMITE_API_EXCEDIDO": 429}.get(result.codigo, 502)
        return JSONResponse(result.model_dump(mode="json"), status_code=status, headers={"Cache-Control": "no-store"})

    return app


app = create_app()
