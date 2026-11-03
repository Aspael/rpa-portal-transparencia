"""Integração com Chromium real e rotas interceptadas; não acessa dados reais."""
import asyncio
import base64
from urllib.parse import parse_qs, urlparse

import pytest

from app.config import Settings
from app.extraction import ORIGIN
from app.models import Query
from app.robot import Robot, TIMEOUT_MESSAGE


SEARCH = '''<html><main>
<form onsubmit="event.preventDefault(); location.href='/results?term='+encodeURIComponent(document.querySelector('#term').value)+'&social='+document.querySelector('#social').checked">
<label>Nome, CPF ou NIS<input id="term" placeholder="Busque por Nome, Nis ou CPF (123456789-00)"></label>
<label><input type="checkbox" id="social">Beneficiário de Programa Social</label>
<button onclick="location.href='/results?term='+encodeURIComponent(document.querySelector('#term').value)+'&social='+document.querySelector('#social').checked">Consultar</button>
</form></main></html>'''

HOME = '<main><a href="/pessoa/visao-geral">Pessoas Físicas e Jurídicas</a></main>'
OVERVIEW = '<main><a href="/pessoa-fisica/busca/lista">Busca de Pessoa Física</a></main>'


def panorama(identifier, benefits=True):
    cards = "".join(f'<section><h3>{name}</h3><a href="/{slug}/beneficiario/{identifier}">Detalhar</a></section>'
                    for name, slug in [("Auxílio Brasil", "auxilio-brasil"), ("Auxílio Emergencial", "auxilio-emergencial"), ("Bolsa Família", "bolsa-familia")]) if benefits else ""
    return f'''<html><main><h1>Pessoa Física</h1><dl><dt>Nome</dt><dd>PESSOA FICTÍCIA {identifier}</dd>
    <dt>CPF</dt><dd>***.123.456-**</dd><dt>Localidade</dt><dd>TESTE - XX</dd></dl>
    <h2>Panorama da relação da pessoa com o Governo Federal</h2>{cards}</main></html>'''


def detail(page=1):
    return f'''<html><main><h1>Detalhes do benefício</h1><table><thead><tr><th>Mês</th><th>Valor</th></tr></thead>
    <tbody><tr><td>0{page}/2024</td><td>R$ 600,00</td></tr></tbody></table>
    <ul class="pagination"><li class="paginate_button next {'disabled' if page == 2 else ''}">
      <button onclick="location.search='?page=2'" aria-label="Próximo">Próximo</button>
    </li></ul></main></html>'''


class FixtureRobot(Robot):
    def __init__(self, settings, blocked=False, detail_blocked=False, stalled=False, benefits=True):
        super().__init__(settings)
        self.blocked, self.detail_blocked = blocked, detail_blocked
        self.stalled, self.benefits = stalled, benefits
        self.requests = []
        # blocked="solvable": as N primeiras páginas pedem verificação e se recarregam
        # sozinhas, simulando a pessoa que resolve a verificação na janela.
        self.blocks_left = 2

    async def _visible(self):
        # Nos testes, a "janela" é o mesmo Chromium headless com as rotas interceptadas.
        return self._browser

    async def start(self):
        await super().start()
        original = self._browser.new_context

        async def new_context(**kwargs):
            context = await original(**kwargs)
            await context.route("**/*", self.route)
            return context

        self._browser.new_context = new_context

    async def route(self, route):
        url = urlparse(route.request.url)
        self.requests.append(route.request.url)
        query = parse_qs(url.query)
        if self.blocked == "solvable" and self.blocks_left:
            self.blocks_left -= 1
            html = ("<html><title>Human Verification</title><body>CAPTCHA"
                    "<script>setTimeout(() => location.reload(), 500)</script></body></html>")
        elif self.blocked == "waf":
            html = '<html><title></title><body><div id="challenge-container"></div></body></html>'
        elif (self.blocked and self.blocked != "solvable") or (self.detail_blocked and "/beneficiario/" in url.path):
            html = "<html><title>Human Verification</title><body>CAPTCHA</body></html>"
        elif url.path == "/":
            html = HOME
        elif url.path == "/pessoa/visao-geral":
            html = OVERVIEW
        elif url.path == "/pessoa-fisica/busca/lista":
            html = SEARCH
        elif url.path == "/results":
            term = query.get("term", [""])[0]
            if self.stalled:
                html = "<body>Carregando...</body>"
            elif term in ("Inexistente", "00000000000"):
                html = "<body>Foram encontrados 0 resultados para o termo</body>"
            else:
                identifier = "2" if term == "Outra Pessoa" else "1"
                html = f'<main><a href="/pessoa-fisica/{identifier}">Pessoa</a><a href="/pessoa-fisica/99">Segunda</a></main>'
        elif url.path.startswith("/pessoa-fisica/"):
            html = panorama(url.path.rsplit("/", 1)[1], self.benefits)
        else:
            html = detail(int(query.get("page", ["1"])[0]))
        await route.fulfill(status=200, content_type="text/html; charset=utf-8", body=html)


async def collect(query, **options):
    settings = options.pop("settings", Settings(query_timeout_seconds=15))
    robot = FixtureRobot(settings, **options)
    await robot.start()
    try:
        result = await robot.query(query)
        assert len(robot._browser.contexts) == 0
        return result, robot.requests
    finally:
        await robot.close()


