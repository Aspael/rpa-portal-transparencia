# Base de conhecimento do assistente

Este arquivo alimenta o RAG do mini chat (junto com `docs/*.md` e o README). Cada seção `##` vira um trecho que pode ser citado como fonte. Escreva em português, com frases que respondam a perguntas comuns.

## O que é o Observa

O Observa consulta quem recebeu pagamentos do Bolsa Família e de programas sociais relacionados, usando os dados publicados pelo Portal da Transparência da Controladoria-Geral da União (CGU). Ele devolve os dados em JSON pela API, por um front-end web, por tools MCP e por este chat. Por padrão as consultas usam uma base local no PostgreSQL, montada a partir dos arquivos mensais de dados abertos, sem acessar o site do governo na hora da consulta.

## Programas cobertos

- **Novo Bolsa Família** (`novo-bolsa-familia`): de 2023 em diante. Benefício mensal para famílias inscritas no Cadastro Único.
- **Bolsa Família original** (`bolsa-familia-pagamentos`): de 2003 até outubro de 2021.
- **Auxílio Brasil** (`auxilio-brasil`): substituiu o Bolsa Família de novembro de 2021 até o início de 2023.
- **Auxílio Emergencial** (`auxilio-emergencial`): auxílio da pandemia de COVID-19, pago em 2020 e 2021.
- **BPC — Benefício de Prestação Continuada** (`bpc`): um salário mínimo mensal para pessoas idosas (65 anos ou mais) e pessoas com deficiência de baixa renda.

## Como pesquisar uma pessoa

Na base local, a busca funciona por **nome completo** (maiúsculas e acentos são ignorados) ou por **NIS** (Número de Identificação Social, 11 dígitos). O CPF vem mascarado nos arquivos do governo, então só funciona quando os 6 dígitos do meio pertencem a uma única pessoa; senão a resposta é `CPF_AMBIGUO`. Se só souber parte do nome, use a pesquisa nos dados importados (pop-up "Consultar dados importados" ou a tool `pesquisar_base_local`, com "contém" para sobrenomes) e depois consulte pelo NIS.

## Por que o CPF aparece com asteriscos

O Portal da Transparência publica o CPF mascarado, no formato `***.123.456-**`, para proteger os beneficiários (Lei Geral de Proteção de Dados). Só os seis dígitos do meio aparecem. Não é possível nem permitido recuperar os dígitos ocultos. NIS e nome vêm completos.

## O que significa cada campo do resultado

- **Mês competência**: o mês a que o arquivo/pagamento se refere na folha do programa.
- **Mês referência**: o mês de origem da parcela; pode ser anterior quando é um pagamento retroativo.
- **Valor**: valor da parcela em reais.
- **Município/UF**: onde o benefício foi pago.
- **Panorama**: resumo da pessoa com programas, número de parcelas e valor total nos meses importados.
- **Meses na base local**: quais arquivos foram pesquisados. Se um mês não foi importado, as parcelas dele não aparecem.

## Por que uma pessoa não foi encontrada

Os motivos mais comuns: o mês em que ela recebeu não foi importado; a importação foi filtrada por outra UF; o nome não está completo ou está escrito diferente; titulares menores de 16 anos aparecem sem nome nos arquivos e são ignorados. Veja os meses importados no cartão "Base local" ou com a tool `listar_base_local`, importe o mês e a UF certos e consulte de novo (com "atualizar" para ignorar o histórico).

## Como importar um mês ou um ano

No cartão "03 · Base local" do front escolha programa, ano, mês (ou "Ano inteiro") e UF e clique em "Baixar e importar". A importação roda em segundo plano, um mês por vez, e pode ser cancelada. Pelo terminal: `python -m app.importar_dados novo-bolsa-familia 202501 --uf AM`. Pela API: `POST /base-local/importacoes`. Pelo MCP: `importar_base_local`. O portal publica os arquivos com alguns meses de atraso; meses ainda não publicados dão "Arquivo não encontrado".

## Tamanho dos arquivos e uso da UF

Um mês inteiro do Bolsa Família tem cerca de 360 MB compactado e 20 milhões de linhas, ocupando vários GB no banco. Filtrar por UF reduz para algumas centenas de milhares de linhas (o ZIP completo é baixado mesmo assim, mas só as linhas do estado são gravadas). Reimportar o mesmo programa e mês substitui as linhas anteriores.

## Histórico de consultas

Consultas com sucesso ficam gravadas no PostgreSQL e aparecem em "Consultas anteriores". Uma nova consulta pelo mesmo termo reaproveita o resultado gravado por até `HISTORY_CACHE_HOURS` horas (24 por padrão). Para forçar uma nova consulta depois de importar meses, use "atualizar".

## Fontes de dados alternativas

Além da base local (`QUERY_SOURCE=dados_abertos`), o Observa pode usar a API de Dados oficial do portal (`QUERY_SOURCE=api` ou `auto`, precisa da chave gratuita `PORTAL_API_KEY`, só CPF/NIS) ou um robô Playwright que navega no site e tira um screenshot como evidência (`QUERY_SOURCE=site`; o site pode exigir CAPTCHA).

## Chaves de acesso

`API_KEY` é a senha que você inventa para proteger a sua API e vai no cabeçalho `X-API-Key` (o front usa a do `.env` automaticamente). `PORTAL_API_KEY` é a chave do governo, recebida por e-mail no cadastro gov.br, e só é usada nos modos `api`/`auto`. Nunca coloque a chave do governo no campo do front.

## Sobre este chat

O chat usa um modelo local do Ollama (`OLLAMA_MODEL`, por padrão `qwen2.5:7b`) e as tools MCP do Observa: `consultar_pessoa`, `pesquisar_base_local`, `listar_base_local` e `status_importacao`. Importar e cancelar importações ficam no cartão "Base local" e nos clientes MCP. O botão "i" lista todas as tools. A memória de curto prazo guarda as últimas trocas da conversa por 30 minutos sem uso; "Nova conversa" apaga essa memória. As respostas sobre o sistema citam trechos da documentação como fontes.
