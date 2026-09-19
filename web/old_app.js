const state = {
  tabs: [{ id: 'welcome', type: 'welcome', title: 'Welcome', closable: false }],
  active: 'welcome',
  config: null,
  document: null,
  runs: {},
  selectedStages: {},
  candidatePages: {},
  candidateFilterSelection: {},
  drawingMode: {},
  selectedLines: {},
  groupSearch: '',
};

const $ = (selector, root = document) => root.querySelector(selector);
const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];

const escapeHtml = (value) =>
  String(value ?? '').replace(/[&<>"']/g, (char) => ({
    '&': '&amp;',
    '<': '&lt;',
    '>': '&gt;',
    '"': '&quot;',
    "'": '&#039;',
  })[char]);

async function api(url, options = {}) {
  const response = await fetch(url, options);
  if (!response.ok) {
    const detail = await response.json().catch(() => ({}));
    throw new Error(detail.detail || `HTTP ${response.status}`);
  }
  return response.headers.get('content-type')?.includes('application/json') ? response.json() : response;
}

const postForm = (url, form) => api(url, { method: 'POST', body: form });

function setMessage(message, isError = false) {
  const node = $('#api-status');
  node.textContent = message;
  node.previousElementSibling.classList.toggle('ok', !isError);
}

function renderTabs() {
  $('#workspace-tabs').innerHTML = state.tabs.map((tab) => `
    <div class="workspace-tab ${tab.id === state.active ? 'active' : ''}" data-tab="${escapeHtml(tab.id)}">
      <span>${escapeHtml(tab.title)}</span>
      ${tab.closable ? `<button class="close-tab" data-close="${escapeHtml(tab.id)}" aria-label="Закрыть">×</button>` : ''}
    </div>
  `).join('');
}

function addTab(tab) {
  if (!state.tabs.some((item) => item.id === tab.id)) state.tabs.push(tab);
  state.active = tab.id;
  renderTabs();
  renderActive();
}

function closeTab(id) {
  state.tabs = state.tabs.filter((tab) => tab.id !== id);
  if (state.active === id) state.active = 'welcome';
  renderTabs();
  renderActive();
}

function renderActive() {
  const tab = state.tabs.find((item) => item.id === state.active) || state.tabs[0];
  if (tab.type === 'welcome') renderWelcome();
  else if (tab.type === 'history') renderHistory();
  else renderAnalysis(tab.runId);
}

$('#workspace-tabs').addEventListener('click', (event) => {
  const close = event.target.closest('[data-close]');
  if (close) {
    event.stopPropagation();
    closeTab(close.dataset.close);
    return;
  }
  const tab = event.target.closest('[data-tab]');
  if (tab) {
    state.active = tab.dataset.tab;
    renderTabs();
    renderActive();
  }
});

$('#history-button').addEventListener('click', () => {
  addTab({ id: 'history', type: 'history', title: 'История', closable: true });
});

async function loadConfig() {
  try {
    state.config = await api('/api/config');
    setMessage('API подключен');
    $('#key-status').textContent = state.config.deepseek_key_present ? 'DEEPSEEK_API_KEY найден' : 'Демо-режим: ключ не найден';
    $('#key-status').style.color = state.config.deepseek_key_present ? 'var(--green)' : 'var(--orange)';
    $('#config-note').textContent = state.config.deepseek_key_present
      ? `Модель ${state.config.deepseek_model}`
      : 'Запуски выполняются через локальный stub-анализатор';
  } catch (error) {
    setMessage(error.message, true);
    $('#key-status').textContent = 'API недоступен';
    $('#config-note').textContent = 'Проверьте запуск server.py';
  }
}

function renderWelcome() {
  const template = $('#welcome-template');
  $('#app').replaceChildren(template.content.cloneNode(true));

  $('#open-history').addEventListener('click', () => {
    addTab({ id: 'history', type: 'history', title: 'История', closable: true });
  });
  $('#load-pdf').addEventListener('click', loadPdf);
  $('#start-analysis').addEventListener('click', startAnalysis);
  $('#groups-search').addEventListener('input', (event) => {
    state.groupSearch = event.target.value;
    renderGroupsList();
  });
  $('#pdf-file').disabled = true;
  $$('input[name="source-mode"]').forEach((input) => {
    input.addEventListener('change', () => {
      $('#pdf-file').disabled = input.value !== 'upload' || !input.checked;
    });
  });

  loadConfig();
  if (state.document) showDocument(state.document);
}

function showDocument(documentData) {
  state.document = documentData;
  $('#document-summary').classList.remove('empty');
  $('#document-summary').innerHTML = `
    <strong>${escapeHtml(documentData.source_name)}</strong>
    · ${documentData.pages_count} страниц
    · ${documentData.groups_count} групп
    · подготовка ${documentData.prepared_seconds} с
  `;
  renderGroupsList();
  $('#start-analysis').disabled = !documentData.groups.length;
}

function visibleGroups() {
  if (!state.document) return [];
  const query = state.groupSearch.trim().toLowerCase();
  return state.document.groups
    .filter((group) => !group.line_id.startsWith('UNKNOWN_'))
    .filter((group) => {
      if (!query) return true;
      const haystack = `${group.line_id} ${group.page_numbers.join(' ')}`.toLowerCase();
      return haystack.includes(query);
    });
}

