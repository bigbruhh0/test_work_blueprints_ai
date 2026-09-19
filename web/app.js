const state = { document: null, runId: null, selected: new Set(), sourceMode: 'local', filesByLine: {}, tracesByLine: {}, selectedPage: {}, currentRun: null };
const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];
const esc = (value) => String(value ?? '').replace(/[&<>"]/g, (c) => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));

const STAGE_LABELS = {
  prepare: 'Подготовка файлов',
  analyze: 'Анализ у провайдера',
  done: 'Готово',
  error: 'Ошибка',
};

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
    const config = await api('/api/config');
    $('#key-status').textContent = config.deepseek
      ? config.model
      : 'Ключ DEEPSEEK_API_KEY не задан';
    $('#config-note').textContent = config.default_pdf ? config.default_pdf : '';
    $('#api-status').textContent = 'API подключено';
  } catch (error) {
    $('#key-status').textContent = 'Бэкенд недоступен';
    $('#config-note').textContent = String(error.message || error);
    $('#api-dot')?.classList.add('failed');
    $('#api-status').textContent = 'Ошибка API';
  }
}

let promptsState = { list: [], current: null };

async function showPrompts() {
  try {
    const list = await api('/api/prompts');
    promptsState.list = list;
    $('#prompt-section').hidden = false;
    $('#runs-section').hidden = true;
    $('#result-section').hidden = true;
    $('#history-section').hidden = true;
    renderPromptTabs(list);
    if (list.length && !promptsState.current) {
      await selectPrompt(list[0].name);
    }
  } catch (error) {
    $('#prompt-status').textContent = 'Ошибка: ' + error.message;
  }
}

function renderPromptTabs(list) {
  const container = $('#prompt-tabs');
  container.innerHTML = '';
  list.forEach(function (item) {
    const button = document.createElement('button');
    button.type = 'button';
    button.className = 'viewer-tab' + (promptsState.current === item.name ? ' on' : '');
    button.textContent = item.title + (item.source === 'override' ? ' ·' : '');
    button.dataset.name = item.name;
    button.addEventListener('click', function () { selectPrompt(item.name); });
    container.appendChild(button);
  });
}

async function selectPrompt(name) {
  promptsState.current = name;
  const item = promptsState.list.find(function (p) { return p.name === name; });
  $('#prompt-tabs').querySelectorAll('.viewer-tab').forEach(function (button) {
    button.classList.toggle('on', button.dataset.name === name);
  });
  try {
    const data = await api('/api/prompts/' + name);
    $('#prompt-editor').value = data.text;
    $('#prompt-meta').textContent = (item ? item.title : name) + ' · источник: ' + data.source;
  } catch (error) {
    $('#prompt-status').textContent = 'Ошибка: ' + error.message;
  }
}

async function saveCurrentPrompt() {
  const name = promptsState.current;
  if (!name) return;
  try {
    await api('/api/prompts/' + name, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ text: $('#prompt-editor').value }),
    });
    $('#prompt-status').textContent = 'Сохранено — применяется к новым запросам';
    const list = await api('/api/prompts');
    promptsState.list = list;
    renderPromptTabs(list);
    $('#prompt-meta').textContent = (promptsState.list.find((p) => p.name === name) || {}).title + ' · источник: override';
  } catch (error) {
    $('#prompt-status').textContent = 'Ошибка: ' + error.message;
  }
}

async function resetCurrentPrompt() {
  const name = promptsState.current;
  if (!name) return;
  try {
    await api('/api/prompts/' + name + '/reset', { method: 'POST' });
    await selectPrompt(name);
    $('#prompt-status').textContent = 'Сброшено к исходному';
    const list = await api('/api/prompts');
    promptsState.list = list;
    renderPromptTabs(list);
  } catch (error) {
    $('#prompt-status').textContent = 'Ошибка: ' + error.message;
  }
}

function applyDocument(data) {
  state.document = data;
  state.selected.clear();
  renderGroups(data.groups);
  $('#document-summary').innerHTML =
    '<strong>' + esc(data.source_name) + '</strong> · ' + data.pages_count + ' страниц · ' + data.groups.length + ' линий'
    + (data.cache ? ' · кеш: <code>' + esc(data.cache) + '</code>' : '');
  $('#document-summary').classList.remove('empty');
  $('#start-analysis').disabled = false;
}

