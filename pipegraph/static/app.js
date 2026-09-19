const state = { document: null, runId: null, selected: new Set(), search: '', timer: null };

const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];
const esc = (value) => String(value ?? '').replace(/[&<>"]/g, (c) => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));

async function api(path, options) {
  const response = await fetch(path, options);
  if (!response.ok) {
    let message = String(response.status);
    try { message = (await response.json()).detail || message; } catch {}
    throw new Error(message);
  }
  return response.json();
}

async function init() {
  try {
    const health = await api('/api/health');
    $('#backend').textContent = health.deepseek ? `Provider: ${health.model}` : 'DEEPSEEK_API_KEY не найден';
    if (health.default_pdf) $('#default-pdf-name').textContent = health.default_pdf;
  } catch (error) {
    $('#backend').textContent = 'Backend недоступен';
  }
}

function applyDocument(data) {
  state.document = data;
  state.selected.clear();
  renderGroups(data.groups);
  $('#step-lines').hidden = false;
  $('#doc-summary').textContent = `${data.source_name} · ${data.pages_count} стр. · ${data.groups.length} линий`;
}

async function loadDefaultPdf() {
  const checkbox = $('#use-default-pdf');
  if (!checkbox.checked) {
    $('#custom-row').style.display = '';
    $('#doc-summary').textContent = 'PDF не загружен';
    return;
  }
  try {
    const data = await api('/api/pdf/load-default', { method: 'POST' });
    applyDocument(data);
    $('#custom-row').style.display = 'none';
  } catch (error) {
    checkbox.checked = false;
    alert(error.message);
  }
}

async function loadPdf() {
  const path = $('#pdf-path').value.trim();
  const fileInput = $('#pdf-file').files[0];
  const button = $('#load-pdf');
  button.disabled = true;
  try {
    let data;
    if (fileInput) {
      const form = new FormData();
      form.append('file', fileInput);
      const response = await fetch('/api/pdf/upload', { method: 'POST', body: form });
      if (!response.ok) {
        let detail = String(response.status);
        try { detail = (await response.json()).detail || detail; } catch {}
        throw new Error(detail);
      }
      data = await response.json();
    } else {
      if (!path) throw new Error('Укажите путь к PDF');
      data = await api('/api/pdf/load', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ file_path: path }),
      });
    }
    applyDocument(data);
  } catch (error) {
    alert(error.message);
  } finally {
    button.disabled = false;
  }
}

function renderGroups(groups) {
  const container = $('#groups');
  container.innerHTML = '';
  for (const group of groups) {
    const row = document.createElement('label');
    row.className = 'chip one-col';
    row.innerHTML = `<input type="checkbox" value="${esc(group.line_id)}">`
      + `<span class="name">${esc(group.line_id)}</span>`
      + `<span class="muted">стр. ${(group.pages || []).join(', ')}</span>`;
    const checkbox = row.querySelector('input');
    checkbox.addEventListener('change', () => {
      row.classList.toggle('on', checkbox.checked);
      if (checkbox.checked) state.selected.add(group.line_id);
      else state.selected.delete(group.line_id);
      $('#groups-count').textContent = `${state.selected.size} выбрано`;
    });
    container.appendChild(row);
  }
  $('#groups-count').textContent = `${groups.length} линий · 0 выбрано`;
}

async function startAnalysis() {
  const lineIds = $$('#groups input:checked').map((item) => item.value);
  if (!lineIds.length) { alert('Выберите хотя бы одну линию'); return; }
  const response = await api('/api/analyze', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ document_id: state.document.document_id, line_ids: lineIds }),
  });
  state.runId = response.run_id;
  $('#step-run').hidden = false;
  $('#step-result').hidden = true;
  await pollRun();
}

async function pollRun() {
  try {
    const run = await api(`/api/runs/${state.runId}`);
    renderRun(run);
    if (run.status !== 'running') {
      renderResults(run);
      return;
    }
  } catch (error) {
    $('#run-status').innerHTML = `<div class="badge err">Ошибка: ${esc(error.message)}</div>`;
    return;
  }
  setTimeout(pollRun, 1800);
}

function renderRun(run) {
  $('#run-status').innerHTML = `Прогон: <span class="mono">${esc(run.run_id)}</span> · статус: <b>${esc(run.status)}</b>`;
  const container = $('#lines-progress');
  container.innerHTML = '';
  for (const line of run.lines) {
    const badge = line.status === 'complete' ? 'ok' : line.status === 'error' ? 'err' : 'run';
    const card = document.createElement('div');
    card.className = 'line-card';
    card.innerHTML = `
      <div class="row" style="margin:0"><b>${esc(line.line_id)}</b> <span class="badge ${badge}">${esc(line.status)} · ${esc(line.stage)}</span></div>
      ${line.error ? `<div class="muted">Ошибка: ${esc(line.error)}</div>` : ''}
      <ul class="events">${(line.events || []).map((item) => `<li>${esc(item.stage)}: ${esc(item.message)}</li>`).join('')}</ul>
    `;
    container.appendChild(card);
  }
}

