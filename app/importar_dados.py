"""Baixa um arquivo mensal dos dados abertos do Portal da Transparência e importa no PostgreSQL.

Exemplos:
    python -m app.importar_dados novo-bolsa-familia 202501 --uf AM
    python -m app.importar_dados bpc 202501 --uf AM --limite 1000
    python -m app.importar_dados --listar

Os arquivos ficam em DADOS_ABERTOS_DIR e são reaproveitados; reimportar o mesmo
programa e mês substitui as linhas anteriores. Sem --uf, um mês do Bolsa Família
tem ~20 milhões de linhas (vários GB no banco).
"""
import argparse
import csv
import io
import re
import sys
import zipfile
from datetime import datetime, timezone
from pathlib import Path

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


def download(programa: str, mes: str, directory: Path, force: bool) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / f"{mes}_{programa}.zip"
    if target.exists() and not force:
        print(f"Usando arquivo já baixado: {target}")
        return target
    partial = target.with_suffix(".part")
    url = download_url(programa, mes)
    print(f"Baixando {url}")
    with httpx.stream("GET", url, follow_redirects=True, timeout=httpx.Timeout(60, read=300)) as response:
        if response.status_code == 404:
            raise SystemExit(f"Arquivo não encontrado no portal: {programa} {mes}. Confira o programa e o mês.")
        response.raise_for_status()
        if "zip" not in response.headers.get("content-type", ""):
            raise SystemExit(f"O portal não devolveu um ZIP para {programa} {mes} (Content-Type: {response.headers.get('content-type')}).")
        size, done, step = int(response.headers.get("content-length", 0)), 0, 0
        with partial.open("wb") as stream:
            for chunk in response.iter_bytes(1 << 20):
                stream.write(chunk)
                done += len(chunk)
                if size and done * 10 // size > step:
                    step = done * 10 // size
                    print(f"  {step * 10}% ({done >> 20} MB)", flush=True)
    partial.replace(target)
    return target


def rows_from_zip(path: Path):
    with zipfile.ZipFile(path) as archive:
        member = next((n for n in archive.namelist() if n.lower().endswith(".csv")), None)
        if not member:
            raise SystemExit(f"Nenhum CSV dentro de {path}")
        with archive.open(member) as raw:
            yield from csv.reader(io.TextIOWrapper(raw, encoding="latin-1", newline=""), delimiter=";")


def import_file(database_url: str, programa: str, mes: str, path: Path, uf: str | None, limit: int | None) -> int:
    engine = create_engine(re.sub(r"^postgresql://", "postgresql+psycopg://", plain_url(database_url)))
    Base.metadata.create_all(engine)
    engine.dispose()

    reader = rows_from_zip(path)
    columns = column_map(next(reader))
    if uf and "uf" not in columns:
        raise SystemExit("Este arquivo não tem coluna UF; rode sem --uf")
    count = 0
    with psycopg.connect(plain_url(database_url)) as conn:
        with conn.cursor() as cur:
            # Mesma transação: se a importação falhar, as linhas antigas continuam.
            cur.execute("DELETE FROM pagamentos WHERE programa = %s AND arquivo_mes = %s", (programa, mes))
            with cur.copy(f"COPY pagamentos ({', '.join(COPY_COLUMNS)}) FROM STDIN") as copy:
                for row in reader:
                    value = parse_row(row, columns)
                    if not value or (uf and value["uf"] != uf):
                        continue
                    copy.write_row((programa, mes, *(value[c] for c in COLUMNS), value["nome_busca"]))
                    count += 1
                    if count % 200_000 == 0:
                        print(f"  {count:,} linhas".replace(",", "."), flush=True)
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


def list_imports(database_url: str):
    with psycopg.connect(plain_url(database_url)) as conn:
        try:
            rows = conn.execute("SELECT programa, arquivo_mes, coalesce(uf, 'todas'), linhas, importado_em "
                                "FROM importacoes ORDER BY programa, arquivo_mes").fetchall()
        except psycopg.errors.UndefinedTable:
            rows = []
    if not rows:
        print("Nenhum arquivo importado ainda.")
    for programa, mes, uf, linhas, quando in rows:
        print(f"{programa:26} {mes}  UF {uf:5} {linhas:>12,} linhas  {quando:%d/%m/%Y %H:%M}".replace(",", "."))


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

    path = download(args.programa, args.mes, settings.dados_abertos_dir, args.baixar_de_novo)
    print(f"Importando {PROGRAMAS[args.programa]} {args.mes}{f' (UF {uf})' if uf else ''}...")
    count = import_file(database_url, args.programa, args.mes, path, uf, args.limite)
    print(f"Concluído: {count:,} linhas importadas.".replace(",", "."))
    if not count:
        sys.exit(1)


if __name__ == "__main__":
    main()
