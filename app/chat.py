"""Mini chat do front: um modelo local do Ollama responde usando as tools MCP do Observa.

- **Tools**: chamadas no mesmo processo (``MCPServer.call_tool``), sem passar por HTTP.
  Só as tools de leitura vão para o modelo (``CHAT_TOOLS``); importar e cancelar ficam
  com o cartão Base local e com clientes MCP.
- **RAG**: a pergunta busca trechos da documentação (``app/conhecimento``, ``docs`` e
  README) por BM25; os melhores entram no prompt e voltam como ``fontes``.
- **Memória de curto prazo**: as últimas ``CHAT_MEMORY_TURNS`` trocas de cada sessão
  ficam na memória do processo por ``CHAT_MEMORY_MINUTES`` sem uso.
"""
import asyncio
import json
import logging
import math
import re
import unicodedata
from collections import Counter, OrderedDict
from dataclasses import dataclass, field
from pathlib import Path
from time import monotonic
from typing import Any
from uuid import uuid4

import httpx
from pydantic import BaseModel, Field

from app.config import Settings

log = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parent.parent
KNOWLEDGE = (Path(__file__).resolve().parent / "conhecimento", ROOT / "docs", ROOT / "README.md")
# Tools que o modelo do chat pode chamar; as demais aparecem no "i" como disponíveis só via MCP/front.
CHAT_TOOLS = ("consultar_pessoa", "pesquisar_base_local", "listar_base_local", "status_importacao")
MAX_TOOL_ROUNDS = 5
MAX_SESSIONS = 200
TOOL_RESULT_CHARS = 12_000   # o que o modelo recebe de cada tool
MEMORY_TOOL_CHARS = 1_500    # o que fica na memória para as próximas perguntas

SYSTEM = """Você é o assistente do Observa, que consulta pagamentos do Bolsa Família e de programas \
relacionados (Novo Bolsa Família, Auxílio Brasil, Auxílio Emergencial, BPC) publicados pelo Portal da \
Transparência do governo federal.

Regras:
- Para dados de pessoas, parcelas, valores ou meses importados, use as ferramentas. Nunca invente nomes, NIS ou valores.
- consultar_pessoa precisa do nome completo ou do NIS. Se a pessoa só souber parte do nome, use pesquisar_base_local \
(contem=true para sobrenome) e depois consulte pelo NIS encontrado.
- Se uma ferramenta devolver status "erro", explique a mensagem com suas palavras e sugira o próximo passo.
- O CPF é publicado mascarado (***.123.456-**); não tente descobrir os dígitos ocultos.
- Para dúvidas sobre o sistema, os programas ou como usar, use o contexto da documentação abaixo e cite as fontes como [1], [2].
- Responda em português do Brasil, de forma curta e direta; valores em reais (R$ 1.234,56) e meses como MM/AAAA."""


# ---------------------------------------------------------------- RAG ----
STOPWORDS = set("""a o as os um uma uns umas de do da dos das no na nos nas em por para com sem que se e ou é ao aos
à às como mais mas foi ser ter tem são está isso esse essa este esta qual quais quando onde quem eu você me meu minha
seu sua pelo pela pelos pelas sobre entre até também já não sim ele ela eles elas lhe muito pode posso fazer faz the
and or of to in is are for on with by an be this that it as at from can you your use""".split())


def fold(text: str) -> str:
    text = unicodedata.normalize("NFD", text.lower())
    return "".join(c for c in text if unicodedata.category(c) != "Mn")


def tokens(text: str) -> list[str]:
    return [t for t in re.findall(r"[a-z0-9_]+", fold(text)) if len(t) > 1 and t not in STOPWORDS]


@dataclass
class Chunk:
    titulo: str
    arquivo: str
    texto: str
    termos: Counter = field(default_factory=Counter)