function setLoading(label) {
  $('#document-summary').innerHTML = '<span class="spinner"></span> ' + esc(label);
  $('#document-summary').classList.remove('empty');
  const button = $('#load-pdf');
  button.disabled = true;
  const original = button.textContent;
  button.textContent = 'Читаю PDF...';
  return function restore() {
    button.disabled = false;
    button.textContent = original;
  };
}

async function loadLocalPdf() {
  const data = await api('/api/pdf/load-default', { method: 'POST' });
  applyDocument(data);
  return data;
}

async function loadPdf() {
  const radio = $$('.source-switch input').find((item) => item.checked);
  const mode = state.sourceMode || (radio ? radio.value : 'local');
  state.sourceMode = mode;
  const upload = mode === 'upload';
  const fileInput = $('#pdf-file');
  if (upload && !fileInput.files[0]) { alert('Сначала выберите файл'); return; }
  const restore = setLoading(upload ? 'Читаю выбранный файл…' : 'Читаю Изометрии.pdf…');
  try {
    if (upload) {
      const form = new FormData();
      form.append('file', fileInput.files[0]);
      const response = await fetch('/api/pdf/upload', { method: 'POST', body: form });
      if (!response.ok) {
        let detail = String(response.status);
        try { detail = (await response.json()).detail || detail; } catch {}
        throw new Error(detail);
      }
      applyDocument(await response.json());
    } else {
      await loadLocalPdf();
    }
  } catch (error) {
    $('#document-summary').innerHTML = '<span class="badge error">' + esc(error.message) + '</span>';
    alert(error.message);
  } finally {
    restore();
  }
}

function renderGroups(groups) {
  const container = $('#groups-list');
  container.innerHTML = '';
  for (const group of groups) {
    const row = document.createElement('label');
    row.className = 'group-item';
    row.innerHTML = '<input type="checkbox" value="' + esc(group.line_id) + '">'
      + '<span class="group-item-main"><span class="name">' + esc(group.line_id) + '</span></span>'
      + '<span class="group-item-meta">стр. ' + (group.pages || []).join(', ') + '</span>';
    const checkbox = row.querySelector('input');
    checkbox.addEventListener('change', () => {
      if (checkbox.checked) state.selected.add(group.line_id);
      else state.selected.delete(group.line_id);
      $('#groups-count').textContent = state.selected.size + ' выбрано';
    });
    container.appendChild(row);
  }
  $('#groups-count').textContent = groups.length + ' групп · 0 выбрано';
}

async function startAnalysis() {
  const lineIds = $$('#groups-list input:checked').map((item) => item.value);
  if (!lineIds.length) { alert('Выберите хотя бы одну линию'); return; }
  try {
    const response = await api('/api/runs', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ document_id: state.document.document_id, line_ids: lineIds, stop_stage: $('#stop-stage').value }),
    });
    state.runId = response.run_id;
    $('#runs-section').hidden = false;
    $('#result-section').hidden = true;
    $('#history-section').hidden = true;
    pollRun();
  } catch (error) { alert(error.message); }
}

async function pollRun() {
  try {
    const run = await api('/api/runs/' + state.runId);
    renderRun(run);
    if (run.status === 'complete') {
      $('#runs-section').hidden = true;
      renderResults(run);
      return;
    }
  } catch (error) {
    $('#run-status').innerHTML = '<div class="badge error">Ошибка: ' + esc(error.message) + '</div>';
    return;
  }
  setTimeout(pollRun, 1800);
}

