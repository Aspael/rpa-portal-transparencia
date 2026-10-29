"""Sobe a aplicação e os contêineres com um único comando.

    python run.py            # API e n8n em contêineres (docker compose)
    python run.py --local    # API no Python local, n8n em contêiner
    python run.py down       # para e remove os contêineres

Usa apenas a biblioteca padrão: funciona antes mesmo de criar o .venv.
Ctrl+C encerra a aplicação e para os contêineres.
"""
import argparse
import os
import shutil
import subprocess
import sys
import time
import urllib.request
import webbrowser
from pathlib import Path

ROOT = Path(__file__).resolve().parent
WORKFLOW_NAME = "RPA Portal da Transparência | Drive + Sheets"
WORKFLOW_FILE = "/workflows/n8n-consulta-google.json"


def load_env():
    """Lê o .env para descobrir portas; variáveis já definidas têm prioridade."""
    env_file = ROOT / ".env"
    if not env_file.is_file():
        example = ROOT / ".env.example"
        if example.is_file():
            shutil.copy(example, env_file)
            print("Criado .env a partir de .env.example.")
    values = {}
    if env_file.is_file():
        for line in env_file.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, value = line.split("=", 1)
                values[key.strip()] = value.strip().strip("'\"")
    values.update(os.environ)
    return values


def compose(*args, env=None, check=True, capture=False):
    if not shutil.which("docker"):
        sys.exit("Docker não encontrado. Instale o Docker Desktop e deixe-o aberto.")
    return subprocess.run(
        ["docker", "compose", *args], cwd=ROOT, env=env, check=check,
        text=True, capture_output=capture,
    )


def wait_http(url, timeout=180, label=""):
    deadline = time.monotonic() + timeout
    print(f"Aguardando {label or url}...", end="", flush=True)
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=5):
                print(" ok")
                return True
        except Exception:
            print(".", end="", flush=True)
            time.sleep(2)
    print(" tempo esgotado")
    return False


def import_workflow_once():
    """Importa o workflow só se ainda não existir (o JSON não tem id fixo)."""
    listed = compose("exec", "-T", "n8n", "n8n", "list:workflow", check=False, capture=True)
    if WORKFLOW_NAME in (listed.stdout or ""):
        return
    print("Importando o workflow no n8n...")
    compose("exec", "-T", "n8n", "n8n", "import:workflow", f"--input={WORKFLOW_FILE}", check=False)


def ensure_chromium(env):
    browsers = Path(env.setdefault("PLAYWRIGHT_BROWSERS_PATH", str(ROOT / ".browsers")))
    if not browsers.is_absolute():
        browsers = ROOT / browsers
        env["PLAYWRIGHT_BROWSERS_PATH"] = str(browsers)
    if not any(browsers.glob("chromium*")):
        print("Instalando o Chromium do Playwright...")
        subprocess.run([sys.executable, "-m", "playwright", "install", "chromium"], env=env, check=True)


def up(args, settings):
    api_port = settings.get("HOST_PORT", "8000")
    n8n_port = settings.get("N8N_HOST_PORT", "5678")
    env = dict(os.environ)
    api = None

    if args.local:
        try:
            import uvicorn  # noqa: F401
        except ImportError:
            sys.exit("Dependências ausentes. Ative o .venv e rode: pip install -e .")
        ensure_chromium(env)
        # O n8n no contêiner alcança a API do computador por host.docker.internal.
        env["RPA_API_URL"] = f"http://host.docker.internal:{api_port}"
        api = subprocess.Popen(
            [sys.executable, "-m", "uvicorn", "app.api:app", "--host", args.host, "--port", api_port],
            cwd=ROOT, env=env,
        )
        # O histórico usa o Postgres do contêiner pela porta publicada em 127.0.0.1.
        # DATABASE_URL do .env tem prioridade (o próprio Settings o lê).
        if not settings.get("DATABASE_URL"):
            env["DATABASE_URL"] = (f"postgresql://{settings.get('POSTGRES_USER', 'observa')}:"
                                   f"{settings.get('POSTGRES_PASSWORD', 'observa')}@localhost:"
                                   f"{settings.get('POSTGRES_HOST_PORT', '5432')}/{settings.get('POSTGRES_DB', 'observa')}")
        compose("up", "-d", "--no-deps", "postgres", "n8n", env=env)
    else:
        compose("up", "-d", *([] if args.no_build else ["--build"]), env=env)

    try:
        if not wait_http(f"http://127.0.0.1:{api_port}/health", label="API"):
            raise SystemExit("A API não respondeu. Veja: docker compose logs api")
        if wait_http(f"http://127.0.0.1:{n8n_port}/healthz", label="n8n"):
            import_workflow_once()

        print(f"\nAPI:  http://127.0.0.1:{api_port}  (Swagger em /docs)")
        print(f"n8n:  http://localhost:{n8n_port}")
        print("Ctrl+C para encerrar.\n")
        if not args.no_browser:
            webbrowser.open(f"http://127.0.0.1:{api_port}")

        if api:
            api.wait()
        else:
            compose("logs", "-f", "--tail", "20", check=False)
    except KeyboardInterrupt:
        pass
    finally:
        print("\nEncerrando...")
        if api and api.poll() is None:
            api.terminate()
            api.wait(timeout=15)
        compose("stop", check=False)


def main():
    parser = argparse.ArgumentParser(description="Sobe a API e o n8n juntos.")
    parser.add_argument("acao", nargs="?", choices=["up", "down"], default="up")
    parser.add_argument("--local", action="store_true", help="API no Python local; só o n8n em contêiner")
    parser.add_argument("--host", default="127.0.0.1", help="Interface da API no modo --local")
    parser.add_argument("--no-build", action="store_true", help="Não reconstruir a imagem da API")
    parser.add_argument("--no-browser", action="store_true", help="Não abrir o navegador")
    args = parser.parse_args()

    settings = load_env()
    if args.acao == "down":
        compose("down")
    else:
        up(args, settings)


if __name__ == "__main__":
    main()