def split_markdown(path: Path, base: Path) -> list[Chunk]:
    """Um trecho por seção (##/###), com o caminho de títulos; seções longas viram vários trechos."""
    name = path.relative_to(base).as_posix() if path.is_relative_to(base) else path.name
    chunks, heads, buffer = [], [], []

    def flush():
        body = "\n".join(buffer).strip()
        buffer.clear()
        if len(body) < 40:
            return
        title = " › ".join(heads) or name
        parts, current = [], ""
        for paragraph in re.split(r"\n\s*\n", body):
            if current and len(current) + len(paragraph) > 1400:
                parts.append(current)
                current = ""
            current = f"{current}\n\n{paragraph}".strip()
        parts.append(current)
        chunks.extend(Chunk(title, name, part) for part in parts if part)

    in_code = False
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.startswith("```"):
            in_code = not in_code
        match = None if in_code else re.match(r"^(#{1,3})\s+(.*)", line)
        if match:
            flush()
            level = len(match.group(1))
            heads[:] = heads[:level - 1] + [match.group(2).strip()]
        else:
            buffer.append(line)
    flush()
    return chunks


class Knowledge:
    """Índice BM25 em memória da documentação. Montado uma vez, no primeiro uso."""

    def __init__(self, sources=KNOWLEDGE):
        self.chunks: list[Chunk] = []
        for source in sources:
            files = sorted(source.glob("*.md")) if source.is_dir() else [source] if source.is_file() else []
            for file in files:
                try:
                    self.chunks.extend(split_markdown(file, ROOT))
                except (OSError, UnicodeDecodeError):
                    log.warning("Não foi possível ler %s para o RAG", file)
        for chunk in self.chunks:
            chunk.termos = Counter(tokens(f"{chunk.titulo} {chunk.titulo} {chunk.texto}"))
        self.df = Counter(term for chunk in self.chunks for term in chunk.termos)
        self.avg = sum(sum(c.termos.values()) for c in self.chunks) / max(len(self.chunks), 1)

    def search(self, query: str, k: int = 3) -> list[tuple[float, Chunk]]:
        terms = set(tokens(query))
        if not terms or not self.chunks:
            return []
        n, k1, b = len(self.chunks), 1.4, 0.75
        scored = []
        for chunk in self.chunks:
            length = sum(chunk.termos.values())
            score = 0.0
            for term in terms:
                tf = chunk.termos.get(term)
                if tf:
                    idf = math.log(1 + (n - self.df[term] + 0.5) / (self.df[term] + 0.5))
                    score += idf * tf * (k1 + 1) / (tf + k1 * (1 - b + b * length / self.avg))
            if score > 0:
                scored.append((score, chunk))
        scored.sort(key=lambda item: item[0], reverse=True)
        # Descarta trechos muito mais fracos que o melhor: viram ruído no prompt de um modelo pequeno.
        return [item for item in scored[:k] if item[0] >= scored[0][0] * 0.35] if scored else []


# ------------------------------------------------------------- memória ---
@dataclass
class Session:
    turns: list[list[dict]] = field(default_factory=list)
    used: float = field(default_factory=monotonic)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


class Memory:
    def __init__(self, turns: int, minutes: float):
        self.max_turns, self.ttl = turns, minutes * 60
        self.sessions: OrderedDict[str, Session] = OrderedDict()

    def get(self, session_id: str) -> Session:
        self._expire()
        session = self.sessions.pop(session_id, None) or Session()
        session.used = monotonic()
        self.sessions[session_id] = session
        while len(self.sessions) > MAX_SESSIONS:
            self.sessions.popitem(last=False)
        return session

    def remember(self, session: Session, turn: list[dict]):
        session.turns.append(turn)
        # [-0:] seria a lista inteira; com 0 a memória fica desligada.
        session.turns[:] = session.turns[-self.max_turns:] if self.max_turns else []

    def forget(self, session_id: str) -> bool:
        return self.sessions.pop(session_id, None) is not None

    def _expire(self):
        now = monotonic()
        for key in [k for k, s in self.sessions.items() if now - s.used > self.ttl]:
            del self.sessions[key]


# --------------------------------------------------------------- chat ----
class ChatRequest(BaseModel):
    mensagem: str = Field(min_length=1, max_length=2000)
    sessao_id: str | None = Field(None, pattern=r"^[A-Za-z0-9_-]{8,64}$",
                                  description="Mesma sessão = mesma memória de curto prazo; vazio cria uma nova")


