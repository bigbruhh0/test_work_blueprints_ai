const state = { document: null, runId: null, selected: new Set(), sourceMode: 'local', filesByLine: {}, tracesByLine: {}, graphsByLine: {}, selectedPage: {}, currentRun: null, three: null, viewers3d: new Set() };
const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];
const esc = (value) => String(value ?? '').replace(/[&<>"]/g, (c) => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));

const STAGE_LABELS = {
  prepare: 'Локальная подготовка + разметка',
  dimensions: 'Локальная подготовка + привязка размеров',
  dimension_review: 'Карта размеров + проверка провайдером',
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
    const model = config.deepseek ? config.model : 'ключ DEEPSEEK_API_KEY не задан';
    $('#api-status').textContent = config.deepseek ? ('API: ' + config.model) : 'API: ключ не задан';
    $('#source-status').textContent = 'Провайдер: ' + model + (config.default_pdf ? ' · файл: ' + config.default_pdf : '');
  } catch (error) {
    $('#api-status').textContent = 'Ошибка API';
    $('#source-status').textContent = 'Бэкенд недоступен: ' + String(error.message || error);
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
    $('#prompt-section').scrollIntoView({ behavior: 'smooth', block: 'start' });
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
    $('#prompt-meta').textContent = (item ? item.title : name) + (item && item.usage ? ' · используется: ' + item.usage : '') + ' · источник: ' + data.source
      + ' · версия: ' + (data.version ?? 'default') + ' · sha256: ' + String(data.sha256 || '').slice(0, 12);
    await loadPromptVersions(name);
  } catch (error) {
    $('#prompt-status').textContent = 'Ошибка: ' + error.message;
  }
}

async function loadPromptVersions(name) {
  try {
    const data = await api('/api/prompts/' + name + '/versions');
    const versions = data.versions || [];
    const container = $('#prompt-versions');
    if (!versions.length) {
      container.innerHTML = '<p class="muted">Версий пока нет — сохраните промпт, чтобы появилась первая версия.</p>';
      return;
    }
    container.innerHTML = versions.map(function (version) {
      const when = version.created_at ? new Date(version.created_at).toLocaleString('ru-RU') : '';
      return '<div class="version-row">'
        + '<span class="mono">v' + version.version + '</span>'
        + '<span class="muted">' + esc(when) + ' · ' + version.length + ' симв.</span>'
        + '<button type="button" class="viewer-tab" data-restore-version="' + version.version + '">Восстановить</button>'
        + '</div>';
    }).join('');
    container.querySelectorAll('[data-restore-version]').forEach(function (button) {
      button.addEventListener('click', function () { restorePromptVersion(Number(button.dataset.restoreVersion)); });
    });
  } catch (error) {
    $('#prompt-versions').innerHTML = '<div class="badge error">' + esc(error.message) + '</div>';
  }
}

async function restorePromptVersion(version) {
  const name = promptsState.current;
  if (!name) return;
  try {
    await api('/api/prompts/' + name + '/restore', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ version: version }),
    });
    await selectPrompt(name);
    $('#prompt-status').textContent = 'Восстановлена версия v' + version;
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
    await selectPrompt(name);
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

function parseExcludedPages(rawValue) {
  if (!rawValue || !rawValue.trim()) return [];
  const numbers = new Set();
  for (const token of rawValue.split(',')) {
    const text = token.trim();
    if (!text) continue;
    if (text.includes('-')) {
      const [startText, endText] = text.split('-', 2);
      const start = Number(startText.trim());
      const end = Number(endText.trim());
      if (!Number.isFinite(start) || !Number.isFinite(end) || start <= 0 || end <= 0 || start > end) {
        throw new Error('Некорректный диапазон листов: ' + text);
      }
      for (let page = start; page <= end; page += 1) numbers.add(page);
      continue;
    }
    const page = Number(text);
    if (!Number.isFinite(page) || page <= 0) {
      throw new Error('Некорректный номер листа: ' + text);
    }
    numbers.add(page);
  }
  return Array.from(numbers).sort((a, b) => a - b);
}

async function startAnalysis() {
  const lineIds = $$('#groups-list input:checked').map((item) => item.value);
  if (!lineIds.length) { alert('Выберите хотя бы одну линию'); return; }
  let excludedPages = [];
  try {
    excludedPages = parseExcludedPages($('#exclude-pages').value);
  } catch (error) {
    alert(error.message);
    return;
  }
  try {
    const response = await api('/api/runs', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ document_id: state.document.document_id, line_ids: lineIds, stop_stage: $('#stop-stage').value, excluded_pages: excludedPages }),
    });
    state.runId = response.run_id;
    $('#runs-section').hidden = false;
    $('#result-section').hidden = true;
    $('#history-section').hidden = true;
    $('#prompt-section').hidden = true;
    $('#runs-section').scrollIntoView({ behavior: 'smooth', block: 'start' });
    pollRun();
  } catch (error) { alert(error.message); }
}