function renderRun(run) {
  state.runId = run.run_id;
  $('#run-title').textContent = run.source_name;
  const running = run.status === 'running';
  $('#run-status').innerHTML = '<div class="muted">Прогон <code>' + esc(run.run_id) + '</code> · до этапа «' + esc(run.stop_stage) + '» · статус: <b>'
    + esc(run.status) + '</b>' + (running ? ' <span class="spinner"></span>' : '') + '</div>';
  const container = $('#lines-progress');
  container.innerHTML = '';
  for (const line of run.lines) {
    const badge = line.status === 'complete' ? 'ok' : line.status === 'error' ? 'error' : 'run';
    let pagesHtml = '';
    for (const pr of (line.page_results || [])) {
      const prRunning = pr.status === 'running' || pr.status === 'pending';
      const prBadge = pr.status === 'complete' ? 'ok' : pr.status === 'error' ? 'error' : 'run';
      const events = pr.events || [];
      const eventsHtml = events.map(function (event, index) {
        const isCurrent = prRunning && index === events.length - 1;
        return '<li class="' + (isCurrent ? 'current' : '') + '">'
          + (isCurrent ? '<span class="spinner"></span> ' : '')
          + '<span class="event-stage">' + esc(STAGE_LABELS[event.stage] || event.stage) + '</span>: ' + esc(event.message)
          + '</li>';
      }).join('');
      pagesHtml += '<div class="page-progress">'
        + '<div class="row"><span class="group-item-meta">Лист ' + pr.page_number + '</span>'
        + '<span class="badge ' + prBadge + '">' + esc(pr.status) + '</span>'
        + '<span class="badge run">' + esc(STAGE_LABELS[pr.stage] || pr.stage) + '</span></div>'
        + (pr.error ? '<div class="badge error">' + esc(pr.error) + '</div>' : '')
        + '<ul class="events">' + (eventsHtml || '') + '</ul>'
        + '</div>';
    }
    const card = document.createElement('div');
    card.className = 'line-card progress-line';
    card.innerHTML = '<div class="row"><b>' + esc(line.line_id) + '</b> <span class="badge ' + badge + '">'
      + esc(line.status) + '</span>'
      + '<span class="group-item-meta">стр. ' + (line.pages || []).join(', ') + '</span></div>'
      + (line.error ? '<div class="badge error">' + esc(line.error) + '</div>' : '')
      + pagesHtml;
    container.appendChild(card);
  }
}

const VIEWER_TABS = [
  { key: 'numbers_pdf', label: 'Разметка чисел' },
  { key: 'vertices_pdf', label: 'Вершины' },
  { key: 'numbers_txt', label: 'Числа (TXT)' },
];

function pageKey(lineId, pageNumber) { return lineId + '::' + pageNumber; }

function artifactLinkUrl(runId, lineId, filename) {
  return '/api/runs/' + encodeURIComponent(runId) + '/artifacts/' + encodeURIComponent(lineId) + '/' + encodeURIComponent(filename);
}

function viewerImageUrl(runId, lineId, filename) {
  return artifactLinkUrl(runId, lineId, filename) + '/preview';
}

function artifactLinks(lineId, pageResult, runId) {
  const files = pageResult.files || {};
  if (!Object.keys(files).length) return '';
  return '<div class="row artifacts">' + Object.entries(files).map(function (pair) {
    return '<a class="artifact-tag" href="' + artifactLinkUrl(runId, lineId, pair[1]) + '" download>⭳ ' + esc(pair[0]) + '</a>';
  }).join('') + '</div>';
}

function viewerCard(lineId, pageResult, runId) {
  const files = pageResult.files || {};
  const tabs = [];
  VIEWER_TABS.forEach(function (tab) {
    if (files[tab.key]) tabs.push({ key: tab.key, label: tab.label });
  });
  if (pageResult.provider_trace) tabs.push({ key: 'ai_trace', label: 'AI запрос' });
  if (!tabs.length) return '';
  const initial = tabs[0];
  const buttons = tabs.map(function (tab, index) {
    return '<button type="button" data-tab="' + esc(tab.key) + '" class="viewer-tab' + (index === 0 ? ' on' : '') + '">'
      + esc(tab.label) + '</button>';
  }).join('');
  const initialBody = initial.key === 'ai_trace'
    ? '<p class="muted">Откройте вкладку «AI запрос».</p>'
    : '<img class="viewer-frame" src="' + viewerImageUrl(runId, lineId, files[initial.key]) + '" alt="">';
  return '<div class="viewer" data-line="' + esc(lineId) + '" data-page="' + pageResult.page_number + '">'
    + '<div class="row viewer-tabs">' + buttons + '</div>'
    + '<div class="viewer-frame-wrap">' + initialBody + '</div>'
    + '</div>';
}

function bindViewer(card) {
  card.querySelectorAll('.viewer-tab').forEach(function (button) {
    button.addEventListener('click', function () { showViewerTab(card, button.dataset.tab); });
  });
}

