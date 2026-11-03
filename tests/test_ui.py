import asyncio
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from playwright.async_api import async_playwright, expect

from app.models import HistoryItem, Result


def test_ui_search_error_download_export_and_mobile():
    async def scenario():
        static = Path(__file__).resolve().parents[1] / "app/static"
        result = Result(codigo="PORTAL_BLOQUEADO", mensagem="Portal exigiu verificação humana", etapa="acessando_busca", duracao_ms=900)
        previous = Result(status="sucesso", modo_execucao="api_oficial", mensagem="Consulta recuperada do histórico", duracao_ms=300)
        submissions = []
        imports, imported, task, searches, chats, forgotten = [], [], {}, [], [], []
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
                    elif url.endswith("/base-local"):
                        await request.fulfill(json={"programas": [], "importados": imported, "tarefa_atual": None,
                                                    "tarefas": [task] if task else []})
                    elif url.endswith("/base-local/importacoes") and request.request.method == "POST":
                        imports.append(request.request.post_data_json)
                        task.update(tarefa_id="t1", programa_nome="BPC", uf="AM", status="concluida", percentual=None,
                                    mensagem="1 de 1 mês(es) importado(s), 3 linhas.",
                                    meses=[{"mes": "202403", "status": "concluido", "linhas": 3, "mensagem": "3 linhas importadas"}])
                        imported.append({"programa": "bpc", "programa_nome": "BPC", "arquivo_mes": "202403", "uf": "AM",
                                         "linhas": 3, "importado_em": "2026-01-01T12:00:00Z"})
                        await request.fulfill(status=202, json=task)
                    elif "/base-local/pagamentos?" in url:
                        searches.append(parse_qs(urlsplit(url).query))
                        await request.fulfill(json={"pagina": 1, "por_pagina": 25, "tem_mais": True, "itens": [
                            {"programa": "bpc", "programa_nome": "BPC", "arquivo_mes": "202403", "nome": "MARIA DA SILVA",
                             "nis": "20643890445", "cpf": "***.085.106-**", "uf": "AM", "municipio": "MANAUS",
                             "mes_competencia": "202403", "mes_referencia": "202403", "valor": "1518.00"}]})
                    elif url.endswith("/chat/ferramentas"):
                        await request.fulfill(json={"modelo": "qwen2.5:7b", "memoria_turnos": 6, "ferramentas": [
                            {"nome": "consultar_pessoa", "descricao": "Consulta uma pessoa.", "no_chat": True,
                             "parametros": [{"nome": "termo", "tipo": "string", "obrigatorio": True, "descricao": ""}]},
                            {"nome": "importar_base_local", "descricao": "Importa um mês.", "no_chat": False, "parametros": []}]})
                    elif url.endswith("/chat") and request.request.method == "POST":
                        chats.append(request.request.post_data_json)
                        await request.fulfill(json={
                            "sessao_id": "sessao-teste-1", "modelo": "qwen2.5:7b", "memoria_turnos": len(chats),
                            "resposta": "O CPF vem **mascarado** para proteger a pessoa [1].",
                            "ferramentas": [{"nome": "consultar_pessoa", "argumentos": {"termo": "20643890445"}, "status": "sucesso"}],
                            "fontes": [{"n": 1, "titulo": "Por que o CPF aparece com asteriscos", "arquivo": "app/conhecimento/observa.md", "citada": True}]})
                    elif "/chat/" in url and request.request.method == "DELETE":
                        forgotten.append(url.rsplit("/", 1)[-1])
                        await request.fulfill(json={"esquecida": True})
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
                await page.get_by_role("button", name="Consultar ↗", exact=True).click()
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
                # Base local: escolher programa, ano, mês e UF e importar.
                await expect(page.locator("#base-status")).to_contain_text("Nenhum mês importado")
                await page.get_by_label("Programa", exact=True).select_option("bpc")
                await page.get_by_label("Ano", exact=True).select_option("2024")
                await page.get_by_label("Mês", exact=True).select_option("3")
                await expect(page.locator("#base-uf")).to_have_value("AM")
                await page.get_by_role("button", name="Baixar e importar").click()
                assert imports == [{"programa": "bpc", "ano": 2024, "mes": 3, "uf": "AM"}]
                await expect(page.locator("#task-status")).to_have_text("Concluída")
                await expect(page.locator(".base-item")).to_contain_text("BPC · 03/2024")
                # Pop-up: "Ver dados" abre já filtrado pelo mês e lista as parcelas.
                await page.get_by_role("button", name="Ver dados de BPC · 03/2024").click()
                dialog = page.get_by_role("dialog", name="Consultar dados importados")
                await expect(dialog).to_be_visible()
                await expect(dialog.get_by_label("Base importada")).to_have_value("bpc|202403")
                await expect(dialog.locator("tbody tr")).to_have_count(1)
                assert searches[-1] == {"pagina": ["1"], "por_pagina": ["25"], "programa": ["bpc"], "arquivo_mes": ["202403"]}
                await dialog.get_by_label("Nome ou NIS").fill("maria da")
                await dialog.get_by_label("Município").fill("manaus")
                await dialog.get_by_role("button", name="Pesquisar").click()
                await expect(dialog.locator("tbody")).to_contain_text("R$ 1.518,00")
                assert (searches[-1]["termo"], searches[-1]["municipio"]) == (["maria da"], ["manaus"])
                await dialog.get_by_role("button", name="Próxima").click()
                await expect(dialog.locator("#browse-page")).to_have_text("Página 1")
                assert searches[-1]["pagina"] == ["2"]
                await page.screenshot(path=str(output / "browse-desktop.png"))
                await page.set_viewport_size({"width": 390, "height": 844})
                await page.screenshot(path=str(output / "browse-mobile.png"))
                # "Ver panorama" fecha o pop-up e consulta a pessoa pelo NIS.
                await dialog.get_by_role("button", name="Ver panorama de MARIA DA SILVA").click()
                await expect(dialog).to_be_hidden()
                await expect(page.locator("#term")).to_have_value("20643890445")
                await expect(page.locator("#status")).to_have_text("Falha")
                assert submissions[-1] == {"termo": "20643890445", "beneficiario_programa_social": True}
                assert await page.evaluate("document.documentElement.scrollWidth <= innerWidth")
                # Mini chat: "i" lista as tools MCP; a resposta mostra tools usadas e fontes do RAG.
                await page.set_viewport_size({"width": 1440, "height": 900})
                await page.get_by_role("button", name="Assistente").click()
                chat = page.locator("#chat")
                await expect(chat).to_be_visible()
                await chat.get_by_role("button", name="Ferramentas MCP disponíveis").click()
                await expect(chat.locator("#chat-tools-list li")).to_have_count(2)
                await expect(chat.locator("#chat-tools-list")).to_contain_text("só MCP / front")
                await expect(chat.locator("#chat-model")).to_contain_text("qwen2.5:7b · 1 tools no chat")
                await chat.get_by_role("button", name="Ferramentas MCP disponíveis").click()
                await chat.get_by_label("Pergunta para o assistente").fill("Por que o CPF tem asteriscos?")
                await chat.get_by_label("Pergunta para o assistente").press("Enter")
                await expect(chat.locator(".chat-msg.assistant").last).to_contain_text("O CPF vem mascarado")
                await expect(chat.locator(".chat-msg.assistant strong").last).to_have_text("mascarado")
                await expect(chat.locator(".chat-used")).to_contain_text('consultar_pessoa(termo: "20643890445")')
                await expect(chat.locator(".cite")).to_have_text("1")
                assert chats == [{"mensagem": "Por que o CPF tem asteriscos?"}]
                await chat.get_by_role("button", name="Por que o CPF").first.wait_for(state="detached")
                await chat.get_by_label("Pergunta para o assistente").fill("E o NIS?")
                await chat.get_by_role("button", name="Enviar").click()
                await expect(chat.locator(".chat-msg.user")).to_have_count(2)
                # Mesma sessão = mesma memória.
                assert chats[-1] == {"mensagem": "E o NIS?", "sessao_id": "sessao-teste-1"}
                await page.screenshot(path=str(output / "chat-desktop.png"))
                await chat.get_by_role("button", name="Nova conversa").click()
                await expect(chat.locator(".chat-msg.user")).to_have_count(0)
                assert forgotten == ["sessao-teste-1"]
                await page.set_viewport_size({"width": 390, "height": 844})
                await chat.get_by_role("button", name="Ferramentas MCP disponíveis").click()
                await page.screenshot(path=str(output / "chat-mobile.png"))
                await chat.get_by_role("button", name="Fechar o assistente").click()
                await expect(chat).to_be_hidden()
                assert await page.evaluate("document.documentElement.scrollWidth <= innerWidth")
                await page.screenshot(path=str(output / "mobile.png"), full_page=True)
                assert errors == []
            finally:
                await browser.close()
    asyncio.run(scenario())