function renderGroupsList() {
  const list = $('#groups-list');
  const count = $('#groups-count');
  if (!state.document) {
    list.className = 'groups-list empty-state';
    list.textContent = 'Сначала загрузите PDF';
    count.textContent = '0 групп';
    return;
  }

  const groups = visibleGroups();
  list.className = 'groups-list';
  count.textContent = `${groups.length} из ${state.document.groups_count}`;
  list.innerHTML = groups.map((group) => `
    <label class="group-item">
      <input type="checkbox" value="${escapeHtml(group.line_id)}">
      <span class="group-item-main">${escapeHtml(group.line_id)}</span>
      <span class="group-item-meta">${group.pages_count} стр. · ${group.page_numbers.join(', ')}</span>
    </label>
  `).join('') || '<p class="hint">По этому поиску группы не найдены.</p>';
}

async function loadPdf() {
  const mode = $('input[name="source-mode"]:checked').value;
  const form = new FormData();
  form.append('mode', mode);
  form.append('use_cache', $('#use-cache').checked);

  if (mode === 'upload') {
    const file = $('#pdf-file').files[0];
    if (!file) {
      alert('Выберите PDF-файл');
      return;
    }
    form.append('file', file);
  }

  const button = $('#load-pdf');
  button.disabled = true;
  button.textContent = 'Читаю PDF...';
  try {
    state.groupSearch = '';
    showDocument(await postForm('/api/pdf/load', form));
    $('#groups-search').value = '';
  } catch (error) {
    alert(error.message);
  } finally {
    button.disabled = false;
    button.textContent = 'Загрузить PDF';
  }
}

async function startAnalysis() {
  const lineIds = $$('#groups-list input:checked').map((input) => input.value);
  if (!lineIds.length) {
    alert('Выберите хотя бы одну линию');
    return;
  }
  try {
    const result = await api('/api/runs', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        document_id: state.document.document_id,
        line_ids: lineIds,
        reanalyze: $('#reanalyze').checked,
        stop_stage: $('#stop-stage')?.value || '',
      }),
    });
    state.runs[result.run_id] = { run_id: result.run_id, title: result.title };
    addTab({
      id: `analysis:${result.run_id}`,
      type: 'analysis',
      title: result.title,
      runId: result.run_id,
      closable: true,
    });
  } catch (error) {
    alert(error.message);
  }
}

function renderHistory() {
  $('#app').innerHTML = `
    <section class="history-page">
      <div class="page-header">
        <div>
          <p class="eyebrow">WORKSPACE / ARCHIVE</p>
          <h1>История запусков</h1>
          <p>Все события остаются доступны независимо от активной вкладки.</p>
        </div>
        <button class="secondary-button" id="refresh-history">Обновить</button>
      </div>
      <div class="history-filters">
        <input id="history-search" placeholder="Фильтр по run_id или линии">
        <select id="history-status">
          <option value="">Все статусы</option>
          <option value="complete">Завершено</option>
          <option value="running">В работе</option>
          <option value="error">Ошибка</option>
        </select>
      </div>
      <div id="run-list" class="run-list"><p class="hint">Загрузка истории...</p></div>
    </section>
  `;
  $('#refresh-history').addEventListener('click', renderHistory);
  $('#history-search').addEventListener('input', loadHistory);
  $('#history-status').addEventListener('change', loadHistory);
  loadHistory();
}

async function loadHistory() {
  try {
    const runs = await api('/api/runs');
    const search = ($('#history-search')?.value || '').toLowerCase();
    const status = $('#history-status')?.value || '';
    const filtered = runs.filter((run) =>
      (!status || run.status === status)
      && (!search || `${run.run_id} ${run.selected_line_ids.join(' ')}`.toLowerCase().includes(search))
    );
    $('#run-list').innerHTML = filtered.length ? filtered.map((run) => `
      <article class="run-card">
        <div>
          <h3>${escapeHtml(run.selected_line_ids.join(' + '))}</h3>
          <small class="mono">${escapeHtml(run.run_id)} · ${new Date(run.created_at).toLocaleString()}</small>
        </div>
        <span class="badge ${run.status === 'error' ? 'error' : ''}">${escapeHtml(run.status)}</span>
        <button class="open-run" data-open-run="${escapeHtml(run.run_id)}">Открыть →</button>
      </article>
    `).join('') : '<p class="hint">Запусков пока нет.</p>';
    $$('[data-open-run]').forEach((button) => {
      button.addEventListener('click', () => {
        const run = runs.find((item) => item.run_id === button.dataset.openRun);
        addTab({
          id: `analysis:${run.run_id}`,
          type: 'analysis',
          title: run.selected_line_ids.length === 1
            ? `Анализ ${run.selected_line_ids[0]}`
            : `Анализ ${run.selected_line_ids[0]} + ${run.selected_line_ids.length - 1}`,
          runId: run.run_id,
          closable: true,
        });
      });
    });
  } catch (error) {
    $('#run-list').innerHTML = `<p class="error-text">${escapeHtml(error.message)}</p>`;
  }
}

const resultFields = [
  ['lines', 'Линии'],
  ['points', 'Точки'],
  ['segments', 'Участки'],
  ['elements', 'Элементы'],
  ['uncertainties', 'Неопределенности'],
  ['candidates', 'Кандидаты PDF'],
  ['candidate_classifications', 'Классификация'],
  ['provider_traces', 'AI prompts / ответы'],
];