function parseNumbersTxt(text) {
  const sections = { numbers: [], vertices: [] };
  let target = null;
  for (const rawLine of String(text).split(/\r?\n/)) {
    const line = rawLine.trim();
    if (!line) continue;
    if (line.startsWith('#')) {
      if (/размерные числа/i.test(line)) target = 'numbers';
      else if (/вершин/i.test(line) && /Найденные/i.test(line)) target = 'vertices';
      continue;
    }
    if (target) sections[target].push(line.split('\t').map(function (cell) { return cell.trim(); }));
  }
  return sections;
}

function renderTrace(trace) {
  if (!trace) return '<p class="muted">Запросов к ИИ не было.</p>';
  const payloadPretty = JSON.stringify(trace.payload || {}, null, 2);
  const answerRaw = trace.response_raw || '';
  const meta = (trace.model ? 'model: ' + esc(trace.model) + ' · ' : '')
    + (trace.status_code != null ? 'status: ' + esc(trace.status_code) + ' · ' : '')
    + (trace.elapsed_seconds != null ? 'время: ' + esc(trace.elapsed_seconds) + 's' : '');
  return '<div class="trace-block">'
    + '<p class="muted">' + meta + '</p>'
    + '<div class="trace-section"><b>Prompt</b><pre class="raw-json">' + esc(trace.prompt || '') + '</pre></div>'
    + '<div class="trace-section"><b>Payload</b><pre class="raw-json">' + esc(payloadPretty) + '</pre></div>'
    + '<div class="trace-section"><b>Ответ (raw)</b><pre class="raw-json">' + esc(answerRaw) + '</pre></div>'
    + '</div>';
}

async function showViewerTab(card, tabKey) {
  card.querySelectorAll('.viewer-tab').forEach(function (button) {
    button.classList.toggle('on', button.dataset.tab === tabKey);
  });
  const wrap = card.querySelector('.viewer-frame-wrap');
  if (!wrap) return;
  const runId = state.runId;
  const lineId = card.dataset.line || '';
  const pageNumber = card.dataset.page || '';
  const key = pageKey(lineId, pageNumber);
  const files = state.filesByLine[key] || {};
  if (tabKey === 'ai_trace') {
    wrap.innerHTML = renderTrace(state.tracesByLine[key]);
    return;
  }
  const filename = files[tabKey] || '';
  if (tabKey === 'numbers_txt') {
    wrap.innerHTML = '<span class="spinner"></span> Читаю TXT…';
    try {
      const response = await fetch(viewerImageUrl(runId, lineId, filename));
      const text = await response.text();
      const sections = parseNumbersTxt(text);
      const table = function (rows, header) {
        if (!rows.length) return '<p class="muted">' + esc(header) + ': строк нет</p>';
        return '<div><b>' + esc(header) + ' (' + rows.length + ')</b><table class="viewer-table"><tbody>'
          + rows.map(function (row) {
            return '<tr>' + row.map(function (cell) { return '<td>' + esc(cell) + '</td>'; }).join('') + '</tr>';
          }).join('') + '</tbody></table></div>';
      };
      wrap.innerHTML = table(sections.numbers, 'Числа') + table(sections.vertices, 'Вершины');
    } catch (error) {
      wrap.innerHTML = '<div class="badge error">Ошибка: ' + esc(error.message) + '</div>';
    }
    return;
  }
  wrap.innerHTML = '<img class="viewer-frame" src="' + viewerImageUrl(runId, lineId, filename) + '" alt="">';
}

function collectMaps(run) {
  state.filesByLine = {};
  state.tracesByLine = {};
  for (const line of run.lines) {
    for (const pr of (line.page_results || [])) {
      const key = pageKey(line.line_id, pr.page_number);
      state.filesByLine[key] = pr.files || {};
      if (pr.provider_trace) state.tracesByLine[key] = pr.provider_trace;
    }
  }
}

function edgeRowHtml(segment) {
  return '<tr><td>' + esc(segment.from) + ' → ' + esc(segment.to) + '</td><td><b>' + esc(segment.value ?? '—') + '</b></td>'
    + '<td class="mono">' + esc(String(segment.coords || [])) + '</td></tr>';
}