async function pollRun() {
  try {
    const run = await api('/api/runs/' + state.runId);
    renderRun(run);
    if (run.status === 'complete' || run.status === 'error') {
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
  { key: 'clean_local_markup_pdf', label: 'Локальная разметка (чистая)' },
  { key: 'numbers_pdf', label: 'Разметка чисел' },
  { key: 'vertices_pdf', label: 'Вершины' },
  { key: 'coordinates_pdf', label: 'Координаты' },
  { key: 'numbers_txt', label: 'Числа (TXT)' },
  { key: 'preprocess_annotations_pdf', label: 'Локальная разметка (старая)' },
  { key: 'dimensions_pdf', label: 'Размеры на графе' },
  { key: 'dimension_graph_pdf', label: 'Чистый граф трубы' },
  { key: 'dimension_skeleton_pdf', label: 'Контур и размерные линии' },
  { key: 'dimensions_json', label: 'Карта размеров (JSON)' },
  { key: 'dimension_map_pdf', label: 'Диагностическая карта' },
  { key: 'dimension_review_json', label: 'Решение провайдера (JSON)' },
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
  if (pageResult.analysis && pageResult.analysis.graph) tabs.push({ key: 'graph', label: 'Граф' });
  if (state.graphsByLine[lineId]) tabs.push({ key: 'graph_3d', label: '3D граф' });
  if (pageResult.provider_trace) tabs.push({ key: 'ai_trace', label: 'AI запрос' });
  if (!tabs.length) return '';
  const initial = tabs[0];
  const buttons = tabs.map(function (tab, index) {
    return '<button type="button" data-tab="' + esc(tab.key) + '" class="viewer-tab' + (index === 0 ? ' on' : '') + '">'
      + esc(tab.label) + '</button>';
  }).join('');
  const initialBody = initial.key === 'ai_trace'
    ? '<p class="muted">Откройте вкладку «AI запрос».</p>'
    : initial.key === 'graph'
      ? renderGraph(pageResult.analysis.graph)
    : initial.key === 'graph_3d'
      ? '<div class="graph-3d-loading"><span class="spinner"></span> Загружаю 3D-сцену...</div>'
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
  const sections = { numbers: [], coordinates: [], vertices: [] };
  let target = null;
  for (const rawLine of String(text).split(/\r?\n/)) {
    const line = rawLine.trim();
    if (!line) continue;
    if (line.startsWith('#')) {
      if (/размерные числа/i.test(line)) target = 'numbers';
      else if (/координат/i.test(line) && /Найденные/i.test(line)) target = 'coordinates';
      else if (/вершин/i.test(line) && /Найденные/i.test(line)) target = 'vertices';
      continue;
    }
    if (target) sections[target].push(line.split('\t').map(function (cell) { return cell.trim(); }));
  }
  return sections;
}

function renderTrace(trace) {
  if (!trace) return '<p class="muted">Запросов к ИИ не было.</p>';
  const pretty = function (value) {
    if (value == null) return '';
    if (typeof value === 'string') return value;
    try { return JSON.stringify(value, null, 2); } catch { return String(value); }
  };
  const payloadPretty = JSON.stringify(trace.payload || {}, null, 2);
  const responsePretty = pretty(trace.response_raw);
  const answerPretty = pretty(trace.answer || trace.parsed_answer);
  const meta = (trace.model ? 'model: ' + esc(trace.model) + ' · ' : '')
    + (trace.prompt_version != null ? 'промпт: v' + esc(trace.prompt_version) + ' · ' : '')
    + (trace.prompt_sha256 ? 'sha256: ' + esc(String(trace.prompt_sha256).slice(0, 12)) + ' · ' : '')
    + (trace.status_code != null ? 'status: ' + esc(trace.status_code) + ' · ' : '')
    + (trace.elapsed_seconds != null ? 'время: ' + esc(trace.elapsed_seconds) + 's' : '');
  return '<div class="trace-block">'
    + '<p class="muted">' + meta + '</p>'
    + '<div class="trace-section"><b>Prompt</b><pre class="raw-json">' + esc(trace.prompt || '') + '</pre></div>'
    + '<div class="trace-section"><b>Payload</b><pre class="raw-json">' + esc(payloadPretty) + '</pre></div>'
    + '<div class="trace-section"><b>Ответ (raw)</b><pre class="raw-json">' + esc(responsePretty) + '</pre></div>'
    + (answerPretty ? '<div class="trace-section"><b>Ответ (parsed)</b><pre class="raw-json">' + esc(answerPretty) + '</pre></div>' : '')
    + '</div>';
}

function renderGraph(graph) {
  const nodes = Array.isArray(graph && graph.nodes) ? graph.nodes : [];
  const edges = Array.isArray(graph && graph.edges) ? graph.edges : [];
  if (!nodes.length && !edges.length) return '<p class="muted">Граф не распознан.</p>';

  const nodeById = new Map(nodes.map(function (node) { return [String(node.id), node]; }));
  const numericPoints = nodes
    .map(function (node) { return { node: node, x: Number(node.x), y: Number(node.y) }; })
    .filter(function (item) { return Number.isFinite(item.x) && Number.isFinite(item.y); });
  const minX = numericPoints.length ? Math.min.apply(null, numericPoints.map(function (item) { return item.x; })) : 0;
  const maxX = numericPoints.length ? Math.max.apply(null, numericPoints.map(function (item) { return item.x; })) : 1;
  const minY = numericPoints.length ? Math.min.apply(null, numericPoints.map(function (item) { return item.y; })) : 0;
  const maxY = numericPoints.length ? Math.max.apply(null, numericPoints.map(function (item) { return item.y; })) : 1;
  const spanX = Math.max(maxX - minX, 1);
  const spanY = Math.max(maxY - minY, 1);
  const positions = new Map();
  nodes.forEach(function (node, index) {
    const x = Number(node.x);
    const y = Number(node.y);
    positions.set(String(node.id), Number.isFinite(x) && Number.isFinite(y)
      ? { x: 70 + ((x - minX) / spanX) * 760, y: 55 + ((y - minY) / spanY) * 280 }
      : { x: 90 + (index % 6) * 145, y: 100 + Math.floor(index / 6) * 130 });
  });
  const point = function (id) { return positions.get(String(id)); };
  const edgeSvg = edges.map(function (edge) {
    const from = point(edge.from_node_id);
    const to = point(edge.to_node_id);
    if (!from || !to) return '';
    return '<line class="graph-edge" x1="' + from.x.toFixed(1) + '" y1="' + from.y.toFixed(1)
      + '" x2="' + to.x.toFixed(1) + '" y2="' + to.y.toFixed(1) + '"></line>';
  }).join('');
  const nodeSvg = nodes.map(function (node) {
    const position = point(node.id);
    if (!position) return '';
    const label = node.id == null ? '—' : String(node.id);
    const coordinates = [node.x, node.y, node.z].every(function (value) { return value != null; })
      ? [node.x, node.y, node.z].join(' / ') : 'координаты не определены';
    return '<g class="graph-node" tabindex="0"><circle cx="' + position.x.toFixed(1) + '" cy="' + position.y.toFixed(1) + '" r="12"></circle>'
      + '<text x="' + (position.x + 18).toFixed(1) + '" y="' + (position.y - 3).toFixed(1) + '">' + esc(label) + '</text>'
      + '<text class="graph-node-meta" x="' + (position.x + 18).toFixed(1) + '" y="' + (position.y + 13).toFixed(1) + '">' + esc(coordinates) + '</text></g>';
  }).join('');
  const duplicateCount = Array.isArray(graph.duplicate_edges) ? graph.duplicate_edges.length : 0;
  const isolatedCount = Array.isArray(graph.isolated_nodes) ? graph.isolated_nodes.length : 0;
  return '<div class="graph-view">'
    + '<svg class="graph-canvas" viewBox="0 0 900 390" role="img" aria-label="Граф соединения вершин">'
    + '<defs><marker id="graph-arrow" markerWidth="8" markerHeight="8" refX="7" refY="4" orient="auto"><path d="M0,0 L8,4 L0,8 z"></path></marker></defs>'
    + edgeSvg + nodeSvg + '</svg>'
    + '<div class="graph-summary"><span>Узлов: <b>' + nodes.length + '</b></span><span>Рёбер: <b>' + edges.length + '</b></span>'
    + '<span>Дубликатов: <b>' + duplicateCount + '</b></span><span>Изолированных: <b>' + isolatedCount + '</b></span></div>'
    + '</div>';
}

function combinedGraphForLine(line) {
  const nodes = new Map();
  const edges = new Map();
  const isolated = new Set();
  const duplicates = [];
  const dimensions = new Map();
  const rememberDimension = function (segment) {
    if (!segment || typeof segment !== 'object') return;
    const from = segment.from || segment.from_node_id || segment.start || segment.start_node_id;
    const to = segment.to || segment.to_node_id || segment.end || segment.end_node_id;
    const value = segment.value ?? segment.length_mm ?? segment.length;
    if (from != null && to != null && value != null) dimensions.set(String(from) + '->' + String(to), value);
  };
  (line.page_results || []).forEach(function (pageResult) {
    const graph = pageResult.analysis && pageResult.analysis.graph;
    if (!graph) return;
    const analysis = pageResult.analysis || {};
    ((analysis.main_chain && analysis.main_chain.segments) || []).forEach(rememberDimension);
    (analysis.branches || []).forEach(function (branch) {
      (branch.segments || []).forEach(rememberDimension);
    });
    (Array.isArray(graph.nodes) ? graph.nodes : []).forEach(function (node, index) {
      const id = String(node.id || ('P' + pageResult.page_number + '-N' + index));
      if (nodes.has(id)) {
        const existing = nodes.get(id);
        if (existing.x == null && node.x != null) Object.assign(existing, node);
        existing.pages = Array.from(new Set((existing.pages || []).concat(pageResult.page_number)));
      } else {
        nodes.set(id, Object.assign({}, node, { id: id, page: pageResult.page_number, pages: [pageResult.page_number] }));
      }
    });
    (Array.isArray(graph.edges) ? graph.edges : []).forEach(function (edge, index) {
      const from = String(edge.from_node_id || '');
      const to = String(edge.to_node_id || '');
      if (!from || !to) return;
      const key = from + '->' + to;
      if (edges.has(key)) {
        duplicates.push({ from_node_id: from, to_node_id: to, page: pageResult.page_number });
        return;
      }
      edges.set(key, Object.assign({}, edge, {
        id: edge.id || ('E' + (edges.size + 1)),
        from_node_id: from,
        to_node_id: to,
        page: pageResult.page_number,
        dimension_mm: edge.length_mm ?? edge.value ?? dimensions.get(key),
      }));
    });
    (Array.isArray(graph.isolated_nodes) ? graph.isolated_nodes : []).forEach(function (id) { isolated.add(String(id)); });
  });
  const connected = new Set();
  edges.forEach(function (edge) { connected.add(edge.from_node_id); connected.add(edge.to_node_id); });
  nodes.forEach(function (_node, id) { if (!connected.has(id)) isolated.add(id); });
  return { nodes: Array.from(nodes.values()), edges: Array.from(edges.values()), duplicate_edges: duplicates, isolated_nodes: Array.from(isolated) };
}

async function loadThree() {
  if (!state.three) {
    const modules = await Promise.all([
      import('three'),
      import('three/addons/controls/OrbitControls.js'),
    ]);
    state.three = { THREE: modules[0], OrbitControls: modules[1].OrbitControls };
  }
  return state.three;
}

function graphPoint(node, index, THREE, bounds) {
  const values = [node.x, node.y, node.z].map(Number);
  if (values.every(Number.isFinite)) {
    return new THREE.Vector3(
      (values[0] - bounds.min[0]) / bounds.span[0] * 12 - 6,
      (values[2] - bounds.min[2]) / bounds.span[2] * 8 - 4,
      -(values[1] - bounds.min[1]) / bounds.span[1] * 12 + 6,
    );
  }
  const angle = (index / Math.max(bounds.count, 1)) * Math.PI * 2;
  return new THREE.Vector3(Math.cos(angle) * 3, 0, Math.sin(angle) * 3);
}

function floatingLabel(text, color, THREE) {
  const canvas = document.createElement('canvas');
  canvas.width = 320;
  canvas.height = 72;
  const context = canvas.getContext('2d');
  context.font = '700 28px sans-serif';
  context.textAlign = 'center';
  context.textBaseline = 'middle';
  context.fillStyle = 'rgba(255,255,255,.78)';
  context.roundRect(3, 3, 314, 66, 10);
  context.fill();
  context.fillStyle = color;
  context.fillText(String(text), 160, 36);
  const texture = new THREE.CanvasTexture(canvas);
  texture.colorSpace = THREE.SRGBColorSpace;
  const sprite = new THREE.Sprite(new THREE.SpriteMaterial({ map: texture, transparent: true, depthTest: false, opacity: 0.86 }));
  sprite.scale.set(1.7, 0.38, 1);
  return sprite;
}

async function render3DGraph(container, graph) {
  if (!graph || !graph.nodes || !graph.nodes.length) {
    container.innerHTML = '<p class="muted">Для 3D-графа нет узлов с результата анализа.</p>';
    return;
  }
  container.innerHTML = '<div class="graph-3d-loading"><span class="spinner"></span> Загружаю 3D-сцену...</div>';
  try {
    const { THREE, OrbitControls } = await loadThree();
    const canvas = document.createElement('canvas');
    canvas.className = 'graph-3d-canvas';
    const shell = document.createElement('div');
    shell.className = 'graph-3d-shell';
    const toolbar = document.createElement('div');
    toolbar.className = 'graph-3d-toolbar';
    toolbar.innerHTML = '<span>ЛКМ: вращение · колесо: масштаб · ПКМ: панорама</span><button type="button" class="secondary-button graph-3d-fullscreen">На весь экран</button>';
    shell.appendChild(canvas);
    shell.appendChild(toolbar);
    container.replaceChildren(shell);

    const scene = new THREE.Scene();
    scene.background = new THREE.Color(0xf3f7f4);
    const camera = new THREE.PerspectiveCamera(45, 1, 0.1, 1000);
    camera.position.set(10, 8, 12);
    const renderer = new THREE.WebGLRenderer({ canvas: canvas, antialias: true, alpha: false });
    renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 2));
    const controls = new OrbitControls(camera, canvas);
    controls.enableDamping = true;
    controls.target.set(0, 0, 0);
    scene.add(new THREE.AmbientLight(0xffffff, 1.8));
    const keyLight = new THREE.DirectionalLight(0xffffff, 2.2);
    keyLight.position.set(5, 10, 8);
    scene.add(keyLight);

    const numeric = graph.nodes.map(function (node) { return [node.x, node.y, node.z].map(Number); }).filter(function (values) { return values.every(Number.isFinite); });
    const min = [0, 0, 0].map(function (_, axis) { return numeric.length ? Math.min.apply(null, numeric.map(function (v) { return v[axis]; })) : 0; });
    const max = [0, 0, 0].map(function (_, axis) { return numeric.length ? Math.max.apply(null, numeric.map(function (v) { return v[axis]; })) : 1; });
    const bounds = { min: min, span: max.map(function (value, axis) { return Math.max(value - min[axis], 1); }), count: graph.nodes.length };
    const positions = new Map();
    graph.nodes.forEach(function (node, index) { positions.set(String(node.id), graphPoint(node, index, THREE, bounds)); });

    const edgePositions = [];
    graph.edges.forEach(function (edge) {
      const from = positions.get(String(edge.from_node_id));
      const to = positions.get(String(edge.to_node_id));
      if (from && to) {
        edgePositions.push(from.x, from.y, from.z, to.x, to.y, to.z);
        const direction = to.clone().sub(from);
        const length = direction.length();
        if (length > 0.001) {
          scene.add(new THREE.ArrowHelper(direction.normalize(), from, length, 0xd97706, 0.28, 0.16));
        }
        if (edge.dimension_mm != null && edge.dimension_mm !== '') {
          const label = floatingLabel(String(edge.dimension_mm) + ' мм', '#9a5a00', THREE);
          label.position.copy(from).add(to).multiplyScalar(0.5);
          label.position.y += 0.3;
          scene.add(label);
        }
      }
    });
    if (edgePositions.length) {
      const geometry = new THREE.BufferGeometry();
      geometry.setAttribute('position', new THREE.Float32BufferAttribute(edgePositions, 3));
      scene.add(new THREE.LineSegments(geometry, new THREE.LineBasicMaterial({ color: 0x27856a }))); 
    }
    const nodeGeometry = new THREE.SphereGeometry(0.16, 18, 12);
    const nodeMaterial = new THREE.MeshStandardMaterial({ color: 0x176044, roughness: 0.55 });
    graph.nodes.forEach(function (node) {
      const mesh = new THREE.Mesh(nodeGeometry, nodeMaterial);
      mesh.position.copy(positions.get(String(node.id)));
      mesh.userData.label = String(node.id);
      scene.add(mesh);
    });
    const grid = new THREE.GridHelper(14, 14, 0xc7d7ce, 0xe0e9e4);
    grid.position.y = -4.1;
    scene.add(grid);

    const axisLength = 3.2;
    const axes = [
      { direction: new THREE.Vector3(1, 0, 0), color: 0xdc2626, label: '+X', position: new THREE.Vector3(axisLength + 0.25, 0, 0) },
      { direction: new THREE.Vector3(0, 0, -1), color: 0x2563eb, label: '+Y', position: new THREE.Vector3(0, 0, -axisLength - 0.25) },
      { direction: new THREE.Vector3(0, 1, 0), color: 0x16a34a, label: '+Z', position: new THREE.Vector3(0, axisLength + 0.25, 0) },
    ];
    const axisGroup = new THREE.Group();
    axisGroup.position.set(-5.4, -3.6, 5.4);
    axes.forEach(function (axis) {
      const arrow = new THREE.ArrowHelper(axis.direction, new THREE.Vector3(0, 0, 0), axisLength, axis.color, 0.38, 0.2);
      arrow.line.material.transparent = true;
      arrow.line.material.opacity = 0.42;
      arrow.cone.material.transparent = true;
      arrow.cone.material.opacity = 0.42;
      axisGroup.add(arrow);
      const label = floatingLabel(axis.label, '#' + axis.color.toString(16).padStart(6, '0'), THREE);
      label.position.copy(axis.direction).multiplyScalar(axisLength + 0.55);
      label.scale.set(0.75, 0.24, 1);
      axisGroup.add(label);
    });
    scene.add(axisGroup);

    const resize = function () {
      const width = Math.max(shell.clientWidth, 320);
      const height = Math.max(shell.clientHeight - toolbar.offsetHeight, 300);
      renderer.setSize(width, height, false);
      camera.aspect = width / height;
      camera.updateProjectionMatrix();
    };
    const resizeObserver = new ResizeObserver(resize);
    resizeObserver.observe(shell);
    resize();
    const animate = function () {
      if (!document.contains(shell)) { resizeObserver.disconnect(); renderer.dispose(); return; }
      controls.update();
      renderer.render(scene, camera);
      requestAnimationFrame(animate);
    };
    animate();
    toolbar.querySelector('.graph-3d-fullscreen').addEventListener('click', function () {
      if (shell.requestFullscreen) shell.requestFullscreen();
    });
  } catch (error) {
    container.innerHTML = '<div class="badge error">Не удалось загрузить 3D-модуль: ' + esc(error.message) + '</div>';
  }
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
  if (tabKey === 'graph') {
    wrap.innerHTML = renderGraph(state.graphsByLine[key]);
    return;
  }
  if (tabKey === 'graph_3d') {
    await render3DGraph(wrap, state.graphsByLine[lineId]);
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
      wrap.innerHTML = table(sections.numbers, 'Числа') + table(sections.coordinates, 'Координаты') + table(sections.vertices, 'Вершины');
    } catch (error) {
      wrap.innerHTML = '<div class="badge error">Ошибка: ' + esc(error.message) + '</div>';
    }
    return;
  }
  if (tabKey === 'dimensions_json') {
    wrap.innerHTML = '<span class="spinner"></span> Читаю JSON…';
    try {
      const response = await fetch(viewerImageUrl(runId, lineId, filename));
      const data = await response.json();
      wrap.innerHTML = '<pre class="raw-json">' + esc(JSON.stringify(data, null, 2)) + '</pre>';
    } catch (error) { wrap.innerHTML = '<div class="badge error">Ошибка: ' + esc(error.message) + '</div>'; }
    return;
  }
  if (tabKey === 'dimension_review_json') {
    wrap.innerHTML = '<span class="spinner"></span> Читаю решение провайдера...';
    try {
      const response = await fetch(viewerImageUrl(runId, lineId, filename));
      const data = await response.json();
      wrap.innerHTML = '<pre class="raw-json">' + esc(JSON.stringify(data, null, 2)) + '</pre>';
    } catch (error) { wrap.innerHTML = '<div class="badge error">Ошибка: ' + esc(error.message) + '</div>'; }
    return;
  }
  wrap.innerHTML = '<img class="viewer-frame" src="' + viewerImageUrl(runId, lineId, filename) + '" alt="">';
}

