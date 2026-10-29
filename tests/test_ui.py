import asyncio
from pathlib import Path

from playwright.async_api import async_playwright, expect

from app.models import HistoryItem, Result


def test_ui_search_error_download_export_and_mobile():
    async def scenario():
        static = Path(__file__).resolve().parents[1] / "app/static"
        result = Result(codigo="PORTAL_BLOQUEADO", mensagem="Portal exigiu verificação humana", etapa="acessando_busca", duracao_ms=900)
        previous = Result(status="sucesso", modo_execucao="api_oficial", mensagem="Consulta recuperada do histórico", duracao_ms=300)
        submissions = []
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch()
            try:
                page = await browser.new_page(viewport={"width": 1440, "height": 1050})
                errors = []
                page.on("pageerror", lambda error: errors.append(str(error)))

                async def route(request):
                    url = request.request.url
                    if url.endswith("/consultas") and request.request.method == "POST":
                        submissions.append(request.request.post_data_json)
                        await request.fulfill(status=502, json=result.model_dump(mode="json"))
                    elif "/consultas?" in url:
                        await request.fulfill(json=[HistoryItem(consulta_id=previous.consulta_id, consultado_em=previous.consultado_em,
                                                                termo="12345678901", nome="Pessoa Anterior", modo_execucao="api_oficial",
                                                                status="sucesso").model_dump(mode="json")])
                    elif url.endswith(f"/consultas/{previous.consulta_id}"):
                        await request.fulfill(json=previous.model_dump(mode="json"))
                    elif url.endswith("/integracoes/google/arquivar"):
                        await request.fulfill(status=503, json={"mensagem": "Configure o OAuth Google"})
                    elif url.endswith("/style.css"):
                        await request.fulfill(content_type="text/css", body=(static / "style.css").read_text())
                    elif url.endswith("/app.js"):
                        await request.fulfill(content_type="text/javascript", body=(static / "app.js").read_text())
                    elif url == "http://observa.test/":
                        await request.fulfill(content_type="text/html", body=(static / "index.html").read_text())
                    else:
                        await request.abort()

                await page.route("**/*", route)
                await page.goto("http://observa.test/")
                await expect(page.get_by_role("heading", name="Nova consulta")).to_be_visible()
                output = Path("outputs/ui")
                output.mkdir(parents=True, exist_ok=True)
                await page.screenshot(path=str(output / "desktop.png"), full_page=True)
                await page.get_by_label("Nome, CPF ou NIS", exact=True).fill("Pessoa Fictícia")
                await page.get_by_label("Apenas beneficiários de programa social").check()
                await page.get_by_role("button", name="Consultar portal").click()
                await expect(page.locator("#status")).to_have_text("Falha")
                await expect(page.locator("#message")).to_have_text(result.mensagem)
                assert submissions == [{"termo": "Pessoa Fictícia", "beneficiario_programa_social": True}]
                async with page.expect_download() as download:
                    await page.get_by_role("button", name="Baixar JSON").click()
                assert result.consulta_id in (await download.value).suggested_filename
                await page.get_by_role("button", name="Salvar no Google").click()
                await expect(page.locator("#export-status")).to_contain_text("Configure o OAuth Google")
                await page.get_by_role("button", name="Pessoa Anterior").click()
                await expect(page.locator("#status")).to_have_text("Concluída")
                await expect(page.locator("#message")).to_have_text(previous.mensagem)
                await expect(page.locator(".history-item.active")).to_have_count(1)
                await page.set_viewport_size({"width": 390, "height": 844})
                assert await page.evaluate("document.documentElement.scrollWidth <= innerWidth")
                await page.screenshot(path=str(output / "mobile.png"), full_page=True)
                assert errors == []
            finally:
                await browser.close()
    asyncio.run(scenario())
