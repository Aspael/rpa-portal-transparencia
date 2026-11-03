const $ = id => document.getElementById(id);
let currentResult = null;
let evidenceUrl = null;
let busy = false;

function setStatus(text, kind = '') {
  $('status').textContent = text;
  $('status').className = `badge ${kind}`;
}

function render(result) {
  currentResult = result;
  $('export-status').hidden = true;
  $('result').hidden = false;
  setStatus({sucesso: 'Concluída', parcial: 'Parcial', erro: 'Falha'}[result.status] || 'Resposta', result.status);
  $('message').textContent = result.origem === 'historico'
    ? `Resultado recuperado do histórico (PostgreSQL), consultado em ${new Date(result.consultado_em).toLocaleString('pt-BR', {dateStyle: 'short', timeStyle: 'short'})}.`
    : result.mensagem || 'Consulta concluída. Os dados e a evidência estão disponíveis abaixo.';
  $('person').replaceChildren();
  if (result.pessoa) {
    for (const [field, label] of Object.entries({nome: 'Nome', cpf: 'CPF', nis: 'NIS', localidade: 'Localidade'})) {
      const term = document.createElement('dt'); term.textContent = label;
      const value = document.createElement('dd'); value.textContent = result.pessoa[field] || 'Não informado';
      $('person').append(term, value);
    }
  }
  $('benefits').replaceChildren();
  for (const benefit of result.beneficios || []) {
    const row = document.createElement('div'); row.className = 'benefit';
    const title = document.createElement('strong'); title.textContent = benefit.programa;
    const state = document.createElement('span'); state.textContent = `${benefit.paginas.length} página(s) · ${benefit.completo ? 'Completo' : 'Incompleto'}`;
    row.append(title, state); $('benefits').append(row);
  }
  const evidence = result.evidencia || result.diagnostico?.evidencia;
  if (evidenceUrl) URL.revokeObjectURL(evidenceUrl);
  evidenceUrl = null;
  $('evidence-wrap').hidden = !evidence;
  if (evidence) {
    const bytes = Uint8Array.from(atob(evidence.base64), c => c.charCodeAt(0));
    evidenceUrl = URL.createObjectURL(new Blob([bytes], {type: 'image/png'}));
    $('evidence').src = evidenceUrl; $('evidence-link').href = evidenceUrl;
    $('evidence-title').textContent = result.evidencia ? 'Evidência do panorama' : 'Captura de diagnóstico da falha';
  }
  $('duration').textContent = `${((result.duracao_ms || 0) / 1000).toFixed(1)} s`;
  $('metadata').textContent = JSON.stringify({consulta_id: result.consulta_id, consultado_em: result.consultado_em, status: result.status, etapa: result.etapa, codigo: result.codigo, url: result.pessoa?.panorama?.url || result.diagnostico?.url}, null, 2);
}

$('query-form').addEventListener('submit', async event => {
  event.preventDefault();
  if (busy) return;
  busy = true; currentResult = null;
  $('submit').disabled = true; $('result').hidden = true; $('empty').hidden = true;
  $('progress').hidden = false; $('progress').textContent = 'Consultando o portal e preparando a evidência…';
  setStatus('Em andamento');
  // Modo assistido: se o portal pedir verificação, uma janela do Chromium abre no computador do servidor.
  const assistedHint = assistedMode && setTimeout(() => {
    $('progress').textContent = 'Ainda consultando… Se o portal pedir verificação humana, uma janela do Chromium vai abrir: resolva a verificação nela e a consulta continua sozinha.';
  }, 8000);
  try {
    const headers = {'Content-Type': 'application/json', ...authHeaders()};
    const response = await fetch('/consultas', {method: 'POST', headers, body: JSON.stringify({termo: $('term').value, beneficiario_programa_social: $('social').checked})});
    const data = await response.json();
    if (!data.consulta_id) {
      throw new Error(response.status === 401 ? 'Informe uma chave de API válida em “Chave de acesso da API”.' : response.status === 422 ? 'Confira o nome ou informe um CPF/NIS com 11 dígitos.' : 'A API não conseguiu processar a consulta.');
    }
    render(data);
  } catch (error) {
    setStatus('Falha', 'erro'); $('result').hidden = true; $('empty').hidden = false;
    $('empty').querySelector('h3').textContent = 'Não foi possível concluir';
    $('empty').querySelector('p').textContent = error.message;
  } finally {
    clearTimeout(assistedHint);
    busy = false; $('submit').disabled = false; $('progress').hidden = true;
    loadHistory();
  }
});

