"""Chat no terminal: um modelo local do Ollama consulta o Observa pela tool MCP ``consultar_pessoa``.

    export MCP_URL=http://127.0.0.1:8000/mcp
    export MCP_API_KEY=SUA_CHAVE
    export OLLAMA_MODEL=qwen2.5:7b
    python scripts/chat_ollama.py

O Ollama não fala MCP: este script lista as tools do servidor MCP, entrega ao modelo
(tool calling), executa as chamadas pedidas em /mcp e devolve o resultado ao modelo
até ele dar a resposta final. Só depende de httpx.
"""
import json
import os
import sys
from itertools import count

import httpx

MCP_URL = os.environ.get("MCP_URL", "http://127.0.0.1:8000/mcp")
MCP_API_KEY = os.environ.get("MCP_API_KEY", "")
OLLAMA_HOST = os.environ.get("OLLAMA_HOST", "http://127.0.0.1:11434").rstrip("/")
OLLAMA_MODEL = os.environ.get("OLLAMA_MODEL", "qwen2.5:7b")
SYSTEM = ("Você ajuda a consultar pagamentos do Bolsa Família e programas relacionados (dados do Portal da "
          "Transparência). Use a tool consultar_pessoa com o nome completo ou o NIS informado. Responda em "
          "português, resumindo programas, meses e valores; se a consulta der erro, explique a mensagem.")


class Mcp:
    """Cliente mínimo de MCP Streamable HTTP (o servidor do Observa é sem sessão e responde JSON)."""

    def __init__(self, client: httpx.Client):
        self.client = client
        self.ids = count(1)

    def request(self, method: str, params: dict | None = None):
        body = {"jsonrpc": "2.0", "id": next(self.ids), "method": method, "params": params or {}}
        response = self.client.post(MCP_URL, json=body)
        if response.status_code == 401:
            sys.exit("O /mcp recusou a chave: defina MCP_API_KEY com o API_KEY da API.")
        response.raise_for_status()
        data = response.json()
        if "error" in data:
            raise RuntimeError(f"MCP {method}: {data['error'].get('message')}")
        return data["result"]

    def notify(self, method: str):
        self.client.post(MCP_URL, json={"jsonrpc": "2.0", "method": method})


def tool_result_text(result: dict) -> str:
    if result.get("structuredContent") is not None:
        return json.dumps(result["structuredContent"], ensure_ascii=False)
    return "\n".join(part.get("text", "") for part in result.get("content", []) if part.get("type") == "text")


def main():
    headers = {"Accept": "application/json, text/event-stream"}
    if MCP_API_KEY:
        headers["X-API-Key"] = MCP_API_KEY
    with httpx.Client(headers=headers, timeout=300) as mcp_http, httpx.Client(timeout=600) as ollama:
        mcp = Mcp(mcp_http)
        try:
            mcp.request("initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                       "clientInfo": {"name": "observa-chat-ollama", "version": "1.0.0"}})
            mcp.notify("notifications/initialized")
            tools = mcp.request("tools/list")["tools"]
        except httpx.HTTPError as exc:
            sys.exit(f"Não foi possível acessar o MCP em {MCP_URL} ({type(exc).__name__}). A API está rodando?")
        ollama_tools = [{"type": "function", "function": {"name": t["name"], "description": t.get("description", ""),
                                                          "parameters": t.get("inputSchema") or t.get("input_schema")}}
                        for t in tools]
        print(f"Modelo {OLLAMA_MODEL} · tools: {', '.join(t['name'] for t in tools)} · Ctrl+C ou 'sair' encerra\n")
        messages = [{"role": "system", "content": SYSTEM}]
        while True:
            try:
                question = input("você> ").strip()
            except (EOFError, KeyboardInterrupt):
                print()
                return
            if question.lower() in ("sair", "exit", "quit"):
                return
            if not question:
                continue
            messages.append({"role": "user", "content": question})
            while True:
                try:
                    response = ollama.post(f"{OLLAMA_HOST}/api/chat", json={
                        "model": OLLAMA_MODEL, "messages": messages, "tools": ollama_tools, "stream": False})
                    response.raise_for_status()
                except httpx.HTTPError as exc:
                    sys.exit(f"Falha ao falar com o Ollama em {OLLAMA_HOST} ({exc}). Rode 'ollama serve' e "
                             f"'ollama pull {OLLAMA_MODEL}'.")
                message = response.json()["message"]
                messages.append(message)
                calls = message.get("tool_calls") or []
                if not calls:
                    print(f"modelo> {message.get('content', '').strip()}\n")
                    break
                for call in calls:
                    name, arguments = call["function"]["name"], call["function"].get("arguments") or {}
                    if isinstance(arguments, str):
                        arguments = json.loads(arguments or "{}")
                    print(f"  ↳ {name}({json.dumps(arguments, ensure_ascii=False)})")
                    try:
                        content = tool_result_text(mcp.request("tools/call", {"name": name, "arguments": arguments}))
                    except (RuntimeError, httpx.HTTPError) as exc:
                        content = json.dumps({"erro": str(exc)}, ensure_ascii=False)
                    messages.append({"role": "tool", "tool_name": name, "content": content})


if __name__ == "__main__":
    main()
