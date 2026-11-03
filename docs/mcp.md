# Servidor MCP e base local

O Observa expõe um servidor MCP em `/mcp` no mesmo processo da API (transporte Streamable HTTP, sem sessão, respostas em JSON). Ele é protegido pelo mesmo cabeçalho `X-API-Key` da API e pode ser desligado com `MCP_ENABLED=false`.

As tools cobrem dois usos:

- **consultar** uma pessoa (`consultar_pessoa`);
- **pesquisar** as parcelas já importadas por nome, NIS, município ou mês (`pesquisar_base_local`);
- **gerir a base local** de dados abertos: ver o que está importado, baixar e importar um mês ou um ano inteiro, acompanhar e cancelar (`listar_base_local`, `importar_base_local`, `status_importacao`, `cancelar_importacao`).

As tools de base local fazem exatamente o mesmo que as rotas `/base-local` da API e o cartão **03 · Base local** do front-end (inclusive o pop-up **Consultar dados importados**). Todas exigem `DATABASE_URL`; sem ela devolvem `BASE_LOCAL_DESATIVADA`.

## Conectar

```bash
# MCP Inspector
npx @modelcontextprotocol/inspector   # URL http://127.0.0.1:8000/mcp, header X-API-Key

# Claude Code
claude mcp add --transport http observa http://127.0.0.1:8000/mcp --header "X-API-Key: SUA_CHAVE"

# Ollama (terminal)
MCP_URL=http://127.0.0.1:8000/mcp MCP_API_KEY=SUA_CHAVE python scripts/chat_ollama.py
```

Chamada crua (JSON-RPC):

```bash
curl -s http://127.0.0.1:8000/mcp \
  -H "X-API-Key: SUA_CHAVE" -H "Accept: application/json, text/event-stream" -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"listar_base_local","arguments":{}}}'
```

O resultado de cada tool vem em `result.structuredContent` (e também como texto em `result.content`).

## Convenções