// Prioridade: campo do front; senão, a chave do .env que o servidor embute na página.
const envApiKey = document.querySelector('meta[name="api-key"]')?.content || '';
const assistedMode = document.querySelector('meta[name="assisted-mode"]')?.content === 'true';
function authHeaders() {
  const key = $('api-key').value.trim() || envApiKey;
  return key ? {'X-API-Key': key} : {};
}

async function loadHistory() {
  const params = new URLSearchParams({limite: 20});
  if ($('history-search').value.trim()) params.set('busca', $('history-search').value.trim());
  $('history-refresh').disabled = true;
  try {
    const response = await fetch(`/consultas?${params}`, {headers: authHeaders()});
    const data = await response.json();
    if (!response.ok) {
      throw new Error(response.status === 401 ? 'Informe a chave em “Chave de acesso da API” para ver o histórico.' : data.mensagem || 'Não foi possível carregar o histórico.');
    }
    $('history-list').replaceChildren(...data.map(historyRow));
    $('history-status').hidden = data.length > 0;
    $('history-status').textContent = params.has('busca') ? 'Nenhuma consulta encontrada para esse filtro.' : 'Nenhuma consulta gravada ainda.';
  } catch (error) {
    $('history-list').replaceChildren();
    $('history-status').hidden = false;
    $('history-status').textContent = error.message === 'Failed to fetch' ? 'Não foi possível carregar o histórico.' : error.message;
  } finally {
    $('history-refresh').disabled = false;
  }
}

function historyRow(item) {
  const button = document.createElement('button');
  button.type = 'button'; button.className = 'history-item'; button.dataset.id = item.consulta_id;
  if (currentResult?.consulta_id === item.consulta_id) button.classList.add('active');
  const when = document.createElement('time'); when.dateTime = item.consultado_em;
  when.textContent = new Date(item.consultado_em).toLocaleString('pt-BR', {dateStyle: 'short', timeStyle: 'short'});
  const who = document.createElement('strong'); who.textContent = item.nome || item.termo;
  const details = document.createElement('small');
  details.textContent = `${{api_oficial: 'API oficial', assistido: 'Assistido', dados_abertos: 'Dados abertos'}[item.modo_execucao] || 'Robô'} · ${item.beneficios} benefício(s)`;
  const state = document.createElement('span'); state.className = `badge ${item.status}`;
  state.textContent = {sucesso: 'Concluída', parcial: 'Parcial', erro: 'Falha'}[item.status] || item.status;
  button.append(when, who, details, state);
  button.addEventListener('click', () => openHistory(item.consulta_id));
  const row = document.createElement('li'); row.append(button);
  return row;
}

async function openHistory(id) {
  if (busy) return;
  busy = true; $('submit').disabled = true;
  try {
    const response = await fetch(`/consultas/${encodeURIComponent(id)}`, {headers: authHeaders()});
    const data = await response.json();
    if (!response.ok || !data.consulta_id) throw new Error(data.mensagem || data.detail || 'Não foi possível abrir a consulta.');
    $('empty').hidden = true;
    render(data);
    for (const button of document.querySelectorAll('.history-item')) button.classList.toggle('active', button.dataset.id === id);
    $('result-title').scrollIntoView({behavior: 'smooth', block: 'start'});
  } catch (error) {
    $('history-status').hidden = false; $('history-status').textContent = error.message;
  } finally {
    busy = false; $('submit').disabled = false;
  }
}

$('history-form').addEventListener('submit', event => { event.preventDefault(); loadHistory(); });
$('api-key').addEventListener('change', loadHistory);
loadHistory();