@pytest.mark.parametrize("term", ["Maria Teste", "12345678900"])
def test_success_with_screenshot_and_all_benefit_pages(term):
    result, _ = asyncio.run(collect(Query(termo=term)))
    assert result.status == "sucesso", result.mensagem
    assert result.pessoa.nome == "PESSOA FICTÍCIA 1"
    assert base64.b64decode(result.evidencia.base64).startswith(b"\x89PNG\r\n\x1a\n")
    assert len(result.beneficios) == 3
    for benefit in result.beneficios:
        assert benefit.completo
        assert len(benefit.paginas) == 2
        assert benefit.paginas[1].tabelas[0][0]["Mês"] == "02/2024"


def test_social_filter():
    result, requests = asyncio.run(collect(Query(termo="Silva", beneficiario_programa_social=True)))
    assert result.status == "sucesso"
    assert any("social=true" in url for url in requests)


@pytest.mark.parametrize("term,message", [("Inexistente", "Foram encontrados 0 resultados para o termo Inexistente"), ("00000000000", TIMEOUT_MESSAGE)])
def test_not_found(term, message):
    result, _ = asyncio.run(collect(Query(termo=term)))
    assert result.codigo == "NAO_ENCONTRADO"
    assert result.mensagem == message


def test_captcha_is_explicit():
    result, _ = asyncio.run(collect(Query(termo="Teste"), blocked=True))
    assert result.status == "erro"
    assert result.codigo == "PORTAL_BLOQUEADO"
    assert result.pessoa is None


def test_assisted_mode_waits_for_the_person_and_continues(monkeypatch):
    # 1º bloqueio: execução headless encerra; 2º: a janela assistida espera a "pessoa" e segue.
    monkeypatch.setattr("app.robot._has_display", lambda: True)
    settings = Settings(_env_file=None, query_timeout_seconds=15, assisted_mode=True, assisted_wait_seconds=10)
    result, _ = asyncio.run(collect(Query(termo="Maria Teste"), blocked="solvable", settings=settings))
    assert result.status == "sucesso", result.mensagem
    assert result.modo_execucao == "assistido"
    assert result.pessoa.nome == "PESSOA FICTÍCIA 1"


def test_assisted_mode_gives_up_after_the_wait(monkeypatch):
    monkeypatch.setattr("app.robot._has_display", lambda: True)
    settings = Settings(_env_file=None, query_timeout_seconds=15, assisted_mode=True, assisted_wait_seconds=2)
    result, _ = asyncio.run(collect(Query(termo="Teste"), blocked=True, settings=settings))
    assert (result.codigo, result.modo_execucao) == ("PORTAL_BLOQUEADO", "assistido")
    assert "não foi concluída" in result.mensagem


def test_assisted_mode_without_screen_explains_why(monkeypatch):
    monkeypatch.setattr("app.robot._has_display", lambda: False)
    settings = Settings(_env_file=None, query_timeout_seconds=15, assisted_mode=True)
    result, _ = asyncio.run(collect(Query(termo="Teste"), blocked=True, settings=settings))
    assert (result.codigo, result.modo_execucao) == ("PORTAL_BLOQUEADO", "autonomo")
    assert "Modo assistido indisponível" in result.mensagem


def test_blank_waf_page_is_detected_and_has_diagnostic():
    result, _ = asyncio.run(collect(Query(termo="Teste"), blocked="waf", settings=Settings(waf_wait_ms=0)))
    assert result.codigo == "PORTAL_BLOQUEADO"
    assert result.etapa == "acessando_busca"
    assert result.diagnostico.evidencia.base64
    assert result.evidencia is None


def test_partial_details():
    result, _ = asyncio.run(collect(Query(termo="Teste"), detail_blocked=True))
    assert result.status == "parcial"
    assert result.evidencia
    assert all(not b.completo for b in result.beneficios)


def test_no_benefits():
    result, _ = asyncio.run(collect(Query(termo="Teste"), benefits=False))
    assert result.status == "sucesso"
    assert result.beneficios == []


def test_page_limit_does_not_report_success():
    result, _ = asyncio.run(collect(Query(termo="Teste"), settings=Settings(max_detail_pages=1)))
    assert result.status == "parcial"
    assert all(not b.completo for b in result.beneficios)


def test_timeout_closes_context():
    result, _ = asyncio.run(collect(Query(termo="Teste"), stalled=True, settings=Settings(query_timeout_seconds=1)))
    assert result.codigo == "TEMPO_ESGOTADO"


def test_concurrent_contexts_and_ids_are_isolated():
    async def scenario():
        robot = FixtureRobot(Settings(max_concurrent=2))
        await robot.start()
        try:
            results = await asyncio.gather(robot.query(Query(termo="Maria Teste")), robot.query(Query(termo="Outra Pessoa")))
            assert {r.pessoa.nome for r in results} == {"PESSOA FICTÍCIA 1", "PESSOA FICTÍCIA 2"}
            assert len({r.consulta_id for r in results}) == 2
            assert len(robot._browser.contexts) == 0
        finally:
            await robot.close()
    asyncio.run(scenario())
