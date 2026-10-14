import asyncio
import base64
import os
import re
import sys
from time import monotonic
from contextlib import suppress

from playwright.async_api import (
    Browser, Error as PlaywrightError, Page,
    TimeoutError as PlaywrightTimeoutError, async_playwright,
)

from app.config import Settings
from app.extraction import ORIGIN, SEARCH_URL, benefit_links, person_fields, portal_url, snapshot
from app.models import Benefit, Diagnostic, Evidence, Person, Query, Result

TIMEOUT_MESSAGE = "Não foi possível retornar os dados no tempo de resposta solicitado"
PERSON_PATH = re.compile(r"/(?:busca/)?pessoa-fisica/\d+(?:-[^/?#]+)?(?:[/?#]|$)")


def _has_display() -> bool:
    """Linux sem servidor gráfico (Docker, VPS) só consegue rodar headless."""
    if not sys.platform.startswith("linux"):
        return True
    return bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))


class PortalError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


class Robot:
    def __init__(self, settings: Settings):
        self.settings = settings
        self._slots = asyncio.Semaphore(settings.max_concurrent)
        self._playwright = None
        self._browser: Browser | None = None

    async def start(self):
        self._playwright = await async_playwright().start()
        try:
            self._browser = await self._playwright.chromium.launch(
                headless=self.settings.headless or not _has_display(),
                # Em contêiner, /dev/shm costuma ser pequeno e não há GPU.
                args=["--disable-dev-shm-usage", "--disable-gpu"],
            )
        except BaseException:
            await self._playwright.stop()
            raise

    async def close(self):
        try:
            if self._browser:
                await self._browser.close()
        finally:
            if self._playwright:
                await self._playwright.stop()

    async def query(self, query: Query) -> Result:
        result = Result()
        started = monotonic()
        context = None
        page = None
        try:
            # O prazo inclui a espera por uma vaga, evitando filas sem limite de tempo.
            async with asyncio.timeout(self.settings.query_timeout_seconds):
                async with self._slots:
                    result.etapa = "abrindo_navegador"
                    if not self._browser:
                        raise PortalError("INDISPONIVEL", "Navegador não inicializado")
                    context = await self._browser.new_context(locale="pt-BR", viewport={"width": 1440, "height": 1000})
                    context.set_default_timeout(self.settings.navigation_timeout_ms)
                    page = await context.new_page()
                    await self._collect(page, query, result)
        except PortalError as exc:
            result.codigo, result.mensagem = exc.code, str(exc)
        except (TimeoutError, PlaywrightTimeoutError):
            result.codigo, result.mensagem = "TEMPO_ESGOTADO", TIMEOUT_MESSAGE
        except PlaywrightError:
            result.codigo, result.mensagem = "FALHA_NAVEGACAO", "Não foi possível navegar no portal ou interagir com sua página"
        finally:
            if result.codigo and page and not page.is_closed():
                with suppress(PlaywrightError, TimeoutError):
                    async with asyncio.timeout(5):
                        result.diagnostico = Diagnostic(url=page.url, titulo=await page.title())
                        result.diagnostico.evidencia = Evidence(
                            base64=base64.b64encode(await page.screenshot(timeout=3000)).decode("ascii")
                        )
            if context:
                with suppress(PlaywrightError):
                    await context.close()
            result.duracao_ms = round((monotonic() - started) * 1000)
        if result.codigo:
            result.status = "parcial" if result.pessoa else "erro"
            for benefit in result.beneficios:
                if not benefit.completo and not benefit.erro:
                    benefit.erro = result.mensagem
        return result

    async def _check_block(self, page: Page):
        # AWS WAF pode devolver HTTP 202 com título/corpo vazios. O DOM
        # identifica esse caso antes da espera pelo campo de busca.
        if await page.locator('#challenge-container, #captcha-container').count():
            raise PortalError("PORTAL_BLOQUEADO", "O portal apresentou um desafio antirrobô (AWS WAF/CAPTCHA)")
        text = (await page.locator("body").inner_text()).casefold()
        title = (await page.title()).casefold()
        if any(word in title + " " + text for word in (
            "human verification", "verify that you're not a robot", "verifique que você é humano",
            "access denied", "acesso negado", "captcha",
        )):
            raise PortalError("PORTAL_BLOQUEADO", "O portal exigiu verificação humana ou bloqueou o acesso automatizado")

    async def _goto(self, page: Page, url: str, ready_selector: str | None = None):
        if not portal_url(url):
            raise PortalError("LINK_INVALIDO", "Link fora do Portal da Transparência")
        response = await page.goto(url, wait_until="domcontentloaded")
        # Dá tempo à verificação automática normal do próprio portal; não
        # resolve CAPTCHA nem injeta cookies/tokens para contorná-la.
        if self.settings.waf_wait_ms and await page.locator('#challenge-container').count():
            with suppress(PlaywrightTimeoutError):
                await page.locator('#challenge-container').wait_for(state="detached", timeout=self.settings.waf_wait_ms)
        await self._check_block(page)
        if ready_selector:
            try:
                await page.locator(ready_selector).first.wait_for(state="attached")
            except PlaywrightTimeoutError:
                await self._check_block(page)
                raise
            # O elemento real da página confirma o fim da navegação normal.
            await self._check_block(page)
            return
        if response and response.status >= 400:
            raise PortalError("PORTAL_INDISPONIVEL", f"Portal respondeu HTTP {response.status}")

    async def _wait(self, page: Page, predicate):
        # Polling condicionado ao estado, com prazo global em query().
        while True:
            await self._check_block(page)
            if await predicate():
                return
            await asyncio.sleep(0.2)

    async def _dismiss_cookies(self, page: Page):
        button = page.get_by_role("button", name=re.compile(r"rejeitar", re.I))
        if await button.count() and await button.first.is_visible():
            await button.first.click()

    async def _open_search(self, page: Page):
        # Segue o percurso do portal, preservando o contexto entre as páginas.
        await self._goto(page, ORIGIN + "/", 'a[href="/pessoa/visao-geral"]')
        await self._dismiss_cookies(page)
        people = page.locator('a[href="/pessoa/visao-geral"]').first
        # O link do cartão fica visually-hidden-focusable. Enter é a ativação
        # acessível normal e evita clicar no cartão que o sobrepõe.
        await people.focus()
        await people.press("Enter")
        await page.wait_for_url("**/pessoa/visao-geral", wait_until="domcontentloaded")
        await self._check_block(page)
        await self._dismiss_cookies(page)
        await page.get_by_role("link", name=re.compile(r"^Busca de Pessoa Física")).click()
        await page.get_by_placeholder(re.compile(r"Busque por Nome.*CPF", re.I)).wait_for(state="visible")
        await self._check_block(page)

    async def _expand_panorama(self, page: Page):
        buttons = page.locator('#main-content .br-accordion button[aria-controls], main .br-accordion button[aria-controls]')
        for button in await buttons.all():
            target_id = await button.get_attribute("aria-controls")
            target = page.locator(f'[id="{target_id}"]')
            if await target.count() and not await target.is_visible():
                await button.click()
                await target.wait_for(state="visible")
        await self._settle_tables(page)

    async def _collect(self, page: Page, query: Query, result: Result):
        result.etapa = "acessando_busca"
        await self._open_search(page)
        await self._dismiss_cookies(page)
        search = page.get_by_placeholder(re.compile(r"Busque por Nome.*CPF", re.I))
        if not await search.count():
            search = page.get_by_role("textbox", name=re.compile(r"Nome.*CPF.*NIS", re.I))
        await search.first.fill(query.termo)
        result.etapa = "consultando"
        if query.beneficiario_programa_social:
            label = page.locator('label[for="beneficiarioProgramaSocial"]')
            if await label.count():
                if not await label.is_visible():
                    await page.get_by_role("button", name=re.compile("Refine a Busca", re.I)).click()
                if not await page.locator("#beneficiarioProgramaSocial").is_checked():
                    await label.click()
                if not await page.locator("#beneficiarioProgramaSocial").is_checked():
                    raise PortalError("FILTRO_NAO_APLICADO", "Não foi possível selecionar o filtro social")
            else:
                await page.get_by_label(re.compile(r"Beneficiário de Programa Social", re.I)).check()
        # Enter envia o formulário visível. O botão Consultar fica recolhido e
        # seu aria-label é diferente do texto visual no portal atual.
        await search.first.press("Enter")

        async def search_ready():
            if await page.locator(".loading-busca:visible").count():
                return False
            # Ignora os registros iniciais que já estavam na tela antes da busca.
            term_marker = page.locator("#infoTermo strong")
            if await term_marker.count() and (await term_marker.inner_text()).strip() != query.termo:
                return False
            text = await page.locator("body").inner_text()
            if re.search(r"Foram encontrados\s+0\s+resultados", text, re.I):
                message = TIMEOUT_MESSAGE if query.is_document else f"Foram encontrados 0 resultados para o termo {query.termo}"
                raise PortalError("NAO_ENCONTRADO", message)
            if TIMEOUT_MESSAGE in text:
                raise PortalError("TEMPO_ESGOTADO", TIMEOUT_MESSAGE)
            if PERSON_PATH.search(page.url):
                return True
            return bool(await self._person_link(page))

        await self._wait(page, search_ready)
        result.etapa = "coletando_panorama"
        if not PERSON_PATH.search(page.url):
            url = await self._person_link(page)
            links = page.locator('a[href*="/pessoa-fisica/"]')
            for link in await links.all():
                if portal_url(await link.get_attribute("href"), page.url) == url:
                    await link.click()
                    break
        await self._wait(page, lambda: page.get_by_text(re.compile("Panorama da relação da pessoa")).first.is_visible())
        await self._dismiss_cookies(page)
        await self._expand_panorama(page)
        panorama = await snapshot(page)
        fields = person_fields(panorama.texto, panorama.campos)
        if not fields["nome"]:
            raise PortalError("LAYOUT_ALTERADO", "Não foi possível identificar o nome na página de panorama")
        result.pessoa = Person(panorama=panorama, **fields)
        result.evidencia = Evidence(base64=base64.b64encode(await page.screenshot(full_page=True)).decode("ascii"))
        links = await benefit_links(page)
        # Não confundir ausência de seletor com ausência de benefícios.
        if not links and re.search(r"aux[ií]lio brasil|aux[ií]lio emergencial|bolsa fam[ií]lia", panorama.texto, re.I):
            raise PortalError("LAYOUT_ALTERADO", "Benefício encontrado, mas seu link de detalhes não foi identificado")
        for name, url in links:
            result.etapa = "coletando_beneficios"
            benefit = Benefit(programa=name, url=url, completo=False)
            result.beneficios.append(benefit)
            try:
                await self._details(page, benefit)
                benefit.completo = True
            except (PortalError, PlaywrightError) as exc:
                benefit.completo = False
                benefit.erro = str(exc) if isinstance(exc, PortalError) else (
                    TIMEOUT_MESSAGE if isinstance(exc, PlaywrightTimeoutError)
                    else "Falha ao navegar ou interagir com os detalhes"
                )
        incomplete = any(not b.completo for b in result.beneficios)
        result.status = "parcial" if incomplete else "sucesso"
        if incomplete:
            result.codigo, result.mensagem = "DETALHES_INCOMPLETOS", "Um ou mais benefícios não foram coletados integralmente"
        else:
            result.etapa = "concluida"

    async def _person_link(self, page: Page) -> str | None:
        for href in await page.locator('a[href*="/pessoa-fisica/"]').evaluate_all("els => els.map(a => a.getAttribute('href'))"):
            url = portal_url(href, page.url)
            if url and PERSON_PATH.search(url):
                return url
        return None

    async def _settle_tables(self, page: Page):
        async def settled():
            processing = page.locator(".dataTables_processing:visible, .dt-processing:visible, [aria-busy=true]:visible, .loading:visible, .loading-grande:visible")
            if await processing.count():
                return False
            for table in await page.locator("table:visible").all():
                if await table.locator("tbody").count() and not await table.locator("tbody tr").count():
                    return False
            return True
        await self._wait(page, settled)

    async def _details(self, page: Page, benefit: Benefit):
        await self._goto(page, benefit.url)
        await self._dismiss_cookies(page)
        await self._settle_tables(page)
        previous = None
        for _ in range(self.settings.max_detail_pages):
            current = await snapshot(page)
            if current.texto == previous:
                raise PortalError("PAGINACAO_REPETIDA", "A paginação não avançou")
            benefit.paginas.append(current)
            previous = current.texto
            next_control = page.locator('.paginate_button.next, .dt-paging-button.next').first
            if not await next_control.count():
                next_control = page.get_by_role("button", name=re.compile(r"^(Próxim[ao]|Seguinte|Next|›|»)$", re.I)).first
            if not await next_control.count():
                next_control = page.get_by_role("link", name=re.compile(r"^(Próxim[ao]|Seguinte|Next|›|»)$", re.I)).first
            if not await next_control.count() or not await next_control.is_visible():
                return
            disabled = await next_control.evaluate("el => el.disabled || el.getAttribute('aria-disabled') === 'true' || !!el.closest('.disabled')")
            if disabled:
                return
            # No portal atual, `.paginate_button.next` é um <li> e a ação fica
            # no <button> interno. Outras versões usam o próprio botão/link.
            nested_action = next_control.locator("button, a").first
            action = nested_action if await nested_action.count() and await nested_action.is_visible() else next_control
            await action.click()
            await self._wait(page, lambda: self._content_changed(page, previous))
            await self._settle_tables(page)
        raise PortalError("LIMITE_PAGINAS", "Limite de páginas atingido; detalhes incompletos")

    async def _content_changed(self, page: Page, previous: str) -> bool:
        return (await snapshot(page)).texto != previous