function collectMaps(run) {
  state.filesByLine = {};
  state.tracesByLine = {};
  state.graphsByLine = {};
  for (const line of run.lines) {
    const combined = combinedGraphForLine(line);
    if (combined.nodes.length || combined.edges.length) state.graphsByLine[line.line_id] = combined;
    for (const pr of (line.page_results || [])) {
      const key = pageKey(line.line_id, pr.page_number);
      state.filesByLine[key] = pr.files || {};
      if (pr.provider_trace) state.tracesByLine[key] = pr.provider_trace;
      if (pr.analysis && pr.analysis.graph) state.graphsByLine[key] = pr.analysis.graph;
    }
  }
}

function edgeRowHtml(segment) {
  return '<tr><td>' + esc(segment.from) + ' → ' + esc(segment.to) + '</td><td><b>' + esc(segment.value ?? '—') + '</b></td>'
    + '<td class="mono">' + esc(String(segment.coords || [])) + '</td></tr>';
}

function coordinatesTable(coordinates) {
  if (!coordinates || !coordinates.length) return '';
  const rows = coordinates.map(function (coord) {
    return '<tr><td class="mono">' + esc(coord.label) + '</td><td><b>' + esc(coord.value) + '</b></td></tr>';
  }).join('');
  return '<div class="coord-block"><b>Координаты X/Y/Z на листе (' + coordinates.length + ')</b>'
    + '<table class="data-table coord-table"><tbody>' + rows + '</tbody></table></div>';
}