function dataForLine(data, lineId) {
  if (!lineId) return data;
  const result = data.result || {};
  const lineItems = (items) => (items || []).filter((item) => item.line_id === lineId || item.id === lineId);
  return {
    ...data,
    result: {
      ...result,
      lines: (result.lines || []).filter((item) => item.id === lineId),
      points: lineItems(result.points),
      segments: lineItems(result.segments),
      elements: lineItems(result.elements),
      uncertainties: lineItems(result.uncertainties),
      vertices: lineItems(result.vertices),
      candidates: lineItems(result.candidates),
      candidate_classifications: lineItems(result.candidate_classifications),
      provider_traces: lineItems(result.provider_traces),
      graph_nodes: lineItems(result.graph_nodes),
      graph_edges: lineItems(result.graph_edges),
      dimension_bindings: result.dimension_bindings || [],
      unresolved_edges: result.unresolved_edges || [],
    },
  };
}

function renderLineTabs(runId, run, data) {
  const lineIds = run.selected_line_ids || [];
  if (lineIds.length <= 1) return '';
  const selected = state.selectedLines[runId] && lineIds.includes(state.selectedLines[runId])
    ? state.selectedLines[runId]
    : lineIds[0];
  state.selectedLines[runId] = selected;
  return `
    <nav class="line-tabs" aria-label="Линии запуска">
      ${lineIds.map((lineId) => {
        const line = (data.result?.lines || []).find((item) => item.id === lineId);
        return `<button class="line-tab ${lineId === selected ? 'active' : ''}" data-line-id="${escapeHtml(lineId)}">
          <strong>${escapeHtml(lineId)}</strong><small>${line ? escapeHtml(line.status) : 'данные'}</small>
        </button>`;
      }).join('')}
    </nav>
  `;
}

function renderTable(items) {
  if (!items?.length) return '<p class="hint">Данных пока нет.</p>';
  const columns = Object.keys(items[0]);
  return `
    <div class="table-wrap">
      <table class="data-table">
        <thead><tr>${columns.map((column) => `<th>${escapeHtml(column)}</th>`).join('')}</tr></thead>
        <tbody>
          ${items.map((item) => `
            <tr>${columns.map((column) => `
              <td>${escapeHtml(typeof item[column] === 'object' ? JSON.stringify(item[column]) : item[column])}</td>
            `).join('')}</tr>
          `).join('')}
        </tbody>
      </table>
    </div>
  `;
}

const candidateTypeLabels = {
  route_segment: 'Трасса',
  coordinate_delta: 'Коорд. перепад',
  coordinate: 'Координата',
  dn: 'DN',
  adjacent_line: 'Соседняя линия',
  support_offset: 'Опора',
  excluded_table: 'Таблица',
  unknown: 'Неясно',
};

function classificationByCandidate(data) {
  return Object.fromEntries((data.result?.candidate_classifications || []).map((item) => [item.candidate_id, item]));
}

function candidateType(candidate, classification) {
  if (candidate.kind === 'coordinate_delta') return 'coordinate_delta';
  return classification?.classification || 'unknown';
}

function segmentsByCandidate(data) {
  const pairs = {};
  (data.result?.segments || []).forEach((segment) => {
    const label = segment.source_ref?.label || '';
    (data.result?.candidates || []).forEach((candidate) => {
      if (label.includes(candidate.id)) {
        if (!pairs[candidate.id]) pairs[candidate.id] = [];
        pairs[candidate.id].push(segment.id);
      }
    });
  });
  return pairs;
}

function candidateRows(data) {
  const classifications = classificationByCandidate(data);
  const usedBy = segmentsByCandidate(data);
  return (data.result?.candidates || []).map((candidate) => {
    const classification = classifications[candidate.id];
    return {
      id: candidate.id,
      page: candidate.page,
      text: candidate.text,
      kind: candidate.kind,
      type: candidateType(candidate, classification),
      zone: candidate.zone,
      accepted: classification?.accepted || false,
      used_by_segments: usedBy[candidate.id] || [],
      gate_reason: classification?.gate_reason || '',
      reason: classification?.reason || '',
    };
  });
}