$('export').addEventListener('click', async () => {
  if (!currentResult || busy) return;
  busy = true; $('export').disabled = true; $('submit').disabled = true;
  $('export-status').hidden = false; $('export-status').textContent = 'Salvando o JSON no Drive e o registro no Sheets…';
  try {
    const headers = {'Content-Type': 'application/json', ...authHeaders()};
    const response = await fetch('/integracoes/google/arquivar', {method: 'POST', headers, body: JSON.stringify(currentResult)});
    const data = await response.json();
    if (!response.ok) throw new Error(data.mensagem || 'Não foi possível exportar. Confira a chave da API e a configuração Google.');
    $('export-status').textContent = 'Arquivo salvo e planilha atualizada. ';
    for (const [label, value] of [['Abrir JSON', data.arquivo_url], ['Abrir planilha', data.planilha_url]]) {
      const url = new URL(value);
      if (url.protocol !== 'https:' || !['drive.google.com', 'docs.google.com'].includes(url.hostname)) continue;
      const link = document.createElement('a'); link.href = url.href; link.textContent = label; link.target = '_blank'; link.rel = 'noopener';
      $('export-status').append(link, document.createTextNode(' · '));
    }
  } catch (error) {
    $('export-status').textContent = `${error.message} Você pode tentar novamente com este mesmo resultado.`;
  } finally {
    busy = false; $('export').disabled = false; $('submit').disabled = false;
  }
});

$('download').addEventListener('click', () => {
  if (!currentResult) return;
  const url = URL.createObjectURL(new Blob([JSON.stringify(currentResult, null, 2)], {type: 'application/json'}));
  const link = document.createElement('a'); link.href = url;
  link.download = `${currentResult.consulta_id}_${currentResult.consultado_em.replace(/[:.]/g, '-')}.json`;
  link.click(); setTimeout(() => URL.revokeObjectURL(url), 1000);
});

// ---- Base local: baixar e importar meses dos dados abertos ----
const PROGRAMS = {
  'novo-bolsa-familia': 'Novo Bolsa Família (2023+)', 'bolsa-familia-pagamentos': 'Bolsa Família (até 2021)',
  'auxilio-brasil': 'Auxílio Brasil (2021–2023)', 'auxilio-emergencial': 'Auxílio Emergencial (2020–2021)',
  'bpc': 'BPC',
};
const PROGRAM_YEARS = {'novo-bolsa-familia': [2023], 'bolsa-familia-pagamentos': [2013, 2021], 'auxilio-brasil': [2021, 2023], 'auxilio-emergencial': [2020, 2021]};
const MONTHS = ['Janeiro', 'Fevereiro', 'Março', 'Abril', 'Maio', 'Junho', 'Julho', 'Agosto', 'Setembro', 'Outubro', 'Novembro', 'Dezembro'];
const UFS = 'AC AL AM AP BA CE DF ES GO MA MG MS MT PA PB PE PI PR RJ RN RO RR RS SC SE SP TO'.split(' ');
const TASK_STATUS = {na_fila: 'Na fila', em_andamento: 'Em andamento', concluida: 'Concluída', parcial: 'Parcial', erro: 'Falha', cancelada: 'Cancelada'};
const MONTH_STATUS = {pendente: 'Na fila', baixando: 'Baixando', importando: 'Importando', concluido: 'Concluído', erro: 'Falha', cancelado: 'Cancelado'};
const ACTIVE = ['na_fila', 'em_andamento'];
let baseTask = null;
let basePoll = null;
let baseImports = [];
let browsePage = 1;

function option(value, label) {
  const item = document.createElement('option'); item.value = value; item.textContent = label; return item;
}

function fillBaseForm() {
  const today = new Date();
  $('base-program').replaceChildren(...Object.entries(PROGRAMS).map(([value, label]) => option(value, label)));
  const years = [];
  for (let year = today.getFullYear(); year >= 2013; year--) years.push(option(year, year));
  $('base-year').replaceChildren(...years);
  $('base-month').replaceChildren(option('', 'Ano inteiro'), ...MONTHS.map((name, index) => option(index + 1, name)));
  $('base-uf').replaceChildren(option('', 'Todas (arquivo inteiro)'), ...UFS.map(uf => option(uf, uf)));
  // Padrão: um mês recente já publicado (o portal publica com atraso), filtrado por UF.
  const recent = new Date(today.getFullYear(), today.getMonth() - 2, 1);
  $('base-year').value = recent.getFullYear(); $('base-month').value = recent.getMonth() + 1; $('base-uf').value = 'AM';
  updateBaseHint();
}