class ChatError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def compact(value: Any, rows: int = 6, pages: int = 6) -> Any:
    """Encolhe o JSON das tools para caber no contexto: corta tabelas e listas longas."""
    if isinstance(value, dict):
        out = {}
        for key, item in value.items():
            if key == "tabelas" and isinstance(item, list):
                out[key] = [table[:rows] + ([{"…": f"mais {len(table) - rows} linha(s)"}] if len(table) > rows else [])
                            for table in item]
            elif key == "paginas" and isinstance(item, list) and len(item) > pages:
                out[key] = [compact(p, rows, pages) for p in item[:pages]] + [{"…": f"mais {len(item) - pages} mês(es)"}]
            elif key in ("evidencia", "base64"):
                out[key] = None
            else:
                out[key] = compact(item, rows, pages)
        return out
    if isinstance(value, list):
        return [compact(item, rows, pages) for item in value]
    return value


def tool_text(result) -> str:
    data = getattr(result, "structured_content", None) or getattr(result, "structuredContent", None)
    if data is not None:
        return json.dumps(compact(data), ensure_ascii=False)
    return "\n".join(getattr(part, "text", "") for part in getattr(result, "content", []) or [])


def describe_tools(mcp_tools) -> list[dict]:
    """Ferramentas do MCP no formato do botão "i" do front."""
    out = []
    for tool in mcp_tools:
        schema = tool.input_schema or {}
        required = set(schema.get("required") or [])
        params = []
        for name, prop in (schema.get("properties") or {}).items():
            kinds = [p.get("type") for p in prop.get("anyOf", [prop]) if p.get("type") and p.get("type") != "null"]
            params.append({"nome": name, "tipo": "/".join(kinds) or "any", "obrigatorio": name in required,
                           "descricao": prop.get("description", "")})
        out.append({"nome": tool.name, "descricao": tool.description or "", "parametros": params,
                    "no_chat": tool.name in CHAT_TOOLS})
    return out