function resultKpis(answer) {
  const edges = (answer.edges || []);
  const known = edges.filter((e) => typeof e.length_mm === 'number');
  const pathSum = known.reduce((sum, e) => sum + e.length_mm, 0);
  const fragile = edges.filter((e) => e.exact === false).length;
  const unclear = (answer.unclear || []).length;
  return { known: known.length, total: edges.length, fragmentSum: path_sum, unclear, uncertain: fragile };
}

function resultBlock(line) {
  const answer = line.result?.answer || {};
  const edges = answer.edges || [];
  const branches = answer.branch_edges || [];
  const unclear = answer.unclear || [];
  const vertices = line.result?.vertices || [];
  const kpi = `
    <div class="kpi">
      <div><span>Сумма по основному пути (округлено)</span><strong>${total_length(answer)}</strong></div>
      <div><span>Уверенных / всех рёбер</span><strong>${answer.edges?.filter(e => e.exact).length ?? 0} / ${(answer.edges || []).length}</strong></div>
      <div><span>Неуверенных рёбер</span><strong>${fragile_count(answer)}</strong></div>
      <div><span>Неопределённых чисел</span><strong>${unclear.length}</strong></div>
    </div>`;
  const table = `
    <h3>Расстояния по вершинам</h3>
    <table><thead><tr><th>Из · В</th><th>мм</th><th>Точность</th><th>Основание</th><th>Примечание</th></tr></thead><tbody>
      ${(answer.edges || []).map((edge) => edgeRow(edge)).join('') || '<tr><td colspan="5">Рёбер нет</td></tr>'}
    </tbody></table>`;
  const branchBlock = (answer.branch_edges || []).length ? `
    <h3>Ответвления</h3>
    <table><thead><tr><th>Из · В</th><th>мм</th><th>Точность</th><th>Примечание</th></tr></thead><tbody>
      ${answer.branch_edges.map((edge) => edgeRow(edge)).join('')}
    </tbody></table>` : '';
  const unclearBlock = unclear.length ? `
    <div class="notes"><b>Неопределённые числа (не выдуманные):</b>
    <ul>${unclear.map((item) => `<li><code>${esc(item.text ?? '')}</code> — ${esc(item.why ?? '')}</li>`).join('')}</ul></div>` : '';
  const orderBlock = (answer.order || []).length ? `<div class="muted">Порядок вершин: ${(answer.order || []).map(esc).join(' → ')}</div>` : '';
  const notesBlock = (answer.notes || []).length ? `<div class="notes">${answer.notes.map(esc).join('<br>')}</div>` : '';
  return `
    <div class="line-card results">
      <div class="row"><b>${esc(line.line_id)}</b><span class="pill-soft">стр. ${(line.result?.page || '')}</span></div>
      ${kpi}
      ${orderBlock}
      ${table}
      ${branches && branches.length ? `<h3>Ответвления</h3><table><tbody>${answer.branch_edges.map(edgeRow).join('')}</tbody></table>` : ''}
      ${unclearBlock}
      ${notesBlock}
      ${branchCount(answer)}
      <details><summary>Сырой ответ модели $(answer.notes && answer.notes.length ? '' : '')</summary><pre>${esc(JSON.stringify(answer, null, 2))}</pre></details>
    </div>`;
}

function edgeRow(edge) {
  const tag = edge.exact ? '<span class="tag exact">точно</span>' : (edge.length_mm == null ? '<span class="tag missing">нет данных</span>' : '<span class="tag approx">неуверенно</span>');
  return `<tr><td class="mono">${esc(edge.from)} → ${esc(edge.to)}</td><td>${edge.length_mm ?? '—'}</td><td>${tag}</td><td>${esc(edge.basis ?? '')}</td><td>${esc(edge.note ?? '')}</td></tr>`;
}

function renderResults(run) {
  const container = $('#step-result');
  container.hidden = false;
  $('#run-title').textContent = run.run_id;
  const body = $('#result-body');
  body.innerHTML = run.lines.map((line) => resultBlock(line)).join('<hr>');
}

function total_length(answer) {
  const sum = (answer.edges || []).filter((edge) => edge.exact && typeof edge.length_mm === 'number').reduce((a, b) => a + (b.length_mm || 0), 0);
  return `${sum} мм`;
}