function updateBaseHint() {
  const program = $('base-program').value, year = Number($('base-year').value);
  const [from, to] = PROGRAM_YEARS[program] || [];
  const notes = [];
  if (from && (year < from || (to && year > to))) notes.push(`Atenção: ${PROGRAMS[program]} não tem arquivos em ${year}.`);
  notes.push($('base-uf').value
    ? 'Com UF, só as linhas do estado entram no banco (o ZIP do mês inteiro é baixado mesmo assim).'
    : 'Sem UF, um mês do Bolsa Família tem ~20 milhões de linhas e ocupa vários GB no banco.');
  if (!$('base-month').value) notes.push('Ano inteiro: importa os 12 meses, um por vez.');
  $('base-size').textContent = notes.join(' ');
}

for (const id of ['base-program', 'base-year', 'base-month', 'base-uf']) $(id).addEventListener('change', updateBaseHint);

function formatMonth(value) { return `${value.slice(4)}/${value.slice(0, 4)}`; }

async function readError(response, fallback) {
  try {
    const data = await response.json();
    if (Array.isArray(data.detail)) return data.detail.map(item => item.msg.replace(/^Value error, /, '')).join('; ');
    return data.mensagem || data.detail || fallback;
  } catch { return fallback; }
}

async function loadBase() {
  try {
    const response = await fetch('/base-local', {headers: authHeaders()});
    if (!response.ok) throw new Error(response.status === 401 ? 'Informe a chave em “Chave de acesso da API” para ver a base local.' : await readError(response, 'Não foi possível carregar a base local.'));
    const data = await response.json();
    renderImports(data.importados);
    renderTask(data.tarefa_atual || (baseTask && data.tarefas.find(task => task.tarefa_id === baseTask.tarefa_id)) || null);
  } catch (error) {
    $('base-list').replaceChildren();
    $('base-status').hidden = false;
    $('base-status').textContent = error.message === 'Failed to fetch' ? 'Não foi possível carregar a base local.' : error.message;
    stopBasePoll();
  }
}

function renderImports(items) {
  baseImports = items;
  $('browse-open').disabled = items.length === 0;
  $('base-badge').textContent = `${items.length} mês(es)`;
  $('base-status').hidden = items.length > 0;
  $('base-status').textContent = 'Nenhum mês importado ainda. Escolha programa, ano e mês e clique em “Baixar e importar”.';
  $('base-list').replaceChildren(...items.map(item => {
    const row = document.createElement('li'); row.className = 'base-item';
    const name = document.createElement('strong'); name.textContent = `${item.programa_nome} · ${formatMonth(item.arquivo_mes)}`;
    const details = document.createElement('small');
    details.textContent = `${item.uf || 'Todas as UFs'} · ${Number(item.linhas).toLocaleString('pt-BR')} linhas · ${new Date(item.importado_em).toLocaleString('pt-BR', {dateStyle: 'short', timeStyle: 'short'})}`;
    const remove = document.createElement('button'); remove.type = 'button'; remove.className = 'link-button';
    remove.textContent = 'Remover'; remove.setAttribute('aria-label', `Remover ${name.textContent}`);
    remove.addEventListener('click', () => removeImport(item, remove));
    const browse = document.createElement('button'); browse.type = 'button'; browse.className = 'link-button browse-link';
    browse.textContent = 'Ver dados'; browse.setAttribute('aria-label', `Ver dados de ${name.textContent}`);
    browse.addEventListener('click', () => openBrowse(item));
    row.append(name, details, browse, remove);
    return row;
  }));
}

function renderTask(task) {
  baseTask = task;
  $('base-task').hidden = !task;
  const active = Boolean(task && ACTIVE.includes(task.status));
  $('base-submit').disabled = active;
  if (!task) { stopBasePoll(); return; }
  const months = task.meses.length === 1 ? formatMonth(task.meses[0].mes) : `${task.meses.length} meses de ${task.meses[0].mes.slice(0, 4)}`;
  $('task-title').textContent = `${task.programa_nome} · ${months}${task.uf ? ` · ${task.uf}` : ''}`;
  $('task-status').textContent = TASK_STATUS[task.status] || task.status;
  $('task-status').className = `badge ${{concluida: 'sucesso', erro: 'erro', parcial: 'parcial', cancelada: 'parcial'}[task.status] || ''}`;
  // Barra: meses terminados + metade do mês atual proporcional ao download.
  const done = task.meses.filter(month => !['pendente', 'baixando', 'importando'].includes(month.status)).length;
  const current = task.meses.find(month => ['baixando', 'importando'].includes(month.status));
  const partial = !current ? 0 : current.status === 'importando' ? 0.5 : (task.percentual || 0) / 200;
  const percent = Math.round((active ? (done + partial) / task.meses.length : 1) * 100);
  $('task-bar').style.width = `${percent}%`;
  $('task-bar').parentElement.setAttribute('aria-valuenow', percent);
  $('task-message').textContent = task.mensagem || 'Na fila…';
  const showMonths = task.meses.length > 1 || task.meses[0].mensagem;
  $('task-months').replaceChildren(...(showMonths ? task.meses : []).map(month => {
    const row = document.createElement('li');
    const label = document.createElement('span'); label.textContent = formatMonth(month.mes);
    const state = document.createElement('small'); state.className = month.status;
    state.textContent = month.mensagem || MONTH_STATUS[month.status];
    row.append(label, state); return row;
  }));
  $('task-cancel').hidden = !active;
  if (active && !basePoll) basePoll = setInterval(loadBase, 2000);
  if (!active) stopBasePoll();
}