function renderCandidateDiagnostics(runId, data) {
  const rows = candidateRows(data);
  const lineId = data.result?.lines?.[0]?.id || data.result?.candidates?.[0]?.line_id || '';
  const vertices = data?.result?.vertices || [];
  const hasVertices = Array.isArray(vertices) && vertices.length > 0;
  const vertexPages = [...new Set(vertices.map((vertex) => vertex.page))].sort((a, b) => a - b);
  if (!rows.length) {
    return `
      <section class="candidate-diagnostics muted">
        <div class="panel-title"><span>PDF DIAGNOSTICS</span><h2>Кандидаты на листе</h2></div>
        <p class="hint">Кандидаты появятся после этапа PDF candidates.</p>
        ${vertices.length ? `
          <p><a class="secondary-button" href="/api/runs/${runId}/vertices/pdf">Скачать PDF с вершинами (${vertices.length})</a></p>
          <img id="candidate-overlay-img" src="/api/runs/${runId}/vertices/overlay?page=${vertexPages[0]}&t=${Date.now()}" alt="Вершины на листе ${vertexPages[0]}">
        ` : ''}
      </section>
    `;
  }

  const pages = [...new Set(rows.map((row) => row.page))].sort((a, b) => a - b);
  const availableTypes = [...new Set(rows.map((row) => row.type))];
  const selectedPage = state.candidatePages[runId] && pages.includes(state.candidatePages[runId])
    ? state.candidatePages[runId]
    : pages[0];
  state.candidatePages[runId] = selectedPage;
  if (!state.candidateFilterSelection[runId] || !availableTypes.includes(state.candidateFilterSelection[runId])) {
    state.candidateFilterSelection[runId] = null;
  }
  const selectedFilter = state.candidateFilterSelection[runId];
  const filterClasses = selectedFilter ? [selectedFilter] : availableTypes;

  const visibleRows = rows.filter((row) => row.page === selectedPage && (!selectedFilter || row.type === selectedFilter));
  const pageQuery = selectedPage;
  const overlayUrl = `/api/runs/${runId}/candidates/overlay?page=${pageQuery}&line_id=${encodeURIComponent(lineId)}&classes=${encodeURIComponent(filterClasses.join(','))}&t=${Date.now()}`;
  const verticesUrl = hasVertices
    ? `/api/runs/${runId}/vertices/overlay?page=${pageQuery}&t=${Date.now()}`
    : overlayUrl;
  const cleanUrl = `/api/runs/${runId}/clean-drawing?page=${pageQuery}&t=${Date.now()}`;
  const skeletonUrl = `/api/runs/${runId}/skeleton-drawing?page=${pageQuery}&t=${Date.now()}`;
  if (!state.drawingMode[runId]) state.drawingMode[runId] = 'overlay';
  const drawingMode = state.drawingMode[runId];
  const imageSrc = drawingMode === 'clean' ? cleanUrl : drawingMode === 'skeleton' ? skeletonUrl : drawingMode === 'vertices' ? verticesUrl : overlayUrl;

  return `
    <section class="analysis-image">
      <div class="panel-title important">
        <span>ЛИСТ ${selectedPage}</span>
        <h2>Разметка PDF</h2>
      </div>
      <div class="candidate-toolbar">
        <label class="clean-toggle">Вид
          <select id="drawing-mode">
            <option value="overlay" ${drawingMode === 'overlay' ? 'selected' : ''}>Разметка кандидатов</option>
            ${hasVertices ? `<option value="vertices" ${drawingMode === 'vertices' ? 'selected' : ''}>Вершины на PDF</option>` : ''}
            <option value="clean" ${drawingMode === 'clean' ? 'selected' : ''}>Чистый лист (без таблиц/штампа)</option>
            <option value="skeleton" ${drawingMode === 'skeleton' ? 'selected' : ''}>Только труба, стрелки и длины</option>
          </select>
        </label>
      </div>
      <img id="candidate-overlay-img" src="${imageSrc}" alt="Разметка применительно к листу ${selectedPage}">
      <p class="hint">Нажмите на изображение, чтобы открыть на весь экран. Выше — выбрать лист и тип разметки; картинка обновляется.</p>
    </section>
    <section class="candidate-diagnostics">
      <div class="panel-title important">
        <span>PDF DIAGNOSTICS</span>
        <h2>Кандидаты на листе</h2>
      </div>
      <div class="candidate-toolbar">
        <label>Лист
          <select id="candidate-page">
            ${pages.map((page) => `<option value="${page}" ${page === selectedPage ? 'selected' : ''}>${page}</option>`).join('')}
          </select>
        </label>
        <div class="candidate-filters">
          ${availableTypes.map((type) => `
            <button class="candidate-chip ${selectedFilter === type ? 'active' : ''}" data-candidate-filter="${type}">
              ${escapeHtml(candidateTypeLabels[type] || type)}
            </button>
          `).join('')}
        </div>
      </div>
      <div class="candidate-side">
        <strong>${visibleRows.length} кандидатов на листе ${selectedFilter ? `· фильтр: ${candidateTypeLabels[selectedFilter] || selectedFilter}` : ''}</strong>
        <p class="hint">Нажмите один тип, чтобы показать только его. Повторное нажатие снимает фильтр (показать все). Изображение выше подстраивается под фильтр.</p>
        <div class="candidate-row-list">
          ${visibleRows.map((row) => `
            <article class="candidate-row">
              <div><span class="mono">${escapeHtml(row.id)}</span><strong>${escapeHtml(row.text)}</strong></div>
              <span class="candidate-type type-${escapeHtml(row.type)}">${escapeHtml(candidateTypeLabels[row.type] || row.type)}</span>
              <small>${row.used_by_segments.length ? `Использован: ${escapeHtml(row.used_by_segments.join(', '))}` : escapeHtml(row.gate_reason || row.reason || 'Не использован в итоговом сегменте')}</small>
            </article>
          `).join('') || '<p class="hint">По выбранным фильтрам кандидатов нет.</p>'}
        </div>
      </div>
    </section>
  `;
}

