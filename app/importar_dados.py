"""Baixa um arquivo mensal dos dados abertos do Portal da Transparência e importa no PostgreSQL.

Exemplos:
    python -m app.importar_dados novo-bolsa-familia 202501 --uf AM
    python -m app.importar_dados bpc 202501 --uf AM --limite 1000
    python -m app.importar_dados --listar

Os arquivos ficam em DADOS_ABERTOS_DIR e são reaproveitados; reimportar o mesmo
programa e mês substitui as linhas anteriores. Sem --uf, um mês do Bolsa Família
tem ~20 milhões de linhas (vários GB no banco).

As mesmas funções são usadas pela API (``/base-local``) e pelas tools MCP, que
rodam a importação em segundo plano com progresso e cancelamento.
"""
import argparse
import csv
import io
import re
import sys
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from threading import Event
from typing import Callable

import httpx
import psycopg
from sqlalchemy import create_engine

from app.config import Settings
from app.dados_abertos import PROGRAMAS, Base, Importacao, download_url, normalize_name


# coluna da tabela -> cabeçalhos possíveis no CSV (sem acento, minúsculas)
COLUMNS = {
    "mes_competencia": ("mes competencia", "mes disponibilizacao"),
    "mes_referencia": ("mes referencia",),
    "uf": ("uf",),
    "municipio": ("nome municipio",),
    "cpf": ("cpf favorecido", "cpf beneficiario"),
    "nis": ("nis favorecido", "nis beneficiario"),
    "nome": ("nome favorecido", "nome beneficiario"),
    "valor": ("valor parcela", "valor beneficio"),
}
REQUIRED = ("mes_competencia", "nome", "valor")
COPY_COLUMNS = ("programa", "arquivo_mes", *COLUMNS, "nome_busca")

# progress(texto, percentual): o CLI imprime; a API guarda na tarefa de importação.
Progress = Callable[[str, int | None], None]


def print_progress(text: str, percent: int | None = None):
    print(text, flush=True)


class ImportacaoErro(Exception):
    """Falha esperada (arquivo ausente, formato desconhecido); a mensagem vai para quem pediu."""


class ImportacaoCancelada(ImportacaoErro):
    pass


def check_cancel(cancel: Event | None):
    if cancel and cancel.is_set():
        raise ImportacaoCancelada("Importação cancelada")


def plain_url(url: str) -> str:
    """URL libpq para o psycopg, mesmo se DATABASE_URL vier com o driver do SQLAlchemy."""
    return re.sub(r"^postgres(ql)?(\+\w+)?://", "postgresql://", url)


def column_map(header: list[str]) -> dict[str, int]:
    names = [normalize_name(h).lower() for h in header]
    found = {}
    for column, options in COLUMNS.items():
        index = next((names.index(o) for o in options if o in names), None)
        if index is not None:
            found[column] = index
    missing = [c for c in REQUIRED if c not in found]
    if missing:
        raise ValueError(f"Formato de arquivo não reconhecido: faltam {missing}. Cabeçalho: {header}")
    return found


def parse_row(row: list[str], columns: dict[str, int]) -> dict[str, str | None] | None:
    """Converte uma linha do CSV; devolve None para linhas sem titular identificável."""
    value = {c: (row[i].strip() or None) if i < len(row) else None for c, i in columns.items()}
    for c in COLUMNS:
        value.setdefault(c, None)
    nome = value["nome"]
    if not nome or nome.startswith("***"):  # ex.: "*** TITULAR MENOR DE 16 ANOS ***"
        return None
    nis = re.sub(r"\D", "", value["nis"] or "")
    value["nis"] = nis if len(nis) == 11 and nis.strip("0") else None
    if value["valor"]:
        value["valor"] = value["valor"].replace(".", "").replace(",", ".")
    value["nome_busca"] = normalize_name(nome)
    return value