function stopBasePoll() { clearInterval(basePoll); basePoll = null; }

$('base-form').addEventListener('submit', async event => {
  event.preventDefault();
  $('base-submit').disabled = true;
  const body = {programa: $('base-program').value, ano: Number($('base-year').value)};
  if ($('base-month').value) body.mes = Number($('base-month').value);
  if ($('base-uf').value) body.uf = $('base-uf').value;
  if ($('base-limit').value) body.limite = Number($('base-limit').value);
  if ($('base-force').checked) body.baixar_de_novo = true;
  try {
    const response = await fetch('/base-local/importacoes', {method: 'POST', headers: {'Content-Type': 'application/json', ...authHeaders()}, body: JSON.stringify(body)});
    if (!response.ok) {
      const message = await readError(response, 'Não foi possível iniciar a importação.');
      throw new Error(response.status === 401 ? 'Informe uma chave de API válida em “Chave de acesso da API”.' : message);
    }
    const task = await response.json();
    renderTask(task);
    if (!ACTIVE.includes(task.status)) loadBase();
  } catch (error) {
    stopBasePoll(); baseTask = null;
    $('base-task').hidden = false;
    $('task-title').textContent = 'Importação não iniciada';
    $('task-status').textContent = 'Falha'; $('task-status').className = 'badge erro';
    $('task-bar').style.width = '0%'; $('task-message').textContent = error.message;
    $('task-months').replaceChildren(); $('task-cancel').hidden = true;
    $('base-submit').disabled = false;
  }
});

$('task-cancel').addEventListener('click', async () => {
  if (!baseTask) return;
  $('task-cancel').disabled = true;
  try {
    const response = await fetch(`/base-local/tarefas/${encodeURIComponent(baseTask.tarefa_id)}/cancelar`, {method: 'POST', headers: authHeaders()});
    if (response.ok) renderTask(await response.json());
  } catch {
    // A próxima atualização mostra o estado real.
  } finally { $('task-cancel').disabled = false; }
});

async function removeImport(item, button) {
  if (!confirm(`Remover ${item.programa_nome} ${formatMonth(item.arquivo_mes)} da base local? As consultas deixam de encontrar essas parcelas.`)) return;
  button.disabled = true;
  try {
    const response = await fetch(`/base-local/importacoes/${encodeURIComponent(item.programa)}/${encodeURIComponent(item.arquivo_mes)}`, {method: 'DELETE', headers: authHeaders()});
    if (!response.ok) throw new Error(await readError(response, 'Não foi possível remover.'));
    await loadBase();
  } catch (error) {
    $('base-status').hidden = false; $('base-status').textContent = error.message; button.disabled = false;
  }
}

// ---- Pop-up: consultar dados importados ----
function brl(value) {
  return value == null ? '' : Number(value).toLocaleString('pt-BR', {style: 'currency', currency: 'BRL'});
}

function openBrowse(item = null) {
  $('browse-base').replaceChildren(option('', 'Todas as bases importadas'), ...baseImports.map(base =>
    option(`${base.programa}|${base.arquivo_mes}`, `${base.programa_nome} · ${formatMonth(base.arquivo_mes)} · ${base.uf || 'todas as UFs'}`)));
  if (item) $('browse-base').value = `${item.programa}|${item.arquivo_mes}`;
  if (!$('browse-uf').options.length) $('browse-uf').replaceChildren(option('', 'Todas'), ...UFS.map(uf => option(uf, uf)));
  if (!$('browse').open) $('browse').showModal();
  $('browse-term').focus();
  browsePage = 1;
  // Já abre listando: com uma base escolhida, o usuário vê o conteúdo sem digitar nada.
  if (item) searchBrowse();
}