function renderGraphVisual(data) {
  const nodes = data.result?.graph_nodes || [];
  const edges = data.result?.graph_edges || [];
  if (!nodes.length || !edges.length) {
    const rr = (data.result?.lines || []).find((item) => item.route_reconstruction)?.route_reconstruction;
    if (!rr || !rr.graph) return '';
    return `
      <section class="graph-visual muted">
        <div class="panel-title"><span>ВИЗУАЛЬНЫЙ ГРАФ</span><h2>Граф трубопровода</h2></div>
        <p class="hint">Граф не построен (method: ${escapeHtml(rr.method || '')}). Векторный путь оси: ${escapeHtml(rr.graph.status || '')}, штрихов оси ${escapeHtml(rr.graph.axis_strokes ?? 0)}, размерных ${escapeHtml(rr.graph.dimension_strokes ?? 0)}.</p>
      </section>
    `;
  }

  const positions = {};
  let fallbackX = 80;
  let fallbackY = 80;
  nodes.forEach((node) => {
    if (node.bbox && node.bbox.length === 4) {
      positions[node.id] = { x: (node.bbox[0] + node.bbox[2]) / 2, y: (node.bbox[1] + node.bbox[3]) / 2 };
    }
  });
  for (let pass = 0; pass < 3; pass += 1) {
    nodes.forEach((node) => {
      if (positions[node.id]) return;
      const related = edges.filter((edge) => edge.from_node_id === node.id || edge.to_node_id === node.id);
      const others = related
        .flatMap((edge) => [edge.from_node_id, edge.to_node_id])
        .filter((id) => positions[id]);
      if (others.length) {
        const xs = others.map((id) => positions[id].x);
        const ys = others.map((id) => positions[id].y);
        positions[node.id] = {
          x: xs.reduce((a, b) => a + b, 0) / xs.length + 45,
          y: ys.reduce((a, b) => a + b, 0) / ys.length + 45,
        };
      }
    });
  }
  nodes.forEach((node) => {
    if (!positions[node.id]) {
      positions[node.id] = { x: fallbackX, y: fallbackY };
      fallbackY += 80;
    }
  });

  const xs = Object.values(positions).map((pt) => pt.x);
  const ys = Object.values(positions).map((pt) => pt.y);
  const minX = Math.min(10, ...xs) - 60;
  const minY = Math.min(10, ...ys) - 50;
  const maxX = Math.max(400, ...xs) + 60;
  const maxY = Math.max(400, ...ys) + 60;
  const edgeColors = { main: '#0d9488', branch: '#ea580c', unknown: '#6b7280' };

  const edgeLines = edges.map((edge) => {
    const from = positions[edge.from_node_id];
    const to = positions[edge.to_node_id];
    if (!from || !to) return '';
    const color = edgeColors[edge.path_type] || edgeColors.unknown;
    const mx = (from.x + to.x) / 2;
    const my = (from.y + to.y) / 2;
    const label = `${edge.id} ${edge.length_mm != null ? `${edge.length_mm} мм` : 'нет длины'}`;
    return `
      <line x1="${from.x}" y1="${from.y}" x2="${to.x}" y2="${to.y}" stroke="${color}" stroke-width="5"></line>
      <text x="${mx}" y="${my - 6}" fill="${color}" font-size="20" text-anchor="middle" class="graph-edge-label">${escapeHtml(label)}</text>
    `;
  }).join('');

  const nodeMarks = nodes.map((node) => {
    const pt = positions[node.id];
    const coord = [node.x, node.y, node.z].filter((v) => v != null).join(', ');
    return `
      <g>
        <circle cx="${pt.x}" cy="${pt.y}" r="13" fill="${node.role === 'начало' ? '#2563eb' : node.role === 'конец' ? '#dc2626' : '#1e293b'}"></circle>
        <text x="${pt.x}" y="${pt.y - 17}" fill="#111827" font-size="19" text-anchor="middle" font-weight="700">${escapeHtml(node.id)}</text>
        <text x="${pt.x}" y="${pt.y + 34}" fill="#475569" font-size="16" text-anchor="middle">${escapeHtml(node.role || '')}${node.x != null ? ` · ${node.x}` : ''}</text>
      </g>
    `;
  }).join('');

  return `
    <section class="graph-visual">
      <div class="panel-title important">
        <span>ВИЗУАЛЬНЫЙ ГРАФ</span>
        <h2>Граф трубопровода (${edges.length} рёбер, ${nodes.length} узлов)</h2>
      </div>
      <div class="graph-canvas">
        <svg viewBox="${minX} ${minY} ${maxX - minX} ${maxY - minY}" role="img">
          ${edgeLines}
          ${nodeMarks}
        </svg>
      </div>
      <p class="hint">
        <span style="color:#0d9488">— main</span> ·
        <span style="color:#ea580c">— branch</span> ·
        <span style="color:#6b7280">— unknown / нет длины.</span>
        Положение узлов — по bbox на листе; подписи рёбер — принятая длина.
      </p>
    </section>
  `;
}

