"""Mini chat: RAG na documentação, memória de curto prazo e tools MCP chamadas no processo.

O Ollama é simulado com httpx.MockTransport: cada teste roteiriza o que o "modelo"
responde (pedir uma tool ou dar a resposta final) e confere o que chegou até ele.
"""
import json
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from app import base_local
from app.api import create_app
from app.base_local import BaseLocal
from app.chat import CHAT_TOOLS, Knowledge, compact, split_markdown
from app.config import Settings
from app.models import Person, Result, Snapshot

HEADERS = {"X-API-Key": "test-key"}


class StubLocal:
    def __init__(self, settings):
        self.calls = []

    async def start(self):
        pass

    async def close(self):
        pass

    async def query(self, query):
        self.calls.append(query.termo)
        return Result(status="sucesso", modo_execucao="dados_abertos",
                      pessoa=Person(nome="MARIA DA SILVA", nis=query.termo,
                                    panorama=Snapshot(url="https://x", texto="ok", campos={"Valor total": "R$ 800,00"})))

    async def find(self, query):
        return None

    async def save(self, query, result):
        pass


class FakeOllama:
    """Responde em ordem as mensagens roteirizadas e guarda cada pedido recebido."""

    def __init__(self, *replies):
        self.replies = list(replies)
        self.requests = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        self.requests.append(body)
        reply = self.replies.pop(0)
        if isinstance(reply, httpx.Response):
            return reply
        if isinstance(reply, Exception):
            raise reply
        return httpx.Response(200, json={"message": reply})


def tool_call(name, **arguments):
    return {"role": "assistant", "content": "", "tool_calls": [{"function": {"name": name, "arguments": arguments}}]}


def answer(text):
    return {"role": "assistant", "content": text}


@pytest.fixture
def searches(monkeypatch):
    seen = []

    def search(url, pesquisa):
        seen.append(pesquisa)
        return {"itens": [{"nome": "MARIA DA SILVA", "nis": "20643890445", "valor": "650.00"}],
                "pagina": 1, "por_pagina": 25, "tem_mais": False}

    monkeypatch.setattr(base_local, "search_payments", search)
    return seen


def make_client(fake: FakeOllama, **extra):
    settings = Settings(_env_file=None, api_key="test-key", database_url="postgresql://stub", **extra)
    return TestClient(create_app(settings, local_factory=StubLocal, history_factory=StubLocal,
                                 base_local_factory=BaseLocal, chat_transport=httpx.MockTransport(fake.handler)))


def ask(client, message, session=None):
    response = client.post("/chat", headers=HEADERS, json={"mensagem": message, **({"sessao_id": session} if session else {})})
    return response


def test_rag_finds_the_right_section():
    knowledge = Knowledge()
    best = knowledge.search("por que o cpf aparece com asteriscos?")
    assert best and "CPF" in best[0][1].titulo and best[0][1].arquivo == "app/conhecimento/observa.md"
    assert "Auxílio Brasil" in knowledge.search("qual programa substituiu o bolsa família em 2021")[0][1].texto
    assert knowledge.search("xyzzy") == [] and knowledge.search("") == []
    # README e docs também entram na base.
    assert {"README.md", "docs/mcp.md"} <= {chunk.arquivo for chunk in knowledge.chunks}


def test_markdown_chunks_keep_heading_path_and_skip_code(tmp_path):
    doc = tmp_path / "x.md"
    doc.write_text("# Guia\n\n## Importar\n\nTexto sobre importar meses do portal.\n\n```bash\n# não é título\n```\n"
                   "### Por UF\n\nFiltrar por UF reduz o tamanho da base local.\n", encoding="utf-8")
    chunks = split_markdown(doc, tmp_path)
    assert [c.titulo for c in chunks] == ["Guia › Importar", "Guia › Importar › Por UF"]
    assert "# não é título" in chunks[0].texto


def test_compact_cuts_long_tables_and_evidence():
    data = {"evidencia": {"base64": "x" * 1000}, "beneficios": [{"paginas": [
        {"tabelas": [[{"v": i} for i in range(20)]]} for _ in range(10)]}]}
    small = compact(data)
    assert small["evidencia"] is None
    pages = small["beneficios"][0]["paginas"]
    assert len(pages) == 7 and "mais 4" in pages[-1]["…"]
    assert len(pages[0]["tabelas"][0]) == 7 and "mais 14" in pages[0]["tabelas"][0][-1]["…"]


def test_tools_endpoint_lists_every_mcp_tool():
    with make_client(FakeOllama()) as client:
        assert client.get("/chat/ferramentas").status_code == 401
        data = client.get("/chat/ferramentas", headers=HEADERS).json()
        tools = {tool["nome"]: tool for tool in data["ferramentas"]}
        assert set(tools) == {"consultar_pessoa", "pesquisar_base_local", "listar_base_local", "importar_base_local",
                              "status_importacao", "cancelar_importacao"}
        assert {name for name, tool in tools.items() if tool["no_chat"]} == set(CHAT_TOOLS)
        termo = next(p for p in tools["consultar_pessoa"]["parametros"] if p["nome"] == "termo")
        assert termo["obrigatorio"] and termo["tipo"] == "string" and termo["descricao"]
        assert data["modelo"] == "qwen2.5:7b"