class Assistant:
    def __init__(self, settings: Settings, mcp, transport: httpx.AsyncBaseTransport | None = None):
        self.settings = settings
        self.mcp = mcp
        self.memory = Memory(settings.chat_memory_turns, settings.chat_memory_minutes)
        self.http = httpx.AsyncClient(base_url=settings.ollama_host.rstrip("/"), transport=transport,
                                      timeout=httpx.Timeout(settings.chat_timeout_seconds, connect=5))
        self._knowledge: Knowledge | None = None

    async def close(self):
        await self.http.aclose()

    @property
    def knowledge(self) -> Knowledge:
        if self._knowledge is None:
            self._knowledge = Knowledge()
        return self._knowledge

    async def tools(self) -> list[dict]:
        return describe_tools(await self.mcp.list_tools())

    def forget(self, session_id: str) -> bool:
        return self.memory.forget(session_id)

    async def ask(self, request: ChatRequest) -> dict:
        session_id = request.sessao_id or uuid4().hex
        session = self.memory.get(session_id)
        async with session.lock:
            return await self._answer(session_id, session, request.mensagem.strip())

    async def _answer(self, session_id: str, session: Session, question: str) -> dict:
        # RAG: a pergunta anterior ajuda em continuações curtas ("e o BPC?").
        previous = next((m["content"] for turn in reversed(session.turns) for m in turn if m["role"] == "user"), "")
        hits = self.knowledge.search(f"{question} {previous}" if len(tokens(question)) < 4 else question)
        context = "\n\n".join(f"[{i}] {chunk.titulo} ({chunk.arquivo})\n{chunk.texto}"
                              for i, (_, chunk) in enumerate(hits, 1))
        system = SYSTEM + (f"\n\nContexto da documentação:\n{context}" if context else "")

        all_tools = await self.mcp.list_tools()
        offered = [{"type": "function", "function": {"name": t.name, "description": t.description or "",
                                                     "parameters": t.input_schema}}
                   for t in all_tools if t.name in CHAT_TOOLS]
        history = [message for turn in session.turns for message in turn]
        turn = [{"role": "user", "content": question}]
        used = []
        for _ in range(MAX_TOOL_ROUNDS + 1):
            message = await self._chat([{"role": "system", "content": system}, *history, *turn], offered)
            calls = message.get("tool_calls") or []
            turn.append({"role": "assistant", "content": message.get("content") or "",
                         **({"tool_calls": calls} if calls else {})})
            if not calls:
                break
            for call in calls:
                name = call.get("function", {}).get("name", "")
                arguments = call.get("function", {}).get("arguments") or {}
                if isinstance(arguments, str):
                    try:
                        arguments = json.loads(arguments or "{}")
                    except json.JSONDecodeError:
                        arguments = {}
                content, status = await self._call_tool(name, arguments)
                used.append({"nome": name, "argumentos": arguments, "status": status})
                turn.append({"role": "tool", "tool_name": name, "content": content})
        else:
            turn.append({"role": "assistant", "content": "Precisei de ferramentas demais para essa pergunta. "
                                                         "Tente dividir em perguntas menores."})
        answer = turn[-1]["content"].strip() or "Não consegui formular uma resposta. Tente reformular a pergunta."
        turn[-1]["content"] = answer
        # Na memória, resultados de tools ficam resumidos: o suficiente para "e em março?".
        self.memory.remember(session, [m if m["role"] != "tool" else {**m, "content": m["content"][:MEMORY_TOOL_CHARS]}
                                       for m in turn])
        cited = {int(n) for n in re.findall(r"\[(\d)\]", answer)}
        return {
            "sessao_id": session_id,
            "resposta": answer,
            "ferramentas": used,
            "fontes": [{"n": i, "titulo": chunk.titulo, "arquivo": chunk.arquivo, "citada": i in cited}
                       for i, (_, chunk) in enumerate(hits, 1)],
            "memoria_turnos": len(session.turns),
            "modelo": self.settings.ollama_model,
        }

    async def _call_tool(self, name: str, arguments: dict) -> tuple[str, str]:
        if name not in CHAT_TOOLS:
            return json.dumps({"status": "erro", "mensagem": f"Ferramenta {name} não está disponível no chat."},
                              ensure_ascii=False), "erro"
        try:
            result = await self.mcp.call_tool(name, arguments)
        except Exception as exc:
            log.info("Tool %s falhou no chat: %s", name, exc)
            return json.dumps({"status": "erro", "mensagem": f"Argumentos inválidos para {name}: {exc}"},
                              ensure_ascii=False), "erro"
        text = tool_text(result)
        status = "erro" if getattr(result, "is_error", False) or '"status": "erro"' in text[:200] else "sucesso"
        if len(text) > TOOL_RESULT_CHARS:
            text = text[:TOOL_RESULT_CHARS] + " …(resultado cortado)"
        return text, status

    async def _chat(self, messages: list[dict], tools: list[dict]) -> dict:
        model = self.settings.ollama_model
        try:
            response = await self.http.post("/api/chat", json={
                "model": model, "messages": messages, "tools": tools, "stream": False,
                "options": {"temperature": 0.2}})
        except httpx.TimeoutException as exc:
            raise ChatError("CHAT_TEMPO_ESGOTADO", "O modelo demorou demais para responder. Tente de novo ou use um "
                                                   "modelo menor (OLLAMA_MODEL).") from exc
        except httpx.HTTPError as exc:
            raise ChatError("CHAT_INDISPONIVEL", f"Não foi possível falar com o Ollama em {self.settings.ollama_host}. "
                                                 "Rode 'ollama serve' ou ajuste OLLAMA_HOST.") from exc
        if response.status_code == 404:
            raise ChatError("MODELO_AUSENTE", f"O modelo {model} não está no Ollama. Rode 'ollama pull {model}' "
                                              "ou ajuste OLLAMA_MODEL.")
        if response.status_code == 400 and "tools" in response.text:
            raise ChatError("MODELO_SEM_TOOLS", f"O modelo {model} não suporta ferramentas. Use, por exemplo, "
                                                "qwen2.5, llama3.1 ou mistral-nemo.")
        if response.status_code >= 400:
            raise ChatError("CHAT_INDISPONIVEL", f"O Ollama respondeu HTTP {response.status_code}.")
        return response.json().get("message") or {}