def download(programa: str, mes: str, directory: Path, force: bool,
             progress: Progress = print_progress, cancel: Event | None = None) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / f"{mes}_{programa}.zip"
    if target.exists() and not force:
        progress(f"Usando arquivo já baixado: {target.name}", 100)
        return target
    partial = target.with_suffix(".part")
    url = download_url(programa, mes)
    progress(f"Baixando {url}", 0)
    try:
        with httpx.stream("GET", url, follow_redirects=True, timeout=httpx.Timeout(60, read=300)) as response:
            if response.status_code == 404:
                raise ImportacaoErro(f"Arquivo não encontrado no portal: {programa} {mes}. "
                                     "Confira o programa e o mês (o portal publica com alguns meses de atraso).")
            if response.status_code >= 400:
                raise ImportacaoErro(f"O portal respondeu HTTP {response.status_code} para {programa} {mes}.")
            if "zip" not in response.headers.get("content-type", ""):
                raise ImportacaoErro(f"O portal não devolveu um ZIP para {programa} {mes} "
                                     f"(Content-Type: {response.headers.get('content-type')}).")
            size, done, step = int(response.headers.get("content-length", 0)), 0, 0
            with partial.open("wb") as stream:
                for chunk in response.iter_bytes(1 << 20):
                    check_cancel(cancel)
                    stream.write(chunk)
                    done += len(chunk)
                    if size and done * 10 // size > step:
                        step = done * 10 // size
                        progress(f"  {step * 10}% ({done >> 20} MB)", step * 10)
    except httpx.HTTPError as exc:
        partial.unlink(missing_ok=True)
        raise ImportacaoErro(f"Falha de rede ao baixar {programa} {mes}: {exc}") from exc
    except BaseException:
        partial.unlink(missing_ok=True)
        raise
    partial.replace(target)
    return target


def rows_from_zip(path: Path):
    with zipfile.ZipFile(path) as archive:
        member = next((n for n in archive.namelist() if n.lower().endswith(".csv")), None)
        if not member:
            raise ImportacaoErro(f"Nenhum CSV dentro de {path.name}")
        with archive.open(member) as raw:
            yield from csv.reader(io.TextIOWrapper(raw, encoding="latin-1", newline=""), delimiter=";")


def import_file(database_url: str, programa: str, mes: str, path: Path, uf: str | None, limit: int | None,
                progress: Progress = print_progress, cancel: Event | None = None) -> int:
    engine = create_engine(re.sub(r"^postgresql://", "postgresql+psycopg://", plain_url(database_url)))
    Base.metadata.create_all(engine)
    engine.dispose()

    try:
        reader = rows_from_zip(path)
        columns = column_map(next(reader))
    except (zipfile.BadZipFile, StopIteration, ValueError) as exc:
        raise ImportacaoErro(f"Arquivo inválido ({path.name}): {exc or 'vazio'}") from exc
    if uf and "uf" not in columns:
        raise ImportacaoErro("Este arquivo não tem coluna UF; importe sem filtrar por UF")
    count = 0
    with psycopg.connect(plain_url(database_url)) as conn:
        with conn.cursor() as cur:
            # Mesma transação: se a importação falhar ou for cancelada, as linhas antigas continuam.
            cur.execute("DELETE FROM pagamentos WHERE programa = %s AND arquivo_mes = %s", (programa, mes))
            with cur.copy(f"COPY pagamentos ({', '.join(COPY_COLUMNS)}) FROM STDIN") as copy:
                for number, row in enumerate(reader):
                    if not number % 10_000:
                        check_cancel(cancel)
                    value = parse_row(row, columns)
                    if not value or (uf and value["uf"] != uf):
                        continue
                    copy.write_row((programa, mes, *(value[c] for c in COLUMNS), value["nome_busca"]))
                    count += 1
                    if count % 200_000 == 0:
                        progress(f"  {count:,} linhas".replace(",", "."), None)
                    if limit and count >= limit:
                        break
            cur.execute(
                f"INSERT INTO {Importacao.__tablename__} (programa, arquivo_mes, uf, linhas, importado_em) "
                "VALUES (%s, %s, %s, %s, %s) ON CONFLICT (programa, arquivo_mes) "
                "DO UPDATE SET uf = EXCLUDED.uf, linhas = EXCLUDED.linhas, importado_em = EXCLUDED.importado_em",
                (programa, mes, uf, count, datetime.now(timezone.utc)),
            )
            cur.execute("ANALYZE pagamentos")
    return count