function renderRouteDiagnostics(data) {
  const line = (data.result?.lines || []).find((item) => item.route_reconstruction);
  if (!line) return '';
  const rr = line.route_reconstruction;
  const reasonsRows = Object.entries(rr.candidate_reasons || {}).map(([id, info]) => ({ id, ...info }));
  const graphEdges = data.result?.graph_edges || [];
  const graphNodes = data.result?.graph_nodes || [];
  const bindings = data.result?.dimension_bindings || [];
  const unresolved = data.result?.unresolved_edges || [];

  const kpi = (label, value, tone = '') => `
    <div class="route-kpi ${tone}">
      <span>${escapeHtml(label)}</span>
      <strong>${value == null ? '—' : `${escapeHtml(String(value))} мм`}</strong>
    </div>
  `;

  const listBlock = (label, items, empty) => `
    <details class="route-collapse">
      <summary>${escapeHtml(label)} (${items.length})</summary>
      <div class="route-list">
        ${items.length ? items.map((item) => `
          <div class="route-item">
            <span class="mono">${escapeHtml(item.candidate_id != null ? item.candidate_id : (item.stroke != null ? `S${item.stroke}` : (item.edge_id != null ? item.edge_id : '')))}</span>
            <span>${escapeHtml(item.reason || item.basis || '')}</span>
            ${item.value != null ? `<strong>${escapeHtml(String(item.value))} мм</strong>` : ''}
          </div>
        `).join('') : `<p class="hint">${escapeHtml(empty)}</p>`}
      </div>
    </details>
  `;

  const method = rr.method || '';
  const isGraph = String(method).includes('graph_solver');

  return `
    <section class="route-diagnostics">
      <div class="panel-title important">
        <span>${isGraph ? 'GRAPH + RULE LAYER /' : 'RULE LAYER /'} ${escapeHtml(line.id)}</span>
        <h2>Реконструкция маршрута</h2>
      </div>
      <div class="route-kpis">
        ${kpi('Основной маршрут', rr.main_sum_mm, 'main')}
        ${kpi('Ветви', rr.branch_sum_mm, 'branch')}
        ${kpi('Итого принято', rr.total_sum_mm)}
      </div>
      ${isGraph ? `
      <details class="route-collapse" open>
        <summary>Ребра графа (${graphEdges.length})</summary>
        <div class="table-wrap">
          <table class="data-table">
            <thead><tr><th>Ребро</th><th>Узлы</th><th>Тип</th><th>мм</th><th>Источник</th></tr></thead>
            <tbody>
              ${graphEdges.map((edge) => `
                <tr>
                  <td class="mono">${escapeHtml(edge.id)}</td>
                  <td class="mono">${escapeHtml(edge.from_node_id)} → ${escapeHtml(edge.to_node_id)}</td>
                  <td>${escapeHtml(edge.path_type || 'unknown')}</td>
                  <td>${edge.length_mm != null ? escapeHtml(String(edge.length_mm)) : '—'}</td>
                  <td>${escapeHtml(edge.length_source || edge.reason || '')}</td>
                </tr>`).join('') || '<tr><td colspan="5">Ребер нет</td></tr>'}
            </tbody>
          </table>
        </div>
      </details>
      <details class="route-collapse">
        <summary>Узлы графа (${graphNodes.length})</summary>
        <div class="table-wrap">
          <table class="data-table">
            <thead><tr><th>Узел</th><th>Роль</th><th>X</th><th>Y</th><th>Z</th></tr></thead>
            <tbody>
              ${graphNodes.map((node) => `
                <tr>
                  <td class="mono">${escapeHtml(node.id)}</td>
                  <td>${escapeHtml(node.role || '')}</td>
                  <td>${escapeHtml(node.x ?? '—')}</td>
                  <td>${escapeHtml(node.y ?? '—')}</td>
                  <td>${escapeHtml(node.z ?? '—')}</td>
                </tr>`).join('') || '<tr><td colspan="5">Узлов нет</td></tr>'}
            </tbody>
          </table>
        </div>
      </details>
      <details class="route-collapse">
        <summary>Привязки размеров к ребрам (${bindings.length})</summary>
        <div class="table-wrap">
          <table class="data-table">
            <thead><tr><th>Кандидат</th><th>Ребро</th><th>Тип привязки</th></tr></thead>
            <tbody>
              ${bindings.map((binding) => `
                <tr>
                  <td class="mono">${escapeHtml(binding.candidate_id)}</td>
                  <td class="mono">${escapeHtml(binding.edge_id)}</td>
                  <td>${escapeHtml(binding.binding_type || '')}</td>
                </tr>`).join('') || '<tr><td colspan="3">Привязок нет</td></tr>'}
            </tbody>
          </table>
        </div>
      </details>` : ''}
      ${unresolved.length ? `
      <details class="route-collapse" open>
        <summary>Нерешенные ребра (${unresolved.length})</summary>
        <div class="route-list">
          ${unresolved.map((item) => `
            <div class="route-item">
              <span class="mono">${escapeHtml(item.edge_id)}</span>
              <span>${escapeHtml(item.reason || '')}</span>
              ${item.suggested_action ? `<small>${escapeHtml(item.suggested_action)}</small>` : ''}
            </div>
          `).join('')}
        </div>
      </details>` : ''}
      ${listBlock('Координатные перепады пропущены', rr.skipped_coordinate_deltas || [], 'Перепадов нет')}
      ${listBlock('Дубликаты (перепад = размер)', rr.ignored_duplicates || [], 'Дубликатов нет')}
      ${listBlock('Отклоненные rule gate', rr.rejected_candidates || [], 'Отклоненных нет')}
      ${listBlock('Не назначено', rr.unassigned_drawing_dims || [], 'Не назначенных нет')}
      ${listBlock('Возможные недостающие длины', rr.suspected_missing_length || [], 'Подозрений нет')}
      <div class="route-graph">
        <strong>Граф PDF: <span class="mono">${escapeHtml(rr.graph?.status || '')}</span></strong>
        <span>штрихов ${escapeHtml(rr.graph?.strokes_total ?? 0)} · ось ${escapeHtml(rr.graph?.axis_strokes ?? 0)} · размерных ${escapeHtml(rr.graph?.dimension_strokes ?? 0)} · путь ${escapeHtml(rr.graph?.primary_path_strokes ?? 0)} · без размеров ${escapeHtml(rr.graph?.unattached_dimension_strokes ?? 0)}</span>
      </div>
      <details class="route-collapse" open>
        <summary>Назначение кандидатов (${reasonsRows.length})</summary>
        <div class="table-wrap">
          <table class="data-table">
            <thead><tr><th>Кандидат</th><th>мм</th><th>Тип</th><th>Основание</th></tr></thead>
            <tbody>
              ${reasonsRows.map((row) => `
                <tr>
                  <td class="mono">${escapeHtml(row.id)}</td>
                  <td>${escapeHtml(row.value ?? '')}</td>
                  <td>${escapeHtml(row.assigned ?? '—')}</td>
                  <td>${escapeHtml(row.basis || '')}</td>
                </tr>`).join('')}
            </tbody>
          </table>
        </div>
      </details>
      <p class="hint">${isGraph ? 'Сборка по графу: длина ребра принимается только с размерной привязкой или координатами; без подтверждения ребро остается unresolved.' : 'Честная сборка: принятые rule gate размерные длины = main; координатные перепады исключены из длин; ветви — по явным признакам в классификации.'}</p>
      ${(rr.notes || []).length ? `<p class="hint muted">${rr.notes.map((note) => escapeHtml(note)).join('<br>')}</p>` : ''}
    </section>
  `;
}

