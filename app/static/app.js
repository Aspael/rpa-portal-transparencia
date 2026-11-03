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
    row.append(name, details, remove);
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

fillBaseForm();
loadBase();
$('api-key').addEventListener('change', loadBase);