def download_and_import(settings: Settings, programa: str, mes: str, uf: str | None = None, limit: int | None = None,
                        force: bool = False, progress: Progress = print_progress, cancel: Event | None = None) -> int:
    """Baixa (ou reaproveita) o ZIP do mês e importa no PostgreSQL. Usado pelo CLI, pela API e pelo MCP."""
    path = download(programa, mes, settings.dados_abertos_dir, force, progress, cancel)
    progress(f"Importando {PROGRAMAS[programa]} {mes}{f' (UF {uf})' if uf else ''}...", None)
    return import_file(settings.database_url.get_secret_value(), programa, mes, path, uf, limit, progress, cancel)


def fetch_imports(database_url: str) -> list[dict]:
    """Meses já importados, do mais recente para o mais antigo."""
    with psycopg.connect(plain_url(database_url), connect_timeout=5) as conn:
        try:
            rows = conn.execute("SELECT programa, arquivo_mes, uf, linhas, importado_em "
                                "FROM importacoes ORDER BY arquivo_mes DESC, programa").fetchall()
        except psycopg.errors.UndefinedTable:
            rows = []
    return [{"programa": p, "programa_nome": PROGRAMAS.get(p, p), "arquivo_mes": m, "uf": uf,
             "linhas": linhas, "importado_em": quando} for p, m, uf, linhas, quando in rows]


def remove_import(database_url: str, programa: str, mes: str) -> bool:
    """Apaga as parcelas de um programa/mês da base local; devolve False se o mês não estava importado."""
    with psycopg.connect(plain_url(database_url), connect_timeout=5) as conn:
        try:
            deleted = conn.execute("DELETE FROM importacoes WHERE programa = %s AND arquivo_mes = %s",
                                   (programa, mes)).rowcount
        except psycopg.errors.UndefinedTable:
            return False
        conn.execute("DELETE FROM pagamentos WHERE programa = %s AND arquivo_mes = %s", (programa, mes))
    return bool(deleted)


def list_imports(database_url: str):
    rows = fetch_imports(database_url)
    if not rows:
        print("Nenhum arquivo importado ainda.")
    for row in sorted(rows, key=lambda r: (r["programa"], r["arquivo_mes"])):
        print(f"{row['programa']:26} {row['arquivo_mes']}  UF {row['uf'] or 'todas':5} {row['linhas']:>12,} linhas  "
              f"{row['importado_em']:%d/%m/%Y %H:%M}".replace(",", "."))


def main():
    parser = argparse.ArgumentParser(description="Importa dados abertos do Portal da Transparência no PostgreSQL")
    parser.add_argument("programa", nargs="?", choices=sorted(PROGRAMAS))
    parser.add_argument("mes", nargs="?", help="AAAAMM, ex.: 202501")
    parser.add_argument("--uf", help="Importa só uma UF (recomendado: reduz de milhões para milhares de linhas)")
    parser.add_argument("--limite", type=int, help="Para depois de N linhas (útil para testar)")
    parser.add_argument("--baixar-de-novo", action="store_true", help="Ignora o ZIP já baixado")
    parser.add_argument("--listar", action="store_true", help="Mostra os meses já importados")
    args = parser.parse_args()

    settings = Settings()
    if not (settings.database_url and settings.database_url.get_secret_value()):
        parser.error("Configure DATABASE_URL (no docker compose ela já vem pronta: rode dentro do contêiner api)")
    database_url = settings.database_url.get_secret_value()
    if args.listar:
        list_imports(database_url)
        return
    if not args.programa or not args.mes or not re.fullmatch(r"20\d{2}(0[1-9]|1[0-2])", args.mes):
        parser.error("Informe o programa e o mês no formato AAAAMM, ex.: novo-bolsa-familia 202501")
    uf = args.uf.upper() if args.uf else None
    if uf and not re.fullmatch(r"[A-Z]{2}", uf):
        parser.error("--uf deve ser a sigla do estado, ex.: AM")

    try:
        count = download_and_import(settings, args.programa, args.mes, uf, args.limite, args.baixar_de_novo)
    except ImportacaoErro as exc:
        raise SystemExit(str(exc)) from exc
    print(f"Concluído: {count:,} linhas importadas.".replace(",", "."))
    if not count:
        sys.exit(1)


if __name__ == "__main__":
    main()