def test_chat_uses_tools_rag_and_short_term_memory(searches):
    fake = FakeOllama(
        tool_call("pesquisar_base_local", termo="SILVA", contem=True),
        tool_call("consultar_pessoa", termo="20643890445"),
        answer("Encontrei **MARIA DA SILVA** (NIS 20643890445), que recebeu R$ 800,00."),
        answer("O CPF vem mascarado para proteger a beneficiária [1]."),
    )
    with make_client(fake) as client:
        first = ask(client, "Procure alguém com sobrenome SILVA e diga quanto recebeu")
        assert first.status_code == 200, first.text
        data = first.json()
        assert data["resposta"].startswith("Encontrei **MARIA DA SILVA**")
        assert [(t["nome"], t["status"]) for t in data["ferramentas"]] == [("pesquisar_base_local", "sucesso"),
                                                                        ("consultar_pessoa", "sucesso")]
        assert searches[0].termo == "SILVA" and searches[0].contem
        assert client.app.state.dados_abertos.calls == ["20643890445"]
        # O modelo recebeu as tools de leitura e o resultado de cada tool.
        offered = {tool["function"]["name"] for tool in fake.requests[0]["tools"]}
        assert offered == set(CHAT_TOOLS)
        tool_messages = [m for m in fake.requests[2]["messages"] if m["role"] == "tool"]
        assert [m["tool_name"] for m in tool_messages] == ["pesquisar_base_local", "consultar_pessoa"]
        assert "20643890445" in tool_messages[0]["content"]

        session = data["sessao_id"]
        second = ask(client, "Por que o CPF dela aparece com asteriscos?", session).json()
        assert second["memoria_turnos"] == 2 and second["sessao_id"] == session
        # Memória: a pergunta e a resposta anteriores voltam ao modelo.
        sent = fake.requests[3]["messages"]
        assert sent[0]["role"] == "system"
        assert any(m["role"] == "user" and "sobrenome SILVA" in m["content"] for m in sent)
        assert any(m["role"] == "assistant" and "R$ 800,00" in m["content"] for m in sent)
        # RAG: o trecho certo entrou no prompt e volta como fonte citada.
        assert "Por que o CPF aparece com asteriscos" in sent[0]["content"]
        assert second["fontes"][0]["citada"] and "CPF" in second["fontes"][0]["titulo"]

        assert client.delete(f"/chat/{session}", headers=HEADERS).json()["esquecida"] is True
        assert client.app.state.chat.memory.sessions == {}


def test_memory_keeps_only_the_last_turns():
    fake = FakeOllama(*(answer(f"resposta {i}") for i in range(4)))
    with make_client(fake, chat_memory_turns=2) as client:
        session = ask(client, "pergunta 0").json()["sessao_id"]
        for i in range(1, 4):
            ask(client, f"pergunta {i}", session)
        remembered = [m["content"] for m in fake.requests[3]["messages"] if m["role"] == "user"]
        assert remembered == ["pergunta 1", "pergunta 2", "pergunta 3"]
        # Outra sessão não vê nada.
        other = FakeOllama(answer("oi"))
        client.app.state.chat.http._transport = httpx.MockTransport(other.handler)
        ask(client, "nova sessão")
        assert [m["role"] for m in other.requests[0]["messages"]] == ["system", "user"]


def test_chat_refuses_tools_outside_the_chat_list():
    fake = FakeOllama(tool_call("importar_base_local", programa="bpc", ano=2024), answer("Não posso importar daqui."))
    with make_client(fake) as client:
        data = ask(client, "importe o BPC de 2024").json()
        assert data["ferramentas"] == [{"nome": "importar_base_local", "argumentos": {"programa": "bpc", "ano": 2024},
                                        "status": "erro"}]
        assert client.app.state.base_local.tarefas == {}
        refused = [m for m in fake.requests[1]["messages"] if m["role"] == "tool"][0]
        assert "não está disponível no chat" in refused["content"]


def test_bad_tool_arguments_go_back_to_the_model():
    fake = FakeOllama(tool_call("consultar_pessoa", nome="Maria"), answer("Preciso do nome completo."))
    with make_client(fake) as client:
        data = ask(client, "consulte a Maria").json()
        assert data["ferramentas"][0]["status"] == "erro" and data["resposta"] == "Preciso do nome completo."


@pytest.mark.parametrize("reply, status, code", [
    (httpx.ConnectError("recusado"), 503, "CHAT_INDISPONIVEL"),
    (httpx.ReadTimeout("lento"), 504, "CHAT_TEMPO_ESGOTADO"),
    (httpx.Response(404, json={"error": "model not found"}), 503, "MODELO_AUSENTE"),
    (httpx.Response(400, json={"error": "llama2 does not support tools"}), 503, "MODELO_SEM_TOOLS"),
])
def test_ollama_errors_are_explained(reply, status, code):
    with make_client(FakeOllama(reply)) as client:
        response = ask(client, "oi")
        assert (response.status_code, response.json()["codigo"]) == (status, code)


def test_validation_and_disabled_chat():
    with make_client(FakeOllama()) as client:
        assert ask(client, "").status_code == 422
        assert client.post("/chat", headers=HEADERS, json={"mensagem": "oi", "sessao_id": "../x"}).status_code == 422
    with make_client(FakeOllama(), chat_enabled=False) as client:
        response = ask(client, "oi")
        assert (response.status_code, response.json()["codigo"]) == (503, "CHAT_DESATIVADO")


def test_knowledge_file_is_packaged():
    assert (Path(__file__).resolve().parents[1] / "app/conhecimento/observa.md").is_file()