- Toda resposta das tools de base local tem `status` (`sucesso` ou `erro`). Em erro, vêm também `codigo` e `mensagem`; o modelo deve citar a mensagem ao usuário.
- `consultar_pessoa` devolve o [contrato de resultado](../README.md#result-contract) da API, igual a `POST /consultas`.
- Programas (`programa`): `novo-bolsa-familia` (2023+), `bolsa-familia-pagamentos` (até 2021), `auxilio-brasil` (nov/2021 – 2023), `auxilio-emergencial` (2020 – 2021), `bpc`.
- Meses usam o formato `AAAAMM` nas respostas (`arquivo_mes`, `mes`); nos pedidos, `ano` e `mes` são números.

## Tools

### `consultar_pessoa`

Consulta uma pessoa. Usa primeiro o histórico no PostgreSQL e depois a fonte de `QUERY_SOURCE` (por padrão, a base local).

| Parâmetro | Tipo | Padrão | Descrição |
|---|---|---|---|
| `termo` | string | obrigatório | nome completo ou NIS (11 dígitos); CPF só se não for ambíguo |
| `beneficiario_programa_social` | bool | `false` | filtro de beneficiário (modos robô/API) |
| `incluir_evidencia` | bool | `false` | inclui o screenshot em Base64; desligado para não lotar o contexto |
| `atualizar` | bool | `false` | ignora o histórico e consulta de novo (útil logo depois de importar um mês) |

Erros comuns: `ENTRADA_INVALIDA`, `NAO_ENCONTRADO`, `BASE_VAZIA`, `CPF_AMBIGUO`, `BASE_LOCAL_INDISPONIVEL`.

### `listar_base_local`

Sem parâmetros. Mostra o que já está na base e o que está sendo importado.

```json
{
  "status": "sucesso",
  "programas": [{"programa": "novo-bolsa-familia", "nome": "Novo Bolsa Família"}, "..."],
  "importados": [
    {"programa": "novo-bolsa-familia", "programa_nome": "Novo Bolsa Família", "arquivo_mes": "202501",
     "uf": "AM", "linhas": 612345, "importado_em": "2026-11-03T14:02:11Z"}
  ],
  "tarefa_atual": null,
  "tarefas": []
}
```

`uf: null` significa que o mês foi importado com todas as UFs. `tarefas` traz até 5 importações recentes desde que o servidor iniciou.

### `pesquisar_base_local`

Navega nas parcelas já importadas, uma linha por parcela. Serve para achar o nome completo ou o NIS de alguém antes de `consultar_pessoa`, ou para listar quem recebeu num município e mês. É a mesma pesquisa do pop-up **Consultar dados importados** do front.

| Parâmetro | Tipo | Padrão | Descrição |
|---|---|---|---|
| `termo` | string \| null | `null` | início do nome (`MARIA DA`; maiúsculas e acentos são ignorados) ou NIS com 11 dígitos |
| `programa` | string \| null | `null` | restringe a um programa |
| `arquivo_mes` | string \| null | `null` | restringe a um mês importado (`AAAAMM`, veja `listar_base_local`) |
| `uf` | string \| null | `null` | sigla da UF |
| `municipio` | string \| null | `null` | início do nome do município |
| `contem` | bool | `false` | procura o termo em qualquer parte do nome (`SILVA`); não usa índice, por isso é mais lento |
| `pagina` | int | `1` | 25 linhas por página |

Sem nenhum filtro, lista as parcelas em ordem alfabética.

```json
{"status": "sucesso", "pagina": 1, "por_pagina": 25, "tem_mais": true, "itens": [
  {"programa": "novo-bolsa-familia", "programa_nome": "Novo Bolsa Família", "arquivo_mes": "202501",
   "nome": "MARIA DA SILVA", "nis": "20643890445", "cpf": "***.085.106-**", "uf": "AM", "municipio": "MANAUS",
   "mes_competencia": "202501", "mes_referencia": "202412", "valor": "650.00"}
]}
```

`valor` vem como texto com ponto decimal (sem arredondar). A pesquisa tem limite de 20 s; se passar, devolve `PESQUISA_LENTA` (filtre por mês/UF ou desligue `contem`). Erros: `ENTRADA_INVALIDA` (NIS sem 11 dígitos, nome com menos de 2 letras, `contem` com menos de 3, mês fora de `AAAAMM`), `BASE_LOCAL_DESATIVADA`, `BASE_LOCAL_INDISPONIVEL`.

### `importar_base_local`

Baixa do Portal da Transparência o arquivo mensal (ZIP com CSV) e importa no PostgreSQL, **em segundo plano**. Volta na hora com a tarefa criada.

| Parâmetro | Tipo | Padrão | Descrição |
|---|---|---|---|
| `programa` | string | obrigatório | um dos programas acima |
| `ano` | int | obrigatório | 2013 em diante |
| `mes` | int \| null | `null` | 1 a 12; **omitido importa o ano inteiro**, um mês por vez, até o mês atual |
| `uf` | string \| null | `null` | sigla do estado (ex.: `AM`); recomendado |
| `limite` | int \| null | `null` | máximo de linhas por mês, para testes |

```json
{"status": "sucesso", "mensagem": "Importação iniciada; acompanhe com status_importacao.",
 "tarefa": {"tarefa_id": "4f1c…", "status": "na_fila", "meses": [{"mes": "202501", "status": "pendente"}], "...": "..."}}
```

Regras:

- **Uma importação por vez.** Um segundo pedido devolve `IMPORTACAO_EM_ANDAMENTO` com a tarefa que está rodando.
- Reimportar o mesmo programa/mês **substitui** as linhas anteriores (com ou sem UF). A troca é feita numa transação: se falhar ou for cancelada, as linhas antigas continuam.
- O ZIP fica em `DADOS_ABERTOS_DIR` e é reaproveitado na próxima vez.
- Tamanho: um mês do Bolsa Família tem ~360 MB zipado e ~20 milhões de linhas. Com `uf` o download é o mesmo, mas só as linhas do estado entram no banco.
- Num ano inteiro, cada mês tem seu próprio resultado. Meses que o portal ainda não publicou (ou fora do período do programa) terminam em `erro` com "Arquivo não encontrado", e os outros seguem; a tarefa fica `parcial`.

Erros: `ENTRADA_INVALIDA` (programa desconhecido, mês fora de 1–12, UF inválida, data no futuro), `IMPORTACAO_EM_ANDAMENTO`, `BASE_LOCAL_DESATIVADA`.

### `status_importacao`

| Parâmetro | Tipo | Padrão | Descrição |
|---|---|---|---|
| `tarefa_id` | string \| null | `null` | ID de `importar_base_local`; omitido mostra a importação atual ou a mais recente |

Devolve `{"status": "sucesso", "tarefa": {...}}` com o formato abaixo, ou `TAREFA_NAO_ENCONTRADA`.

### `cancelar_importacao`

| Parâmetro | Tipo | Descrição |
|---|---|---|
| `tarefa_id` | string | ID de `importar_base_local` |

Pede o cancelamento e devolve a tarefa. A importação para no próximo bloco (download a cada 1 MB, importação a cada 10 mil linhas); o mês em curso é desfeito, e meses já concluídos ficam na base.

## Tarefa de importação

| Campo | Descrição |
|---|---|
| `tarefa_id` | identificador (fica só na memória do processo; as 20 mais recentes são mantidas) |
| `programa`, `programa_nome`, `uf`, `limite` | o que foi pedido |
| `status` | `na_fila`, `em_andamento`, `concluida`, `parcial` (alguns meses falharam), `erro` (nenhum mês entrou), `cancelada` |
| `mes_atual` | `AAAAMM` em processamento |
| `percentual` | progresso do download do mês atual, quando o portal informa o tamanho |
| `mensagem` | última linha de progresso, ou o resumo final ("11 de 12 mês(es) importado(s), …") |
| `meses[]` | `{mes, status, linhas, mensagem}`; `status` em `pendente`, `baixando`, `importando`, `concluido`, `erro`, `cancelado` |
| `linhas` | total de linhas importadas na tarefa |
| `criada_em`, `terminada_em` | horários em UTC |

## Fluxo sugerido para um modelo

1. `consultar_pessoa` devolveu `BASE_VAZIA` ou `NAO_ENCONTRADO` → `listar_base_local` para ver os meses cobertos. Se o usuário só sabe parte do nome, `pesquisar_base_local` (com `contem=true` se for o sobrenome) e depois `consultar_pessoa` com o NIS encontrado.
2. Se o usuário pedir, `importar_base_local` com programa, ano, mês e UF.
3. `status_importacao` até `status` sair de `na_fila`/`em_andamento`.
4. `consultar_pessoa` de novo com `atualizar=true`.

As instruções do servidor MCP (`initialize`) já descrevem esse fluxo.

## Mesmas operações pela API HTTP

Todas com `X-API-Key`; documentadas no Swagger (`/docs`) na seção **Base local**.

| Método e rota | Equivale a | Respostas |
|---|---|---|
| `GET /base-local` | `listar_base_local` | 200; 503 `BASE_LOCAL_DESATIVADA`/`BASE_LOCAL_INDISPONIVEL` |
| `GET /base-local/pagamentos?termo=&programa=&arquivo_mes=&uf=&municipio=&contem=&pagina=&por_pagina=` | `pesquisar_base_local` (`por_pagina` até 100) | 200; 422 validação; 504 `PESQUISA_LENTA` |
| `POST /base-local/importacoes` | `importar_base_local` (corpo `{programa, ano, mes?, uf?, limite?, baixar_de_novo?}`) | 202 tarefa; 409 `IMPORTACAO_EM_ANDAMENTO`; 422 validação |
| `GET /base-local/tarefas/{tarefa_id}` | `status_importacao` | 200; 404 |
| `POST /base-local/tarefas/{tarefa_id}/cancelar` | `cancelar_importacao` | 200; 404 |
| `DELETE /base-local/importacoes/{programa}/{AAAAMM}` | — (só API e front) | 200 `{removido: true}`; 404; 409 se o mês está sendo importado |

Remover meses não é exposto como tool MCP de propósito: é destrutivo e fica com quem opera o front ou a API.

```bash
curl -X POST http://127.0.0.1:8000/base-local/importacoes \
  -H "X-API-Key: SUA_CHAVE" -H "Content-Type: application/json" \
  -d '{"programa": "novo-bolsa-familia", "ano": 2025, "mes": 1, "uf": "AM"}'
```

O importador de linha de comando continua disponível e usa o mesmo código: `python -m app.importar_dados novo-bolsa-familia 202501 --uf AM`.
