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
  $('message').textContent = result.mensagem || 'Consulta concluída. Os dados e a evidência estão disponíveis abaixo.';
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
    busy = false; $('submit').disabled = false; $('progress').hidden = true;
    loadHistory();
  }
});

function authHeaders() {
  return $('api-key').value ? {'X-API-Key': $('api-key').value} : {};
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
  details.textContent = `${{api_oficial: 'API oficial', assistido: 'Assistido'}[item.modo_execucao] || 'Robô'} · ${item.beneficios} benefício(s)`;
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
