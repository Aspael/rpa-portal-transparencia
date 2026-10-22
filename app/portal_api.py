"""Consulta por CPF/NIS na API de Dados oficial do Portal da Transparência.

Canal oficial para acesso automatizado: não passa pelo site (AWS WAF/CAPTCHA).
Exige a chave gratuita do cadastro gov.br, enviada no header ``chave-api-dados``,
e respeita o limite de requisições por minuto da chave.
"""
import asyncio
from collections import deque
from time import monotonic
from urllib.parse import urlencode

import httpx

from app.config import Settings
from app.models import Benefit, Person, Query, Result, Snapshot
from app.robot import TIMEOUT_MESSAGE, PortalError

# (indicador em /pessoa-fisica, programa, endpoint, parâmetro do CPF/NIS)
PROGRAMS = [
    ("favorecidoNovoBolsaFamilia", "Novo Bolsa Família", "novo-bolsa-familia-sacado-por-cpf-ou-nis", "codigo"),
    ("favorecidoBolsaFamilia", "Bolsa Família", "bolsa-familia-sacado-por-cpf-ou-nis", "codigo"),
    ("favorecidoAuxilioBrasil", "Auxílio Brasil", "auxilio-brasil-sacado-por-cpf-ou-nis", "codigo"),
    ("auxilioEmergencial", "Auxílio Emergencial", "auxilio-emergencial-por-cpf-ou-nis", "codigoBeneficiario"),
    ("favorecidoBpc", "Benefício de Prestação Continuada (BPC)", "bpc-por-cpf-ou-nis", "codigo"),
    ("favorecidoPeti", "PETI", "peti-por-cpf-ou-nis", "codigo"),
    ("favorecidoSafra", "Garantia-Safra", "safra-codigo-por-cpf-ou-nis", "codigo"),
    ("favorecidoSeguroDefeso", "Seguro Defeso", "seguro-defeso-codigo", "codigo"),
]


def flatten(data, prefix: str = "") -> dict[str, str]:
    """Achata o JSON aninhado em colunas texto, como as tabelas do robô."""
    if isinstance(data, dict):
        flat = {}
        for key, value in data.items():
            flat.update(flatten(value, f"{prefix}.{key}" if prefix else key))
        return flat
    if isinstance(data, list):
        return {prefix: "; ".join(str(item) for item in data)}
    return {prefix: "" if data is None else str(data)}


class RateLimiter:
    """Janela deslizante de 60 s: no máximo ``rpm`` requisições por minuto."""

    def __init__(self, rpm: int):
        self.rpm = rpm
        self._sent: deque[float] = deque()
        self._lock = asyncio.Lock()

    async def acquire(self):
        async with self._lock:
            while True:
                now = monotonic()
                while self._sent and now - self._sent[0] >= 60:
                    self._sent.popleft()
                if len(self._sent) < self.rpm:
                    self._sent.append(now)
                    return
                await asyncio.sleep(60 - (now - self._sent[0]))