async function searchBrowse() {
  const [programa, mes] = $('browse-base').value.split('|');
  const params = new URLSearchParams({pagina: browsePage, por_pagina: 25});
  if (programa) { params.set('programa', programa); params.set('arquivo_mes', mes); }
  for (const [key, id] of [['termo', 'browse-term'], ['uf', 'browse-uf'], ['municipio', 'browse-city']]) {
    if ($(id).value.trim()) params.set(key, $(id).value.trim());
  }
  if ($('browse-contains').checked) params.set('contem', 'true');
  $('browse-submit').disabled = true;
  $('browse-status').hidden = false; $('browse-status').textContent = 'Pesquisando na base local…';
  try {
    const response = await fetch(`/base-local/pagamentos?${params}`, {headers: authHeaders()});
    if (!response.ok) throw new Error(response.status === 401 ? 'Informe a chave em “Chave de acesso da API”.' : await readError(response, 'Não foi possível pesquisar.'));
    renderBrowse(await response.json());
  } catch (error) {
    $('browse-status').textContent = error.message === 'Failed to fetch' ? 'Não foi possível pesquisar na base local.' : error.message;
    document.querySelector('.browse-table').hidden = true; document.querySelector('.browse-pager').hidden = true;
  } finally {
    $('browse-submit').disabled = false;
  }
}

function renderBrowse(data) {
  const empty = data.itens.length === 0;
  $('browse-status').hidden = !empty;
  $('browse-status').textContent = data.pagina > 1 ? 'Não há mais resultados.' : 'Nenhuma parcela encontrada com esses filtros. Confira o nome completo ou tente “contém”.';
  document.querySelector('.browse-table').hidden = empty;
  $('browse-rows').replaceChildren(...data.itens.map(item => {
    const row = document.createElement('tr');
    const cells = [item.nome, item.nis || '—', item.cpf || '—', [item.municipio, item.uf].filter(Boolean).join('/') || '—',
      `${PROGRAMS[item.programa]?.replace(/ \(.*\)$/, '') || item.programa_nome} · ${formatMonth(item.arquivo_mes)}${item.mes_referencia && item.mes_referencia !== item.arquivo_mes ? ` (ref. ${formatMonth(item.mes_referencia)})` : ''}`,
      brl(item.valor)];
    cells.forEach((text, index) => {
      const cell = document.createElement('td'); cell.textContent = text;
      cell.dataset.label = ['Nome', 'NIS', 'CPF', 'Local', 'Mês', 'Valor'][index];
      if (index === 5) cell.className = 'num';
      row.append(cell);
    });
    const action = document.createElement('td');
    const open = document.createElement('button'); open.type = 'button'; open.className = 'secondary small';
    open.textContent = 'Ver panorama'; open.setAttribute('aria-label', `Ver panorama de ${item.nome}`);
    open.addEventListener('click', () => {
      $('browse').close();
      $('term').value = item.nis || item.nome;
      $('query-form').requestSubmit();
      $('result-title').scrollIntoView({behavior: 'smooth', block: 'start'});
    });
    action.append(open); row.append(action);
    return row;
  }));
  document.querySelector('.browse-pager').hidden = data.pagina === 1 && !data.tem_mais;
  $('browse-prev').disabled = data.pagina === 1; $('browse-next').disabled = !data.tem_mais;
  $('browse-page').textContent = `Página ${data.pagina}`;
}

$('browse-open').addEventListener('click', () => openBrowse());
$('browse-close').addEventListener('click', () => $('browse').close());
$('browse').addEventListener('click', event => { if (event.target === $('browse')) $('browse').close(); });
$('browse-form').addEventListener('submit', event => { event.preventDefault(); browsePage = 1; searchBrowse(); });
$('browse-prev').addEventListener('click', () => { browsePage = Math.max(1, browsePage - 1); searchBrowse(); });
$('browse-next').addEventListener('click', () => { browsePage += 1; searchBrowse(); });

fillBaseForm();
loadBase();
$('api-key').addEventListener('change', loadBase);