function fragile_count(answer) {
  return (answer.edges || []).filter((edge) => typeof edge.length_mm === 'number' && edge.exact === false).length;
}

function resultBlock(line) {
  const answer = line.result?.answer;
  if (!answer) {
    return `<div class="line-card"><b>${esc(line.line_id)}</b><div class="muted">${line.status === 'error' ? ('Ошибка: ' + esc(line.error)) : 'Нет результата'}</div></div>`;
  }
  const edges = answer.edges || [];
  const branches = answer.branch_edges || [];
  const unclear = answer.unclear || [];
  const edgeRow = (edge) => `
    <tr>
      <td>${esc(edge.from)} → ${esc(edge.to)}</td>
      <td>${edge.length_mm ?? '—'}</td>
      <td>${edge.exact ? '<span class="tag exact">точно</span>' : (edge.length_mm == null ? '<span class="tag missing">нет длины</span>' : '<span class="tag approx">неуверенно</span>')}</td>
      <td>${esc(edge.basis || '')}</td>
      <td>${esc(edge.note || '')}</td>
    </div>`;
  const knownSum = edges.filter((edge) => edge.exact && typeof edge.length_mm === 'number').reduce((sum, edge) => sum + edge.length_mm, 0);
  const approxSum = edges.filter((edge) => !edge.exact && typeof edge.length_mm === 'number').reduce((sum, edge) => sum + edge.length_mm, 0);
  const kpis = `
    <div class="kpi">
      <div><span>Сумма уверенных длин</span><strong>${knownSum} мм</strong></div>
      <div><span>Сумма неуверенных</span><strong>${approxSum} мм</strong></div>
      <div><span>Рёбер всего / без длины</span><strong>${edges.length} / ${edges.filter((edge) => edge.length_mm == null).length}</strong></div>
      <div><span>Неопределённых чисел</span><strong>${unclear.length}</strong></div>
    </div>`;
  const orderBlock = (answer.order || []).length ? `<p class="muted">Порядок: ${answer.order.map(esc).join(' → ')}</p>` : '';
  const table = `
    <table><thead><tr><th>Из · В</th><th>мм</th><th>Точность</th><th>Основание</th><th>Примечание</th></tr></thead><tbody>
    ${edges.map(edgeRow).join('') || '<tr><td colspan="5" class="muted">Рёбер нет</td></tr>'}
    </tbody></table>`;
  const branch = branches.length ? `
    <h1 class="h-sub" style="font-size:15px;margin:14px 0 4px">Ответвления</h1>
    <table><tbody>${branches.map(edgeRow).join('')}</tbody></table>` : '';
  const unclearBlock = unclear.length ? `
    <div class="notes"><b>Неопределённые значения (видимужу, но не понятные):</b>
      <ul>${unclear.map((item) => `<li><code>${esc(item.text ?? '')}</code> — ${esc(item.why ?? '')}</li>`).join('')}</ul>
    </div>` : '';
  const notes = (answer.notes || []).length ? `<div class="notes">${answer.notes.map(esc).join('<br>')}</div>` : '';
  return `
    <div class="line-card results">
      <div><b>${esc(line.line_id)}</b> <span class="pill-soft">${esc(line.result.page || '')}</span></div>
      ${kpis}
      ${orderBlock}
      ${table}
      ${branch}
      ${unclear}
      ${notes}
      <details><summary>Сырой ответ DeepSeek</summary><pre style="overflow:auto;max-height:320px;font-size:12px">${esc(JSON.stringify(answer, null, 2))}</pre></details>
    </div>`;
}

function renderRun(run) {
  renderRunProgress(run);
  if (run.status === 'complete') renderResults(run);
}

function renderRunProgress(run) {
  $('#run-status').innerHTML = `<div class="muted">Прогон <code>${esc(run.run_id)}</code> · статус: <b>${esc(run.status)}</b></div>`;
  const container = $('#lines-progress');
  container.innerHTML = '';
  for (const line of run.lines) {
    const badge = line.status === 'complete' ? 'ok' : line.status === 'error' ? 'err' : 'run';
    const events = (line.events || []).map((event) => `<li>${esc(event.stage)} · ${esc(event.message)}</li>`).join('');
    const card = `
      <div class="line-card">
        <div class="row"><b>${esc(line.line_id)}</b> <span class="badge ${badge}">${esc(line.status)} · ${esc(line.stage)}</span></div>
        ${line.error ? `<div class="muted">Ошибка: ${esc(line.error)}</div>` : ''}
        <ul class="events">${events || '<li>Ожидание…</li>'}</ul>
      </div>`;
    container.insertAdjacentHTML('beforeend', card);
  }
}