class PortalApi:
    # Espera antes de repetir após HTTP 429; depois da última, desiste.
    RETRY_DELAYS = (5, 15)

    def __init__(self, settings: Settings, transport: httpx.AsyncBaseTransport | None = None):
        self.settings = settings
        self._slots = asyncio.Semaphore(settings.max_concurrent)
        self._limiter = RateLimiter(settings.portal_api_max_rpm)
        self._transport = transport
        self._client: httpx.AsyncClient | None = None

    async def start(self):
        self._client = httpx.AsyncClient(
            base_url=self.settings.portal_api_url,
            headers={"chave-api-dados": self.settings.portal_api_key.get_secret_value(), "Accept": "application/json"},
            timeout=self.settings.navigation_timeout_ms / 1000,
            transport=self._transport,
        )

    async def close(self):
        if self._client:
            await self._client.aclose()

    def public_url(self, path: str, params: dict) -> str:
        # A chave vai no header, nunca na URL registrada no resultado.
        return f"{self.settings.portal_api_url}/{path}?{urlencode(params)}"

    async def _get(self, path: str, params: dict):
        for delay in (*self.RETRY_DELAYS, None):
            await self._limiter.acquire()
            try:
                response = await self._client.get(f"/{path}", params=params)
            except httpx.TimeoutException:
                raise TimeoutError
            except httpx.HTTPError:
                raise PortalError("FALHA_API_PORTAL", "Não foi possível conectar à API de Dados do Portal da Transparência")
            if response.status_code == 429:
                if delay is None:
                    raise PortalError("LIMITE_API_EXCEDIDO", "Limite de requisições da API de Dados excedido; aguarde um minuto e tente novamente")
                retry_after = response.headers.get("Retry-After", "")
                await asyncio.sleep(int(retry_after) if retry_after.isdigit() else delay)
                continue
            if response.status_code in (401, 403):
                raise PortalError("CHAVE_PORTAL_INVALIDA", "A API de Dados recusou a chave; confira PORTAL_API_KEY")
            if response.status_code == 404:
                return None
            if response.status_code >= 400:
                raise PortalError("FALHA_API_PORTAL", f"A API de Dados respondeu HTTP {response.status_code}")
            try:
                return response.json()
            except ValueError:
                raise PortalError("FALHA_API_PORTAL", "A API de Dados devolveu uma resposta que não é JSON")

    async def _person(self, document: str):
        # 11 dígitos podem ser CPF ou NIS: tenta CPF e, sem registro, NIS.
        for field in ("cpf", "nis"):
            params = {field: document}
            data = await self._get("pessoa-fisica", params)
            if isinstance(data, dict) and any(data.get(key) for key in ("nome", "cpf", "nis")):
                return data, self.public_url("pessoa-fisica", params)
        return None, None

    async def query(self, query: Query) -> Result:
        result = Result(modo_execucao="api_oficial")
        started = monotonic()
        try:
            async with asyncio.timeout(self.settings.query_timeout_seconds):
                async with self._slots:
                    await self._collect(query, result)
        except PortalError as exc:
            result.codigo, result.mensagem = exc.code, str(exc)
        except TimeoutError:
            result.codigo, result.mensagem = "TEMPO_ESGOTADO", TIMEOUT_MESSAGE
        finally:
            result.duracao_ms = round((monotonic() - started) * 1000)
        if result.codigo:
            result.status = "parcial" if result.pessoa else "erro"
            for benefit in result.beneficios:
                if not benefit.completo and not benefit.erro:
                    benefit.erro = result.mensagem
        return result

    async def _collect(self, query: Query, result: Result):
        result.etapa = "consultando_pessoa"
        data, url = await self._person(query.termo)
        if not data:
            raise PortalError("NAO_ENCONTRADO", "Nenhuma pessoa encontrada para o CPF/NIS informado")
        programs = [program for program in PROGRAMS if data.get(program[0])]
        if query.beneficiario_programa_social and not programs:
            raise PortalError("NAO_ENCONTRADO", "A pessoa não consta como beneficiária de programa social")
        fields = flatten(data)
        received = ", ".join(name for _, name, _, _ in programs) or "nenhum programa social"
        result.pessoa = Person(
            nome=data.get("nome"), cpf=data.get("cpf"), nis=data.get("nis"),
            panorama=Snapshot(url=url, texto=f"{data.get('nome') or 'Pessoa'}: {received}", campos=fields),
        )

        result.etapa = "consultando_beneficios"
        for _, name, path, param in programs:
            benefit = Benefit(programa=name, url=self.public_url(path, {param: query.termo}), completo=False)
            result.beneficios.append(benefit)
            for page in range(1, self.settings.max_detail_pages + 1):
                params = {param: query.termo, "pagina": page}
                rows = await self._get(path, params) or []
                if not rows:
                    benefit.completo = True
                    break
                benefit.paginas.append(Snapshot(
                    url=self.public_url(path, params),
                    texto=f"{name} — página {page}: {len(rows)} registro(s)",
                    tabelas=[[flatten(row) for row in rows]],
                ))
            else:
                benefit.erro = f"Limite de {self.settings.max_detail_pages} páginas atingido (MAX_DETAIL_PAGES)"
        result.etapa = "concluida"
        result.status = "sucesso"