function stageContent(stage, data, events) {
  if (stage === 'groups') return `<p class="hint">Группы выбраны на Welcome. Событий: ${events.length}</p>`;
  const field = { extract: 'candidates', classify: 'candidate_classifications', validate: 'uncertainties' }[stage];
  if (field) return renderTable(data.result?.[field]);
  return `
    <div class="log-list stage-log">
      ${events.length ? events.map((event) => `
        <div class="log-item">
          <time>${new Date(event.time).toLocaleTimeString()}</time>
          <strong>${escapeHtml(event.event)}</strong>
          <div>${escapeHtml(Object.entries(event)
            .filter(([key]) => !['time', 'event'].includes(key))
            .map(([key, value]) => `${key}: ${typeof value === 'object' ? JSON.stringify(value) : value}`)
            .join(' · '))}</div>
        </div>
      `).join('') : '<p class="hint">Событий для этапа пока нет.</p>'}
    </div>
  `;
}

async function renderAnalysis(runId) {
  $('#app').innerHTML = `
    <section class="analysis-page">
      <div id="analysis-head"><p class="hint">Загрузка запуска...</p></div>
      <div id="analysis-content"></div>
    </section>
  `;
  await refreshAnalysis(runId);
}

function stepState(step, run, activeStage) {
  const steps = state.config?.pipeline_steps || [];
  const terminalStage = run.completed_stage || run.stage;
  const currentIndex = steps.findIndex((item) => item.id === terminalStage);
  const stepIndex = steps.findIndex((item) => item.id === step.id);
  const done = currentIndex >= 0 && stepIndex <= currentIndex;
  return [
    step.id === activeStage ? 'active' : '',
    done ? 'done' : '',
    step.id === run.stage && run.status === 'running' ? 'current' : '',
  ].filter(Boolean).join(' ');
}

function renderProgress(run, activeStage) {
  const percent = Math.max(0, Math.min(100, Math.round((run.progress || 0) * 100)));
  const steps = state.config?.pipeline_steps || [];
  return `
    <section class="analysis-progress-card">
      <div class="progress-copy">
        <span>Прогресс анализа</span>
        <strong>${percent}%</strong>
      </div>
      <div class="progress-track large">
        <div class="progress-fill" style="width:${percent}%"></div>
      </div>
      <div class="pipeline-track" style="--progress:${percent}%">
        ${steps.map((step) => `
          <button class="pipeline-step ${stepState(step, run, activeStage)}" data-stage="${step.id}">
            <span class="step-dot"></span>
            <span class="step-label">${escapeHtml(step.label)}</span>
          </button>
        `).join('')}
      </div>
    </section>
  `;
}

function renderExportBlock(runId, run) {
  if (run.status !== 'complete') {
    return `
      <div class="export-card muted">
        <strong>Экспорт появится после завершения анализа</strong>
        <span>Пока можно смотреть этапы и full logs справа.</span>
      </div>
    `;
  }
  return `
    <div class="export-card ready">
      <div>
        <strong>Анализ завершен</strong>
        <span>Скачайте проверочный JSON или Excel-таблицы.</span>
      </div>
      <div class="export-actions">
        <a href="/api/runs/${runId}/export.json">JSON ↓</a>
        <a href="/api/runs/${runId}/export.xlsx">Excel ↓</a>
      </div>
    </div>
  `;
}