function renderResults(run) {
  const body = $('#result-body');
  body.innerHTML = run.lines.map((line) => {
    const answer = line.result?.answer;
    if (!answer) {
      return `<div class="line-card"><b>${esc(line.line_id)}</b>
        <div class="muted">${line.status === 'error' ? 'Ошибка: ' + esc(line.error) : 'Результат ещё не получен'}</div></div>`;
    }
    const edges = answer.edges || [];
    const branches = answer.branch_edges || [];
    const unclear = answer.unclear || [];
    const edgeRows = (list) => list.map((edge) => `
      <tr>
        <td>${esc(edge.from)} → ${esc(edge.to)}</td>
        <td>${edge.length_mm ?? '—'}</td>
        <td>${edge.exact
          ? '<span class="tag exact">точно</span>'
          : (edge.length_mm == null ? '<span class="tag missing">нет длины</span>' : '<span class="tag approx">неуверенно</span>')}</td>
        <td>${esc(edge.basis || '')}</td>
        <td>${esc(edge.note || '')}</td>
      </tr>`).join('');
    const exactLengths = edges.filter((edge) => edge.exact && typeof edge.length_mm === 'number').map((edge) => edge.length_mm);
    const approxLengths = edges.filter((edge) => !edge.exact && typeof edge.length_mm === 'number').map((edge) => edge.length_mm);
    const kpi = `
      <div class="kpi">
        <div><span>Сумма уверенных длин</span><strong>${exactLengths.reduce((a, b) => a + b, 0).toLocaleString('ru-RU')} мм</strong></div>
        <div><span>Сумма неуверенных</span><strong>${approxLengths.reduce((a, b) => a + b, 0)} мм</strong></div>
        <div><span>Рёбер / вершин</span><strong>${edges.length} / ${(line.result?.vertices || []).length}</strong></div>
        <div><span>Неопределено</span><strong>${unclear.length}</strong></div>
      </div>`;
    const order = answer.order || [];
    return `
      <div class="line-card results">
        <div><b>${esc(line.line_id)}</b> <span class="pill-soft">стр. ${esc(String(line.result.page || ''))}</span></div>
        ${kpis}
        ${order.length ? `<div class="muted">Порядок: ${order.map(esc).join(' → ')}</div>` : ''}
        <table>
          <thead><tr><th>Ребро</th><th>мм</th><th>Точность</th><th>Основание</th><th>Примечание</th></tr></thead>
          <tbody>${edgeRows(edges)}</tbody>
        </table>
        ${branches.length ? `<h1 style="font-size:15px;margin:14px 0 6px">Ответвления</h1>
          <table><tbody>${edgeRows(branches)}</tbody></table>` : ''}
        ${unclear.length ? `<div class="notes"><b>Неопределённые числа (что видно на чертеже):</b>
          <ul>${unclear.map((item) => `<li><code>${esc(item.text ?? '')}</code> — ${esc(item.why ?? '')}</li>`).join('')}</ul></div>` : ''}
        ${(answer.notes || []).length ? `<div class="notes">${answer.notes.map(esc).join('<br>')}</div>` : ''}
        <details><summary>Сырой ответ DeepSeek</summary><pre style="max-height:320px;overflow:auto;font-size:12px">${esc(JSON.stringify(answer, null, 2))}</pre></details>
      </div>`;
  }).join('<hr style="border-color:var(--line)">');
}

async function pollRun() {
  try {
    const run = await api(`/api/runs/${state.runId}`);
    renderRunProgress(run);
    if (run.status === 'complete') {
      renderResults(run);
      $('#step-result').hidden = false;
      return;
    }
  } catch (error) {
    $('#run-status').innerHTML = `<div class="badge err">Ошибка опроса: ${esc(error.message)}</div>`;
    return;
  }
  setTimeout(pollRun, 2000);
}

document.addEventListener('DOMContentLoaded', () => {
  $('#load-pdf').addEventListener('click', loadPdf);
  $('#use-default-pdf').addEventListener('change', loadDefaultPdf);
  $('#pdf-file').addEventListener('change', () => {
    const file = $('#pdf-file').files[0];
    if (file) {
      $('#pdf-path').value = '';
      $('#doc-summary').textContent = `Файл выбран: ${file.name}`;
      $('#use-default-pdf').checked = false;
      $('#custom-row').style.display = '';
    }
  });
  $('#start-analysis').addEventListener('click', startAnalysis);
  $('#group-search').addEventListener('input', (event) => {
    const query = String(event.currentTarget.value).trim().toUpperCase().replace(/-/g, '_');
    $$('#groups .chip').forEach((chip) => {
      const name = (chip.querySelector('.name')?.textContent ?? '').toUpperCase();
      const visible = !query || name.includes(query);
      chip.style.display = visible ? '' : 'none';
    });
  });
  init();
});