function pageDetailHtml(lineId, pr) {
  const analysis = pr.analysis;
  const downloads = artifactLinks(lineId, pr, state.runId);
  const viewer = viewerCard(lineId, pr, state.runId);
  if (!analysis) {
    return '<div class="muted">Лист ' + pr.page_number + ': '
      + (pr.status === 'error' ? '<span class="badge error">' + esc(pr.error || 'Ошибка') + '</span>'
        : 'анализ не запускался (этап подготовки)') + '</div>'
      + downloads + viewer;
  }
  const main = analysis.main_chain || {};
  const segments = main.segments || [];
  const total = segments.reduce(function (sum, segment) {
    return sum + (typeof segment.value === 'number' ? segment.value : 0);
  }, 0);
  const branches = analysis.branches || [];
  const skipped = analysis.skipped || [];
  return '<h4>Лист ' + pr.page_number + ' — сумма ' + total + ' мм</h4>'
    + ((main.path || []).length ? '<p class="muted">Путь: ' + main.path.map(esc).join(' → ') + '</p>' : '')
    + '<table class="data-table"><thead><tr><th>Ребро</th><th>Размер</th><th>Координаты числа</th></tr></thead><tbody>'
    + (segments.map(edgeRowHtml).join('') || '<tr><td colspan="3" class="muted">Сегментов нет</td></tr>')
    + '</tbody></table>'
    + (branches.length ? '<h3>Ответвления</h3><table class="data-table"><thead><tr><th>Ответвл.</th><th>Путь</th><th>Размеры</th></tr></thead><tbody>'
        + branches.map(function (branch) {
          return '<tr><td>' + esc(branch.junction) + ' → ' + esc(branch.endpoint) + '</td><td>' + esc((branch.path || []).join(' → ')) + '</td><td>'
            + (branch.segments || []).map(function (segment) { return esc(String(segment.value ?? '—')); }).join(', ') + '</td></tr>';
        }).join('') + '</tbody></table>' : '')
    + (skipped.length ? '<div class="notes"><b>Непривязанные числа</b><ul>'
        + skipped.map(function (item) {
          return '<li><code>' + esc(item.value) + '</code> — ' + esc(item.reason || '') + '</li>';
        }).join('') + '</ul></div>' : '')
    + downloads
    + viewer;
}

function renderResults(run) {
  state.runId = run.run_id;
  state.currentRun = run;
  collectMaps(run);
  $('#result-section').hidden = false;
  $('#result-title').textContent = run.run_id;
  const body = $('#result-body');
  body.innerHTML = run.lines.map(function (line) {
    const pages = line.page_results || [];
    let totalMain = 0;
    let edgeCount = 0;
    let totalBranches = 0;
    let totalSkipped = 0;
    pages.forEach(function (pr) {
      const a = pr.analysis;
      if (!a) return;
      const segs = (a.main_chain || {}).segments || [];
      totalMain += segs.reduce(function (sum, segment) {
        return sum + (typeof segment.value === 'number' ? segment.value : 0);
      }, 0);
      edgeCount += segs.length;
      totalBranches += (a.branches || []).length;
      totalSkipped += (a.skipped || []).length;
    });
    const selectedPage = state.selectedPage[line.line_id];
    const pageButtons = pages.map(function (pr) {
      return '<button type="button" class="viewer-tab page-selector' + (selectedPage === pr.page_number ? ' on' : '') + '" data-line="' + esc(line.line_id) + '" data-page="' + pr.page_number + '">Лист ' + pr.page_number + '</button>';
    }).join('');
    const firstPage = selectedPage ? pages.find(function (p) { return p.page_number === selectedPage; }) || pages[0] : pages[0];
    return '<div class="line-card">'
      + '<h3>' + esc(line.line_id) + ' <span class="group-item-meta">стр. ' + (line.pages || []).join(', ') + '</span></h3>'
      + '<div class="kpi">'
      + '<div><span class="kpi-label">Сумма (все листы)</span><strong>' + totalMain + ' мм</strong></div>'
      + '<div><span class="kpi-label">Рёбер</span><strong>' + edgeCount + '</strong></div>'
      + '<div><span class="kpi-label">Ответвлений</span><strong>' + totalBranches + '</strong></div>'
      + '<div><span class="kpi-label">Непривязанных</span><strong>' + totalSkipped + '</strong></div>'
      + '</div>'
      + '<div class="row page-selector-row">' + pageButtons + '</div>'
      + '<div class="page-content" data-line="' + esc(line.line_id) + '">'
      + (firstPage ? pageDetailHtml(line.line_id, firstPage) : '<p class="muted">Страниц нет</p>')
      + '</div>'
      + '</div>';
  }).join('<hr>');
  bindPageSelectors();
  $('#result-body').querySelectorAll('.viewer').forEach(bindViewer);
}