function pointsTable(points) {
  if (!points || !points.length) return '';
  const rows = points.map(function (point) {
    const xyz = [point.x, point.y, point.z].map(function (v) { return v == null ? '—' : esc(String(v)); }).join(' / ');
    const sources = Array.isArray(point.source_coordinate_labels)
      ? point.source_coordinate_labels.join(', ')
      : (point.source_coordinate_labels || '—');
    return '<tr><td class="mono">' + esc(point.vertex_id || point.id) + '</td><td>' + xyz + '</td><td>' + esc(sources || '—') + '</td></tr>';
  }).join('');
  return '<div class="coord-block"><b>Координаты узлов (от нейросети, ' + points.length + ')</b>'
    + '<table class="data-table coord-table"><thead><tr><th>Вершина</th><th>X / Y / Z</th><th>Источник в чертеже</th></tr></thead><tbody>' + rows + '</tbody></table></div>';
}

function pageDetailHtml(lineId, pr) {
  const analysis = pr.analysis;
  const downloads = artifactLinks(lineId, pr, state.runId);
  const viewer = viewerCard(lineId, pr, state.runId);
  const coords = coordinatesTable(pr.coordinates);
  const points = analysis ? pointsTable(analysis.points) : '';
  const coordinateWarnings = analysis && Array.isArray(analysis.coordinate_warnings) && analysis.coordinate_warnings.length
    ? '<div class="notes"><b>Проверка координат</b><ul>' + analysis.coordinate_warnings.map(function (item) { return '<li>' + esc(item) + '</li>'; }).join('') + '</ul></div>'
    : '';
  if (!analysis) {
    return '<div class="muted">Лист ' + pr.page_number + ': '
      + (pr.status === 'error' ? '<span class="badge error">' + esc(pr.error || 'Ошибка') + '</span>'
        : 'анализ не запускался (этап подготовки)') + '</div>'
      + coords + downloads + viewer;
  }
  if (analysis.dimension_mapping) {
    const mapping = analysis.dimension_mapping;
    const dimensions = mapping.dimensions || [];
    const review = analysis.dimension_review;
    const localCandidates = dimensions.map(function (item, index) {
      return {
        id: item.id || ('D' + (index + 1)),
        text: item.text || '—',
        kind: item.kind || item.status || 'dimension',
        status: item.status || 'mapped',
        hints: Array.isArray(item.hints) ? item.hints : [],
        edge_id: item.edge_id || '—',
        gap_px: item.gap_px ?? '—',
        leader_attached: !!item.leader_attached,
        conflict_with: item.conflict_with || '',
        valid: item.valid !== false,
      };
    });
    const localHandwheels = localCandidates.filter(function (item) {
      return item.status === 'handwheel' || (item.hints || []).includes('handwheel');
    });
    const lengths = review && review.lengths ? review.lengths : {
      clean_length_mm: 0,
      dirty_length_mm: 0,
      ambiguous_length_mm: 0,
      included_candidate_ids: [],
      excluded_candidate_ids: [],
      ambiguous_candidate_ids: [],
      duplicate_included_candidate_ids: [],
      deterministically_invalid_candidate_ids: [],
      main: { clean_length_mm: 0, dirty_length_mm: 0, ambiguous_length_mm: 0 },
      branch: { clean_length_mm: 0, dirty_length_mm: 0, ambiguous_length_mm: 0 }
    };
    const routeKpi = function (label, route) {
      if (!route) return '';
      return '<div class="route-kpi ' + (label === 'Ответвления' ? 'branch' : 'main') + '"><span>' + label + '</span><strong>чистая ' + esc(route.clean_length_mm) + ' мм</strong><small>грязная ' + esc(route.dirty_length_mm) + ' мм · сомнения ' + esc(route.ambiguous_length_mm) + ' мм</small></div>';
    };
    const branchInfo = review && (review.branch_routes || []).length
      ? '<p><b>Ответвления:</b> ' + review.branch_routes.map(function (route) { return esc((route.junction_vertex_id || '?') + ' → ' + (route.endpoint_vertex_id || '?') + ' [' + (route.edge_ids || []).join(', ') + ']'); }).join('; ') + '</p>'
      : '';
    const crossSheetInfo = review && (review.cross_sheet_connections || []).length
      ? '<p><b>Переходы на другие листы:</b> ' + review.cross_sheet_connections.map(function (item) { return esc((item.vertex_id || '?') + ': ' + (item.label || item.target_sheet || '')); }).join('; ') + '</p>'
      : '';
    const invalidCandidates = (lengths.deterministically_invalid_candidate_ids || []).length
      ? '<div class="notes"><b>Предварительно невалидно</b><p><span class="badge err">не считается в длине</span> ' + esc((lengths.deterministically_invalid_candidate_ids || []).join(', ') || '—') + '</p></div>'
      : '';
    const evalStats = review && review.eval
      ? '<div class="notes"><b>Eval по эталону</b>' + Object.entries(review.eval).map(function ([groupId, stats]) {
          return '<p><span class="badge run">' + esc(groupId) + '</span> ' + esc(stats.accuracy) + '% (' + esc(stats.correct) + '/' + esc(stats.total_candidates) + ') — ' + esc(stats.incorrect) + ' ошибок</p>';
        }).join('') + '</div>'
      : '';
    const reviewBlock = review
      ? '<div class="review-summary"><div class="kpi"><div><span class="kpi-label">Чистая длина</span><strong>' + esc(lengths.clean_length_mm) + ' мм</strong></div><div><span class="kpi-label">Грязная длина</span><strong>' + esc(lengths.dirty_length_mm) + ' мм</strong></div></div><div class="route-kpis">' + routeKpi('Основная линия', lengths.main) + routeKpi('Ответвления', lengths.branch) + '</div><div class="notes"><b>Проверка провайдером</b><p>Сомнения: <b>' + esc(lengths.ambiguous_length_mm) + ' мм</b> · дубли включённых: ' + esc((lengths.duplicate_included_candidate_ids || []).join(', ') || '—') + '</p>' + branchInfo + crossSheetInfo + '<p>include: ' + esc((lengths.included_candidate_ids || []).join(', ') || '—') + '<br>exclude: ' + esc((lengths.excluded_candidate_ids || []).join(', ') || '—') + '<br>ambiguous: ' + esc((lengths.ambiguous_candidate_ids || []).join(', ') || '—') + '</p></div>' + invalidCandidates + evalStats + '</div>'
      : '';
    const localCandidatesBlock = localCandidates.length
      ? '<div class="notes"><b>Локальные кандидаты</b><table class="data-table"><thead><tr><th>ID</th><th>Текст</th><th>Ключ</th><th>Статус</th><th>Ребро</th></tr></thead><tbody>'
        + localCandidates.map(function (item) {
          return '<tr class="candidate-' + esc(item.status) + '"><td>' + esc(item.id) + '</td><td>' + esc(item.text) + '</td><td>' + esc((item.hints || []).join(', ') || item.kind) + '</td><td>' + esc(item.status) + '</td><td>' + esc(item.edge_id || '—') + '</td></tr>';
        }).join('') + '</tbody></table></div>'
      : '';
    const handwheelBlock = localHandwheels.length
      ? '<div class="notes"><b>Штурвалы / рукоятки / маховики</b><table class="data-table"><thead><tr><th>ID</th><th>Текст</th><th>Статус</th><th>Ребро</th></tr></thead><tbody>'
        + localHandwheels.map(function (item) {
          return '<tr class="candidate-handwheel"><td>' + esc(item.id) + '</td><td>' + esc(item.text) + '</td><td>' + esc(item.status) + '</td><td>' + esc(item.edge_id || '—') + '</td></tr>';
        }).join('') + '</tbody></table></div>'
      : '';

    return '<h4>Лист ' + pr.page_number + ' — привязка размеров</h4>'
      + '<p class="muted">Рёбер графа: ' + (mapping.edges || []).length + ' · размеров: ' + dimensions.length + '</p>'
      + reviewBlock
      + localCandidatesBlock
      + handwheelBlock
      + '<table class="data-table"><thead><tr><th>Размер</th><th>Отрезок</th><th>Зазор, px</th><th>Статус</th><th>Пояснение модели</th></tr></thead><tbody>'
      + (dimensions.map(function (item) {
        const reviewDecision = review && review.answer && (review.answer.candidate_decisions || []).find(function (row) { return row.candidate_id === item.id; });
        const status = reviewDecision ? reviewDecision.decision : item.status;
        return '<tr class="candidate-' + esc(status) + '"><td>' + esc(item.text) + '</td><td>' + esc(item.edge_id || '—') + '</td><td>' + esc(item.gap_px ?? '—') + '</td><td>' + esc(status) + (item.leader_attached ? ' · стрелка' : '') + (item.conflict_with ? ' → ' + esc(item.conflict_with) : '') + '</td><td class="candidate-reason">' + esc(reviewDecision ? reviewDecision.reason : '') + '</td></tr>';
      }).join('') || '<tr><td colspan="5" class="muted">Размеров нет</td></tr>')
      + '</tbody></table>' + downloads + viewer;
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
    + points
    + coordinateWarnings
    + coords
    + downloads
    + viewer;
}

function renderResults(run) {
  state.runId = run.run_id;
  state.currentRun = run;
  collectMaps(run);
  $('#result-section').hidden = false;
  $('#result-title').textContent = run.run_id;
  $('#export-json').href = '/api/runs/' + encodeURIComponent(run.run_id) + '/export/json';
  $('#export-excel').href = '/api/runs/' + encodeURIComponent(run.run_id) + '/export/excel';
  const body = $('#result-body');
  body.innerHTML = run.lines.map(function (line) {
    const pages = line.page_results || [];
    let totalMain = 0;
    let edgeCount = 0;
    let totalBranches = 0;
    let totalSkipped = 0;
    let reviewCleanMain = 0;
    let reviewDirtyMain = 0;
    let reviewCleanBranch = 0;
    let reviewDirtyBranch = 0;
    let hasDimensionReview = false;
    pages.forEach(function (pr) {
      const a = pr.analysis;
      if (!a) return;
      if (a.dimension_review && a.dimension_review.lengths) {
        hasDimensionReview = true;
        const lengths = a.dimension_review.lengths;
        reviewCleanMain += Number(lengths.main?.clean_length_mm || 0);
        reviewDirtyMain += Number(lengths.main?.dirty_length_mm || 0);
        reviewCleanBranch += Number(lengths.branch?.clean_length_mm || 0);
        reviewDirtyBranch += Number(lengths.branch?.dirty_length_mm || 0);
      }
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
      + (state.graphsByLine[line.line_id]
        ? '<section class="result-3d-block"><div class="panel-title"><span>ТОПОЛОГИЯ / ' + esc(line.line_id) + '</span><h2>3D-граф трубы</h2></div><div class="line-graph-3d" data-graph-3d-line="' + esc(line.line_id) + '"><div class="graph-3d-loading"><span class="spinner"></span> Загружаю 3D-сцену...</div></div></section>'
        : '')
      + '<div class="kpi">'
      + (hasDimensionReview
        ? '<div><span class="kpi-label">Основная линия · чистая / грязная</span><strong>' + reviewCleanMain + ' / ' + reviewDirtyMain + ' мм</strong></div>'
          + '<div><span class="kpi-label">Ответвления · чистая / грязная</span><strong>' + reviewCleanBranch + ' / ' + reviewDirtyBranch + ' мм</strong></div>'
          + '<div><span class="kpi-label">Рёбер</span><strong>' + edgeCount + '</strong></div>'
          + '<div><span class="kpi-label">Листов</span><strong>' + pages.length + '</strong></div>'
        : '<div><span class="kpi-label">Сумма (все листы)</span><strong>' + totalMain + ' мм</strong></div>'
          + '<div><span class="kpi-label">Рёбер</span><strong>' + edgeCount + '</strong></div>'
          + '<div><span class="kpi-label">Ответвлений</span><strong>' + totalBranches + '</strong></div>'
          + '<div><span class="kpi-label">Непривязанных</span><strong>' + totalSkipped + '</strong></div>')
      + '</div>'
      + '<div class="row page-selector-row">' + pageButtons + '</div>'
      + '<div class="page-content" data-line="' + esc(line.line_id) + '">'
      + (firstPage ? pageDetailHtml(line.line_id, firstPage) : '<p class="muted">Страниц нет</p>')
      + '</div>'
      + '</div>';
  }).join('<hr>');
  $('#result-body').querySelectorAll('[data-graph-3d-line]').forEach(function (container) {
    const lineId = container.getAttribute('data-graph-3d-line') || '';
    render3DGraph(container, state.graphsByLine[lineId]);
  });
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

async function showEval() {
  try {
    const data = await api('/api/eval');
    $('#eval-section').hidden = false;
    $('#history-section').hidden = true;
    $('#runs-section').hidden = true;
    $('#result-section').hidden = true;
    $('#prompt-section').hidden = true;
    const body = $('#eval-body');
    if (!data.prompt_versions || !data.prompt_versions.length) {
      body.innerHTML = '<p class="muted">Нет данных eval для промптов.</p>';
      $('#eval-section').scrollIntoView({ behavior: 'smooth', block: 'start' });
      return;
    }
    const tableRows = (data.prompt_versions || []).map(function (item) {
      const stats = item.stats || {};
      const groupsText = (stats.groups || []).length
        ? (stats.groups || []).map(function (group) {
            return esc(group.group_id) + ': ' + esc(group.accuracy) + '% (' + esc(group.correct) + '/' + esc(group.total_candidates) + ')';
          }).join('<br>')
        : '—';
      return '<tr>'
        + '<td>' + esc(item.title) + '</td>'
        + '<td>' + esc(item.prompt_name) + '</td>'
        + '<td>' + esc(item.version) + '</td>'
        + '<td>' + esc(stats.accuracy) + '%</td>'
        + '<td>' + esc(stats.correct) + '/' + esc(stats.total_candidates) + '</td>'
        + '<td>' + groupsText + '</td>'
        + '</tr>';
    }).join('');

    body.innerHTML = '<table class="data-table">'
      + '<thead><tr><th>Промпт</th><th>Имя</th><th>Версия</th><th>Точность</th><th>Корректно/всего</th><th>Группы</th></tr></thead>'
      + '<tbody>' + tableRows + '</tbody>'
      + '</table>';
    $('#eval-section').scrollIntoView({ behavior: 'smooth', block: 'start' });
  } catch (error) {
    $('#eval-body').innerHTML = '<div class="badge error">' + esc(error.message) + '</div>';
  }
}

async function showHistory() {
  try {
    const runs = await api('/api/runs');
    $('#history-section').hidden = false;
    $('#runs-section').hidden = true;
    $('#result-section').hidden = true;
    $('#prompt-section').hidden = true;
    $('#eval-section').hidden = true;
    const body = $('#history-body');
    if (!runs.length) {
      body.innerHTML = '<p class="muted">История пуста — запустите первый прогон.</p>';
      $('#history-section').scrollIntoView({ behavior: 'smooth', block: 'start' });
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
        + (run.error_count ? '<div class="badge error">Ошибок: ' + run.error_count + '<br>' + run.errors.map(esc).join('<br>') + '</div>' : '')
        + '<button class="open-run" type="button" data-open-run="' + esc(run.run_id) + '">Открыть →</button>'
        + '</div>';
    }).join('');
    $('#history-body').querySelectorAll('[data-open-run]').forEach(function (button) {
      button.addEventListener('click', function () { openHistoryRun(button.dataset.openRun); });
    });
    $('#history-section').scrollIntoView({ behavior: 'smooth', block: 'start' });
  } catch (error) {
    $('#history-body').innerHTML = '<div class="badge error">' + esc(error.message) + '</div>';
  }
}

async function openHistoryRun(runId) {
  try {
    const run = await api('/api/runs/' + runId);
    state.runId = runId;
    $('#history-section').hidden = true;
    if (run.status === 'running') {
      $('#runs-section').hidden = false;
      $('#result-section').hidden = true;
      renderRun(run);
      pollRun();
    } else {
      renderResults(run);
    }
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
  document.addEventListener('click', function (event) {
    if (event.target && event.target.classList && event.target.classList.contains('viewer-frame')) {
      const lightbox = $('#image-lightbox');
      const img = $('#image-lightbox-img');
      img.src = event.target.src;
      lightbox.hidden = false;
    } else if (event.target && event.target.id === 'image-lightbox') {
      $('#image-lightbox').hidden = true;
    }
  });
  $('#pdf-file').addEventListener('change', function () {
    const file = $('#pdf-file').files[0];
    if (file) $('#document-summary').textContent = 'Файл выбран: ' + file.name;
  });
  $('#load-pdf').addEventListener('click', loadPdf);
  $('#start-analysis').addEventListener('click', startAnalysis);
  $('#history-button').addEventListener('click', showHistory);
  $('#prompts-button').addEventListener('click', showPrompts);
  $('#eval-button').addEventListener('click', showEval);
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