// ---- Mini chat: Ollama + tools MCP + RAG + memória de curto prazo (guardada no servidor) ----
const CHAT_SUGGESTIONS = ['Quais meses estão importados?', 'Por que o CPF aparece com asteriscos?', 'Procure pessoas com sobrenome SILVA em Manaus'];
let chatSession = null;
let chatBusy = false;
let chatToolsLoaded = false;

function storedSession() {
  try { return sessionStorage.getItem('observa-chat') || null; } catch { return null; }
}
function storeSession(id) {
  try { id ? sessionStorage.setItem('observa-chat', id) : sessionStorage.removeItem('observa-chat'); } catch { /* sem storage: memória só nesta aba */ }
}

// Texto do modelo sem HTML: só **negrito**, quebras de linha e citações [n].
// O CPF mascarado (***.123.456-**) entra antes no split para os asteriscos não virarem negrito.
const MASKED_CPF = /^\*{3}\.\d{3}\.\d{3}-\*{2}$/;
function richText(text, sources) {
  const fragment = document.createDocumentFragment();
  for (const part of text.split(/(\*{3}\.\d{3}\.\d{3}-\*{2}|\*\*[^*\s][^*]*\*\*|\[\d\])/g)) {
    if (!part) continue;
    const bold = !MASKED_CPF.test(part) && part.match(/^\*\*([^*]+)\*\*$/);
    const cite = part.match(/^\[(\d)\]$/);
    if (bold) {
      const strong = document.createElement('strong'); strong.textContent = bold[1]; fragment.append(strong);
    } else if (cite && sources.some(source => source.n === Number(cite[1]))) {
      const mark = document.createElement('sup'); mark.className = 'cite'; mark.textContent = cite[1];
      mark.title = sources.find(source => source.n === Number(cite[1])).titulo; fragment.append(mark);
    } else {
      fragment.append(document.createTextNode(part));
    }
  }
  return fragment;
}

function chatMessage(role, text, extra = {}) {
  const item = document.createElement('div'); item.className = `chat-msg ${role}`;
  const bubble = document.createElement('div'); bubble.className = 'bubble';
  bubble.append(role === 'assistant' ? richText(text, extra.fontes || []) : document.createTextNode(text));
  item.append(bubble);
  if (extra.ferramentas?.length) {
    const tools = document.createElement('ul'); tools.className = 'chat-used';
    for (const tool of extra.ferramentas) {
      const row = document.createElement('li'); row.className = tool.status;
      const args = Object.entries(tool.argumentos || {}).map(([key, value]) => `${key}: ${JSON.stringify(value)}`).join(', ');
      row.textContent = `↳ ${tool.nome}(${args})${tool.status === 'erro' ? ' · erro' : ''}`;
      tools.append(row);
    }
    item.append(tools);
  }
  if (extra.fontes?.length) {
    const sources = document.createElement('details'); sources.className = 'chat-sources';
    const summary = document.createElement('summary'); summary.textContent = `Fontes da documentação (${extra.fontes.length})`;
    const list = document.createElement('ol');
    for (const source of extra.fontes) {
      const row = document.createElement('li'); row.value = source.n;
      row.textContent = `${source.titulo} — ${source.arquivo}`;
      if (source.citada) row.className = 'cited';
      list.append(row);
    }
    sources.append(summary, list); item.append(sources);
  }
  $('chat-log').append(item);
  $('chat-log').scrollTop = $('chat-log').scrollHeight;
  return item;
}

function chatWelcome() {
  $('chat-log').replaceChildren();
  chatMessage('assistant', 'Olá! Posso consultar pessoas na base local, pesquisar os dados importados e explicar como o Observa funciona. Clique em “i” para ver as ferramentas MCP que eu uso.');
  const chips = document.createElement('div'); chips.className = 'chat-chips';
  for (const text of CHAT_SUGGESTIONS) {
    const chip = document.createElement('button'); chip.type = 'button'; chip.className = 'chip'; chip.textContent = text;
    chip.addEventListener('click', () => { $('chat-input').value = text; $('chat-form').requestSubmit(); });
    chips.append(chip);
  }
  $('chat-log').append(chips);
}

function openChat(open) {
  $('chat').hidden = !open;
  $('chat-toggle').setAttribute('aria-expanded', String(open));
  $('chat-toggle').hidden = open;
  if (open) {
    if (!$('chat-log').childElementCount) chatWelcome();
    loadChatTools();
    $('chat-input').focus();
  } else {
    $('chat-toggle').focus();
  }
}