function bindPageSelectors() {
  $('#result-body').querySelectorAll('.page-selector').forEach(function (button) {
    button.addEventListener('click', function () {
      const lineId = button.dataset.line;
      const pageNumber = Number(button.dataset.page);
      state.selectedPage[lineId] = pageNumber;
      const card = button.closest('.line-card');
      card.querySelectorAll('.page-selector').forEach(function (b) {
        b.classList.toggle('on', b === button);
      });
      const content = card.querySelector('.page-content');
      const line = (state.currentRun.lines || []).find(function (l) { return l.line_id === lineId; });
      const pr = (line.page_results || []).find(function (p) { return p.page_number === pageNumber; });
      if (pr) {
        content.innerHTML = pageDetailHtml(lineId, pr);
        content.querySelectorAll('.viewer').forEach(bindViewer);
      }
    });
  });
}

async function showHistory() {
  try {
    const runs = await api('/api/runs');
    $('#history-section').hidden = false;
    $('#runs-section').hidden = true;
    $('#result-section').hidden = true;
    const body = $('#history-body');
    if (!runs.length) {
      body.innerHTML = '<p class="muted">История пуста — запустите первый прогон.</p>';
      return;
    }
    body.innerHTML = runs.map(function (run) {
      const badge = run.status === 'complete' ? 'ok' : run.status === 'error' ? 'error' : 'run';
      const when = run.created_at ? new Date(run.created_at).toLocaleString('ru-RU') : '';
      const kindLabel = run.kind_label || run.stop_stage || '';
      const model = run.model ? ' · ' + run.model : '';
      return '<div class="line-card history-row" data-run-id="' + esc(run.run_id) + '">'
        + '<div class="row"><b>' + esc(run.source_name || '') + '</b> <span class="badge ' + badge + '">' + esc(run.status) + '</span>'
        + '<span class="badge kind">' + esc(kindLabel) + '</span>'
        + '<span class="group-item-meta">' + esc(when) + '</span></div>'
        + '<div class="muted">' + esc(run.run_id) + ' · ' + (run.line_ids || []).map(esc).join(', ') + esc(model) + '</div>'
        + '<button class="open-run" type="button" data-open-run="' + esc(run.run_id) + '">Открыть →</button>'
        + '</div>';
    }).join('');
    $('#history-body').querySelectorAll('[data-open-run]').forEach(function (button) {
      button.addEventListener('click', function () { openHistoryRun(button.dataset.openRun); });
    });
  } catch (error) {
    $('#history-body').innerHTML = '<div class="badge error">' + esc(error.message) + '</div>';
  }
}

async function openHistoryRun(runId) {
  try {
    const run = await api('/api/runs/' + runId);
    state.runId = runId;
    $('#history-section').hidden = true;
    renderResults(run);
  } catch (error) {
    alert(error.message);
  }
}

document.addEventListener('DOMContentLoaded', function () {
  $$('.source-switch input').forEach(function (radio) {
    radio.addEventListener('change', function () {
      $('#pdf-file').style.display = (radio.checked && radio.value === 'upload') ? '' : 'none';
    });
  });
  $('#pdf-file').addEventListener('change', function () {
    const file = $('#pdf-file').files[0];
    if (file) $('#document-summary').textContent = 'Файл выбран: ' + file.name;
  });
  $('#load-pdf').addEventListener('click', loadPdf);
  $('#start-analysis').addEventListener('click', startAnalysis);
  $('#history-button').addEventListener('click', showHistory);
  $('#prompts-button').addEventListener('click', showPrompts);
  $('#save-prompt').addEventListener('click', saveCurrentPrompt);
  $('#reset-prompt').addEventListener('click', resetCurrentPrompt);
  $('#groups-search').addEventListener('input', function (event) {
    const query = String(event.currentTarget.value).trim().toUpperCase().replace(/-/g, '_');
    $$('#groups-list .group-item').forEach(function (row) {
      const name = (row.querySelector('.name')?.textContent ?? '').toUpperCase();
      row.style.display = (!query || name.includes(query)) ? '' : 'none';
    });
  });
  init();
});