async function refreshAnalysis(runId) {
  try {
    const [run, data, eventData] = await Promise.all([
      api(`/api/runs/${runId}`),
      api(`/api/runs/${runId}/result`),
      api(`/api/runs/${runId}/events`),
    ]);
    state.runs[runId] = run;
    const selectedLineId = run.selected_line_ids?.length > 1
      ? (state.selectedLines[runId] && run.selected_line_ids.includes(state.selectedLines[runId])
        ? state.selectedLines[runId]
        : run.selected_line_ids[0])
      : run.selected_line_ids?.[0];
    state.selectedLines[runId] = selectedLineId;
    const lineData = dataForLine(data, selectedLineId);

    const activeStage = state.selectedStages[runId] || run.stage || 'groups';
    const activeStep = (state.config?.pipeline_steps || []).find((step) => step.id === activeStage);
    const stageEvents = eventData.events.filter((event) => eventStageClient(event.event) === activeStage);

    $('#analysis-head').innerHTML = `
      <div class="analysis-top">
        <div>
          <p class="eyebrow">RUN / ${escapeHtml(run.model_mode)}</p>
          <h1>${escapeHtml(run.selected_line_ids.join(' + '))}</h1>
          <div class="analysis-meta">${escapeHtml(runId)} · ${escapeHtml(run.progress_text)}</div>
        </div>
        <span class="badge ${run.status === 'error' ? 'error' : ''}">${escapeHtml(run.status)}</span>
      </div>
      ${renderLineTabs(runId, run, data)}
      ${renderProgress(run, activeStage)}
    `;

    $('#analysis-content').innerHTML = `
      <div class="analysis-layout">
        <div class="result-panel">
          ${renderExportBlock(runId, run)}
          ${renderGraphVisual(lineData)}
          ${renderCandidateDiagnostics(runId, lineData)}
          ${renderRouteDiagnostics(lineData)}
          <section class="stage-card">
            <div class="panel-title">
              <span>Выбранный этап</span>
              <h2>${escapeHtml(activeStep?.label || activeStage)}</h2>
            </div>
            <div id="stage-data">${stageContent(activeStage, lineData, stageEvents)}</div>
          </section>
          <section class="final-results-card">
            <div class="panel-title important">
              <span>Главный результат</span>
              <h2>Итоговые данные</h2>
            </div>
            <div class="result-tabs">
              ${resultFields.map(([field, label], index) => `
                <button class="result-tab ${index === 0 ? 'active' : ''}" data-result-field="${field}">${label}</button>
              `).join('')}
            </div>
            <div id="result-table">${renderTable(lineData.result?.lines)}</div>
          </section>
        </div>
        <aside class="log-panel">
          <h2>Full logs <span class="mono">${eventData.events.length}</span></h2>
          <div class="live-note">${run.status === 'running' ? 'Обновляется онлайн' : 'Журнал сохранен'}</div>
          <div class="log-list">
            ${eventData.events.slice().reverse().map((event) => `
              <div class="log-item">
                <time>${new Date(event.time).toLocaleTimeString()}</time>
                <strong>${escapeHtml(event.event)}</strong>
                <div>${escapeHtml(event.line_id || event.progress_text || event.error || '')}</div>
              </div>
            `).join('') || '<p class="hint">Логи появятся во время анализа.</p>'}
          </div>
        </aside>
      </div>
      <div id="image-lightbox" class="image-lightbox" title="Нажмите, чтобы закрыть">
        <img id="image-lightbox-img" src="" alt="Разметка на весь экран">
      </div>
    `;

    $$('[data-stage]').forEach((button) => {
      button.addEventListener('click', () => {
        state.selectedStages[runId] = button.dataset.stage;
        refreshAnalysis(runId);
      });
    });
    $$('[data-line-id]').forEach((button) => {
      button.addEventListener('click', () => {
        state.selectedLines[runId] = button.dataset.lineId;
        state.candidatePages[runId] = null;
        state.candidateFilterSelection[runId] = null;
        refreshAnalysis(runId);
      });
    });
    $$('[data-result-field]').forEach((button) => {
      button.addEventListener('click', () => {
        $$('[data-result-field]').forEach((item) => item.classList.remove('active'));
        button.classList.add('active');
        $('#result-table').innerHTML = renderTable(lineData.result?.[button.dataset.resultField]);
      });
    });
    $('#candidate-page')?.addEventListener('change', (event) => {
      state.candidatePages[runId] = Number(event.target.value);
      refreshAnalysis(runId);
    });
    $('#drawing-mode')?.addEventListener('change', (event) => {
      state.drawingMode[runId] = event.currentTarget.value;
      refreshAnalysis(runId);
    });
    $$('[data-candidate-filter]').forEach((button) => {
      button.addEventListener('click', () => {
        const type = button.dataset.candidateFilter;
        const current = state.candidateFilterSelection[runId];
        const next = current === type ? null : type;
        state.candidateFilterSelection[runId] = next;
        refreshAnalysis(runId);
      });
    });
    $('#candidate-overlay-img')?.addEventListener('click', (event) => {
      const lightbox = $('#image-lightbox');
      const lightboxImg = $('#image-lightbox-img');
      if (!lightbox || !lightboxImg) return;
      lightboxImg.src = event.currentTarget.src;
      lightbox.classList.add('open');
    });
    $('#image-lightbox')?.addEventListener('click', () => {
      $('#image-lightbox').classList.remove('open');
    });

    if (run.status === 'pending' || run.status === 'running') {
      setTimeout(() => {
        if (state.active === `analysis:${runId}`) refreshAnalysis(runId);
      }, 1000);
    }
  } catch (error) {
    $('#analysis-head').innerHTML = `<p class="error-text">${escapeHtml(error.message)}</p>`;
  }
}

function eventStageClient(event) {
  if (event === 'run.done' || event.startsWith('cache.')) return 'cache';
  if (event.startsWith('pipeline.validation')) return 'validate';
  if (event.startsWith('pipeline.reconstruct')) return 'reconstruct';
  if (event.startsWith('pipeline.geometry')) return 'geometry';
  if (event.startsWith('pipeline.group')) return 'extract';
  if (event.startsWith('deepseek.graph')) return 'reconstruct';
  if (event.startsWith('deepseek.classify')) return 'classify';
  if (event.startsWith('deepseek.')) return 'analyze';
  return 'groups';
}

renderTabs();
renderWelcome();