async function loadChatTools() {
  if (chatToolsLoaded) return;
  try {
    const response = await fetch('/chat/ferramentas', {headers: authHeaders()});
    if (!response.ok) throw new Error(await readError(response, 'Não foi possível listar as ferramentas.'));
    const data = await response.json();
    chatToolsLoaded = true;
    $('chat-model').textContent = `Ollama · ${data.modelo} · ${data.ferramentas.filter(tool => tool.no_chat).length} tools no chat`;
    $('chat-foot').textContent = `RAG na documentação · memória das últimas ${data.memoria_turnos} trocas`;
    $('chat-tools-list').replaceChildren(...data.ferramentas.map(tool => {
      const row = document.createElement('li');
      const head = document.createElement('div'); head.className = 'tool-head';
      const name = document.createElement('code'); name.textContent = tool.nome;
      const tag = document.createElement('span'); tag.className = `tag ${tool.no_chat ? 'on' : ''}`;
      tag.textContent = tool.no_chat ? 'no chat' : 'só MCP / front';
      head.append(name, tag);
      const description = document.createElement('p'); description.textContent = tool.descricao;
      row.append(head, description);
      if (tool.parametros.length) {
        const params = document.createElement('p'); params.className = 'tool-params';
        params.textContent = 'Parâmetros: ' + tool.parametros.map(param => `${param.nome}${param.obrigatorio ? '*' : ''} (${param.tipo})`).join(', ');
        row.append(params);
      }
      return row;
    }));
  } catch (error) {
    $('chat-tools-list').replaceChildren();
    const row = document.createElement('li'); row.textContent = error.message === 'Failed to fetch' ? 'Não foi possível listar as ferramentas.' : error.message;
    $('chat-tools-list').append(row);
  }
}

$('chat-toggle').addEventListener('click', () => openChat(true));
$('chat-close').addEventListener('click', () => openChat(false));
$('chat').addEventListener('keydown', event => { if (event.key === 'Escape') openChat(false); });
$('chat-info').addEventListener('click', () => {
  const show = $('chat-tools').hidden;
  $('chat-tools').hidden = !show;
  $('chat-info').setAttribute('aria-expanded', String(show));
  $('chat-info').classList.toggle('active', show);
  if (show) loadChatTools();
});
$('chat-reset').addEventListener('click', async () => {
  const id = chatSession;
  chatSession = null; storeSession(null);
  chatWelcome();
  if (id) fetch(`/chat/${encodeURIComponent(id)}`, {method: 'DELETE', headers: authHeaders()}).catch(() => {});
});
$('chat-input').addEventListener('keydown', event => {
  if (event.key === 'Enter' && !event.shiftKey) { event.preventDefault(); $('chat-form').requestSubmit(); }
});
$('chat-input').addEventListener('input', () => {
  $('chat-input').style.height = 'auto';
  $('chat-input').style.height = `${Math.min($('chat-input').scrollHeight, 120)}px`;
});

$('chat-form').addEventListener('submit', async event => {
  event.preventDefault();
  const text = $('chat-input').value.trim();
  if (!text || chatBusy) return;
  chatBusy = true; $('chat-send').disabled = true;
  document.querySelector('.chat-chips')?.remove();
  chatMessage('user', text);
  $('chat-input').value = ''; $('chat-input').style.height = 'auto';
  const thinking = chatMessage('assistant pending', 'Pensando…');
  try {
    const response = await fetch('/chat', {method: 'POST', headers: {'Content-Type': 'application/json', ...authHeaders()},
      body: JSON.stringify({mensagem: text, ...(chatSession ? {sessao_id: chatSession} : {})})});
    if (!response.ok) throw new Error(response.status === 401 ? 'Informe a chave em “Chave de acesso da API”.' : await readError(response, 'O assistente não respondeu.'));
    const data = await response.json();
    chatSession = data.sessao_id; storeSession(chatSession);
    thinking.remove();
    chatMessage('assistant', data.resposta, data);
  } catch (error) {
    thinking.remove();
    chatMessage('assistant error', error.message === 'Failed to fetch' ? 'Não foi possível falar com o servidor.' : error.message);
  } finally {
    chatBusy = false; $('chat-send').disabled = false; $('chat-input').focus();
  }
});

chatSession = storedSession();
