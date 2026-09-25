const state = { document: null, runId: null, selected: new Set(), sourceMode: 'local', provider: 'deepseek', config: null, filesByLine: {}, tracesByLine: {}, graphsByLine: {}, selectedPage: {}, currentRun: null, feedback: {}, manualEdit: false, providerDecisionEdit: new Set(), three: null, viewers3d: new Set(), pollToken: 0 };
const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];
const esc = (value) => String(value ?? '').replace(/[&<>"]/g, (c) => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));

const STAGE_LABELS = {
  prepare: 'Локальная подготовка + разметка',
  dimensions: 'Локальная подготовка + привязка размеров',
  lead_detection: 'Поиск lead-стрелок размеров',
  pipeline_length: 'Расчет длины трубопровода',
  dimension_review: 'Карта размеров + проверка провайдером',
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
    state.config = config;
    state.provider = config.default_provider || 'deepseek';
    $('#provider-select').value = state.provider;
    $('#source-status').textContent = (config.default_pdf ? 'Файл: ' + config.default_pdf : '');
    renderProviderSettings(config);
  } catch (error) {
    $('#api-status').textContent = 'Ошибка API';
    $('#source-status').textContent = 'Бэкенд недоступен: ' + String(error.message || error);
  }
}

function providerName(providerId, config) {
  const provider = (config?.providers || []).find((item) => item.id === providerId);
  return provider ? provider.label : providerId;
}

function renderProviderSettings(config) {
  const codex = config.codex_cli || {};
  updateCodexLoginStatus(codex);
  $('#codex-login-panel').hidden = $('#provider-select').value !== 'codex_cli';
  updateProviderConnectionStatus();
}

function updateCodexLoginStatus(status) {
  if (state.config) state.config.codex_cli = status || {};
  const node = $('#codex-login-status');
  if (!status || !status.available) {
    node.textContent = 'Codex CLI не найден';
    node.className = 'badge error';
    return;
  }
  if (status.logged_in) {
    node.textContent = 'Codex CLI: вход выполнен';
    node.className = 'badge ok';
    return;
  }
  node.textContent = status.status || 'Codex CLI: нужен вход';
  node.className = 'badge run';
  updateProviderConnectionStatus();
}

function providerInfo(providerId) {
  return (state.config?.providers || []).find((item) => item.id === providerId) || { id: providerId, label: providerId };
}

function isProviderConnected(providerId) {
  const info = providerInfo(providerId);
  if (providerId === 'deepseek') return Boolean(info.available || state.config?.deepseek);
  if (providerId === 'codex_cli') return Boolean(info.available && (info.logged_in || state.config?.codex_cli?.logged_in));
  return Boolean(info.available);
}

function updateProviderConnectionStatus() {
  const selected = $('#provider-select').value || state.provider;
  state.provider = selected;
  const info = providerInfo(selected);
  const connected = isProviderConnected(selected);
  const apiDot = $('#api-dot');
  apiDot.classList.toggle('ok', connected);
  $('#api-status').textContent = connected ? ('Провайдер: ' + (info.label || selected)) : 'Провайдер не подключен';
  const status = $('#provider-connection-status');
  if (connected) {
    status.innerHTML = '<span class="badge ok">Подключен</span><span class="muted"> ' + esc(info.label || selected) + (info.model ? ' · ' + esc(info.model) : '') + '</span>';
  } else if (selected === 'deepseek') {
    status.innerHTML = '<span class="badge error">Не подключен</span><p class="muted">Для DeepSeek нужен DEEPSEEK_API_KEY в .env или окружении.</p>';
  } else if (selected === 'codex_cli') {
    status.innerHTML = '<span class="badge error">Не подключен</span><p class="muted">Нужен вход в Codex CLI.</p>';
  } else {
    status.innerHTML = '<span class="badge error">Не подключен</span>';
  }
}

async function refreshCodexLogin() {
  try {
    const status = await api('/api/providers/codex/status', { cache: 'no-store' });
    updateCodexLoginStatus(status);
    const info = providerInfo('codex_cli');
    info.available = status.available;
    info.logged_in = status.logged_in;
    updateProviderConnectionStatus();
  } catch (error) {
    $('#codex-login-status').textContent = 'Ошибка проверки: ' + error.message;
    $('#codex-login-status').className = 'badge error';
    $('#codex-login-command').textContent = 'Для входа выполните в терминале:\n\ncodex login --device-auth\n\nПосле входа нажмите «Проверить вход».';
    $('#codex-login-command').hidden = false;
    updateProviderConnectionStatus();
  }
}

function startCodexLogin() {
  $('#codex-login-command').textContent = 'Для входа выполните в терминале:\n\ncodex login --device-auth\n\nПосле входа нажмите «Проверить вход».';
  $('#codex-login-command').hidden = false;
}

const promptWorkspace = { list: [], activeName: null, active: null, versions: [], token: 0, busy: false };

function promptFeedback(feedback) {
  const data = feedback || {};
  return '<span class="prompt-feedback prompt-feedback-up">↑ ' + Number(data.positive || 0) + '</span>'
    + '<span class="prompt-feedback prompt-feedback-down">↓ ' + Number(data.negative || 0) + '</span>';
}

async function showPrompts() {
  $('#prompt-section').hidden = false;
  $('#runs-section').hidden = true;
  $('#result-section').hidden = true;
  $('#history-section').hidden = true;
  $('#eval-section').hidden = true;
  if (promptWorkspace.busy) return;
  try {
    promptWorkspace.list = await api('/api/prompts', { cache: 'no-store' });
    if (!promptWorkspace.list.some((item) => item.name === promptWorkspace.activeName)) {
      promptWorkspace.activeName = promptWorkspace.list[0]?.name || null;
    }
    renderPromptCatalog();
    if (promptWorkspace.activeName) await loadPromptWorkspace(promptWorkspace.activeName);
    $('#prompt-section').scrollIntoView({ behavior: 'smooth', block: 'start' });
  } catch (error) {
    setPromptStatus('Ошибка: ' + error.message, true);
  }
}

function setPromptStatus(message, error = false) {
  const node = $('#prompt-status');
  node.textContent = message;
  node.classList.toggle('error', error);
}

function setPromptBusy(busy) {
  promptWorkspace.busy = busy;
  $('.prompt-detail').setAttribute('aria-busy', String(busy));
  $('#save-prompt').disabled = busy || !promptWorkspace.active;
  $('#prompt-editor').disabled = busy || !promptWorkspace.active;
  $$('#prompt-tabs button, #prompt-versions button').forEach((button) => { button.disabled = busy; });
}

function clearPromptDetails() {
  promptWorkspace.active = null;
  promptWorkspace.versions = [];
  $('#prompt-active-title').textContent = promptWorkspace.list.find((item) => item.name === promptWorkspace.activeName)?.title || '';
  $('#prompt-active-version').textContent = '…';
  $('#prompt-active-source').textContent = '';
  $('#prompt-active-sha').textContent = '';
  $('#prompt-active-stats').replaceChildren();
  $('#prompt-versions').replaceChildren();
  $('#prompt-editor').value = '';
}

function applyPromptSnapshot(data) {
  if (data.name !== promptWorkspace.activeName) return;
  promptWorkspace.active = data;
  promptWorkspace.versions = Array.isArray(data.versions) ? data.versions : [];
  const item = promptWorkspace.list.find((entry) => entry.name === data.name);
  if (item) Object.assign(item, { active_version: data.version, active_sha256: data.sha256 });
  renderPromptCatalog();
  renderActivePrompt();
  renderPromptVersions();
  setPromptStatus('Активна версия v' + data.version);
}

function renderPromptCatalog() {
  const container = $('#prompt-tabs');
  $('#prompt-catalog-count').textContent = String(promptWorkspace.list.length);
  container.innerHTML = promptWorkspace.list.map((item) => {
    const active = item.name === promptWorkspace.activeName;
    const version = item.active_version ?? 'default';
    return '<button type="button" class="prompt-catalog-item' + (active ? ' is-active' : '') + '"'
      + ' data-prompt-name="' + esc(item.name) + '" aria-selected="' + active + '">'
      + '<span class="prompt-catalog-mark">' + (active ? '✓' : '') + '</span>'
      + '<span class="prompt-catalog-copy"><b>' + esc(item.title || item.name) + '</b>'
      + '<small>' + esc(item.name) + ' · v' + esc(version) + '</small></span>'
      + '</button>';
  }).join('') || '<div class="muted">Промпты не найдены</div>';
  container.querySelectorAll('[data-prompt-name]').forEach((button) => {
    button.addEventListener('click', () => loadPromptWorkspace(button.dataset.promptName));
  });
}

function renderActivePrompt() {
  const data = promptWorkspace.active;
  if (!data) return;
  $('#prompt-active-title').textContent = data.title || data.name;
  $('#prompt-active-version').textContent = data.version ?? 'default';
  $('#prompt-active-source').textContent = data.source || 'default';
  $('#prompt-active-sha').textContent = String(data.sha256 || '').slice(0, 16);
  const activeFeedback = data.active_feedback;
  const promptTotal = data.prompt_feedback || {};
  const legacy = data.unversioned_feedback || {};
  $('#prompt-active-stats').innerHTML = '<div class="prompt-stat-card" data-stat-scope="version"><small>Текущая версия · v' + esc(data.version) + '</small><span>' + promptFeedback(activeFeedback) + '</span></div>'
    + '<div class="prompt-stat-card" data-stat-scope="prompt"><small>Все версии промпта</small><span>' + promptFeedback(promptTotal) + '</span></div>'
    + (Number(legacy.total || 0) > 0
      ? '<div class="prompt-stat-card is-legacy"><small>Без версии</small><span>' + promptFeedback(legacy) + '</span></div>'
      : '');
  $('#prompt-editor').value = data.text || '';
}

function renderPromptVersions() {
  const container = $('#prompt-versions');
  const versions = Array.isArray(promptWorkspace.versions) ? promptWorkspace.versions : [];
  if (!versions.length) {
    container.innerHTML = '<div class="prompt-empty">История пока пуста</div>';
    return;
  }
  container.innerHTML = versions.map((version) => {
    const safeVersion = version || {};
    const active = Boolean(safeVersion.is_active);
    const when = safeVersion.created_at ? new Date(safeVersion.created_at).toLocaleString('ru-RU') : 'Встроенная версия';
    return '<article data-version="' + esc(safeVersion.version) + '" class="prompt-version' + (active ? ' is-current' : '') + '">'
      + '<div class="prompt-version-main"><div class="prompt-version-title"><b>v' + esc(safeVersion.version) + '</b>'
      + (active ? '<span class="prompt-current-version">ТЕКУЩАЯ</span>' : '') + '</div>'
      + '<small>' + esc(when) + ' · ' + Number(safeVersion.length || 0) + ' символов</small></div>'
      + '<div class="prompt-version-stats">' + promptFeedback(safeVersion.feedback) + '</div>'
      + (active ? '<span class="prompt-version-lock">Активна</span>' : '<button type="button" class="prompt-use-button" data-use-version="' + esc(safeVersion.version) + '">Сделать текущей</button>')
      + '</article>';
  }).join('');
  container.querySelectorAll('[data-use-version]').forEach((button) => {
    button.addEventListener('click', () => usePromptVersion(Number(button.dataset.useVersion)));
  });
}

async function loadPromptWorkspace(name) {
  if (promptWorkspace.busy) return;
  const token = ++promptWorkspace.token;
  promptWorkspace.activeName = name;
  clearPromptDetails();
  renderPromptCatalog();
  setPromptBusy(true);
  setPromptStatus('Загрузка…');
  try {
    const active = await api('/api/prompts/' + encodeURIComponent(name), { cache: 'no-store' });
    if (token !== promptWorkspace.token) return;
    applyPromptSnapshot(active);
  } catch (error) {
    if (token === promptWorkspace.token) setPromptStatus('Ошибка: ' + error.message, true);
  } finally {
    if (token === promptWorkspace.token) setPromptBusy(false);
  }
}

async function usePromptVersion(version) {
  const name = promptWorkspace.activeName;
  if (!name || promptWorkspace.busy) return;
  clearPromptDetails();
  setPromptBusy(true);
  setPromptStatus('Переключение версии…');
  try {
    const restored = await api('/api/prompts/' + encodeURIComponent(name) + '/restore', {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ version }),
    });
    applyPromptSnapshot(restored);
  } catch (error) {
    setPromptStatus('Ошибка: ' + error.message, true);
  } finally {
    setPromptBusy(false);
  }
}

async function saveCurrentPrompt() {
  const name = promptWorkspace.activeName;
  if (!name || promptWorkspace.busy || !promptWorkspace.active) return;
  const text = $('#prompt-editor').value;
  setPromptBusy(true);
  setPromptStatus('Сохранение…');
  try {
    const saved = await api('/api/prompts/' + encodeURIComponent(name), {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ text }),
    });
    applyPromptSnapshot(saved);
    setPromptStatus('Новая версия сохранена');
  } catch (error) {
    setPromptStatus('Ошибка: ' + error.message, true);
  } finally {
    setPromptBusy(false);
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
  const restore = setLoading(upload ? 'Читаю выбранный файл…' : 'Читаю isometries.pdf…');
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
  if (!isProviderConnected($('#provider-select').value)) {
    $('#provider-popover').hidden = false;
    $('#provider-toggle').setAttribute('aria-expanded', 'true');
    updateProviderConnectionStatus();
    alert('Выбранный провайдер не подключен');
    return;
  }
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
      body: JSON.stringify({ document_id: state.document.document_id, line_ids: lineIds, stop_stage: $('#stop-stage').value, excluded_pages: excludedPages, provider: $('#provider-select').value }),
    });
    state.runId = response.run_id;
    $('#runs-section').hidden = false;
    $('#result-section').hidden = true;
    $('#history-section').hidden = true;
    $('#prompt-section').hidden = true;
    $('#runs-section').scrollIntoView({ behavior: 'smooth', block: 'start' });
    state.pollToken += 1;
    pollRun(state.pollToken);
  } catch (error) { alert(error.message); }
}

async function pollRun(token) {
  if (token !== state.pollToken) return;
  try {
    const progress = await api('/api/runs/' + state.runId + '/progress');
    if (token !== state.pollToken) return;
    renderRun(progress);
    if (progress.status === 'complete' || progress.status === 'error') {
      $('#runs-section').hidden = true;
      await refreshFeedback();
      if (token !== state.pollToken) return;
      const run = await api('/api/runs/' + state.runId);
      if (token !== state.pollToken) return;
      renderResults(run);
      return;
    }
  } catch (error) {
    $('#run-status').innerHTML = '<div class="badge error">Ошибка: ' + esc(error.message) + '</div>';
    return;
  }
  setTimeout(function () { pollRun(token); }, 1800);
}

async function refreshFeedback() {
  try {
    const data = await api('/api/feedback');
    state.feedback = {};
    (data.rows || []).forEach(function (row) {
      const key = [row.run_id, row.line_id, row.page_number, row.candidate_id].join('|');
      state.feedback[key] = row.rating;
    });
  } catch (error) {
    console.warn('Не удалось загрузить оценки анализа', error);
  }
}

function renderRun(run) {
  state.runId = run.run_id;
  $('#run-title').textContent = run.source_name;
  const running = run.status === 'running';
  $('#run-status').innerHTML = '<div class="muted">Прогон <code>' + esc(run.run_id) + '</code> · до этапа «' + esc(run.stop_stage) + '» · статус: <b>'
    + esc(run.status) + '</b> · провайдер: <b>' + esc(run.provider || run.model || '—') + '</b>' + (running ? ' <span class="spinner"></span>' : '') + '</div>';
  const container = $('#lines-progress');
  container.innerHTML = '';
  for (const line of run.lines) {
    const badge = line.status === 'complete' ? 'ok' : line.status === 'error' ? 'error' : 'run';
    const lineRunning = line.status === 'pending' || line.status === 'running';
    const lineEvents = line.events || [];
    const lineEventsHtml = lineEvents.map(function (event, index) {
      const isCurrent = lineRunning && index === lineEvents.length - 1;
      return '<li class="' + (isCurrent ? 'current' : '') + '">'
        + (isCurrent ? '<span class="spinner"></span> ' : '')
        + '<span class="event-stage">' + esc(STAGE_LABELS[event.stage] || event.stage) + '</span>: ' + esc(event.message)
        + '</li>';
    }).join('');
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
      + '<ul class="events line-events">' + (lineEventsHtml || '') + '</ul>'
      + pipelineLengthSummary(line)
      + lineArtifactLinks(line.line_id, line, state.runId)
      + pagesHtml;
    container.appendChild(card);
  }
}

const VIEWER_TABS = [
  { key: 'local_markup_overview_pdf', label: 'Вся локальная разметка' },
  { key: 'clean_local_markup_pdf', label: 'Локальная разметка' },
  { key: 'lead_detection_pdf', label: 'Поиск lead-стрелок' },
  { key: 'local_dimension_filter_pdf', label: 'Локальная фильтрация размеров' },
  { key: 'pipeline_length_diagnostic_pdf', label: 'Расчет длины: базовая геометрия' },
  { key: 'final_contour_rays_pdf', label: 'Лучевые привязки финального контура' },
  { key: 'numbers_pdf', label: 'Разметка чисел' },
  { key: 'vertices_pdf', label: 'Вершины' },
  { key: 'coordinates_pdf', label: 'Координаты' },
  { key: 'numbers_txt', label: 'Числа (TXT)' },
  { key: 'preprocess_annotations_pdf', label: 'Локальная разметка подготовки' },
  { key: 'dimensions_pdf', label: 'Размеры на графе' },
  { key: 'dimension_graph_pdf', label: 'Чистый граф трубы' },
  { key: 'dimension_skeleton_pdf', label: 'Контур и размерные линии' },
  { key: 'dimensions_json', label: 'Карта размеров (JSON)' },
  { key: 'dimension_map_pdf', label: 'Диагностическая карта' },
  { key: 'dimension_review_json', label: 'Решение провайдера (JSON)' },
];
const PRIMARY_VIEWER_KEYS = new Set([
  'local_markup_overview_pdf',
  'clean_local_markup_pdf',
  'dimensions_json',
  'dimension_map_pdf',
  'dimension_review_json',
]);

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

function lineArtifactLinks(lineId, line, runId) {
  const files = line.files || {};
  if (!Object.keys(files).length) return '';
  return '<div class="row artifacts">' + Object.entries(files).map(function (pair) {
    return '<a class="artifact-tag" href="' + artifactLinkUrl(runId, lineId, pair[1]) + '" download>⭳ ' + esc(pair[0]) + '</a>';
  }).join('') + '</div>';
}

function pipelineLengthEstimates(line) {
  const analysis = line.analysis && line.analysis.pipeline_length;
  const local = analysis && analysis.local_result;
  if (local && Array.isArray(local.candidate_estimates) && local.candidate_estimates.length) return local.candidate_estimates;
  const pages = line.provider_trace && line.provider_trace.payload && line.provider_trace.payload.pages;
  if (!Array.isArray(pages)) return [];
  return pages.flatMap(function (page) {
    return (page.dimensions || []).map(function (row) {
      const decision = row.local_decision || {};
      return {
        candidate_id: row.id,
        candidate_key: row.candidate_key || (page.page + ':' + row.id),
        page: page.page,
        value_mm: row.value_mm,
        edge_id: row.edge_id,
        local_decision: decision.decision,
        reason: decision.reason,
      };
    });
  });
}

function pipelineManualSets(line) {
  const analysis = line.analysis && line.analysis.pipeline_length;
  const manual = (analysis && analysis.manual_confirmation) || {};
  return {
    accepted: new Set((manual.candidate_ids || []).map(String)),
    keptLocal: new Set((manual.keep_local_candidate_ids || []).map(String)),
  };
}

function pipelineProviderAssessmentMap(line) {
  const analysis = line.analysis && line.analysis.pipeline_length;
  const provider = (analysis && analysis.provider_result) || {};
  const map = new Map();
  (Array.isArray(provider.candidate_assessments) ? provider.candidate_assessments : []).forEach(function (item) {
    const keys = [item.candidate_key, item.candidate_id].filter(Boolean).map(String);
    keys.forEach(function (key) { map.set(key, item); });
  });
  return map;
}

function pipelineEffectiveCandidateDecision(line, candidate) {
  const candidateKey = String(candidate.candidate_key || candidate.candidate_id || candidate.id || '');
  const localId = String(candidate.candidate_id || candidate.id || '').split(':').pop();
  const localDecision = candidate.local_decision || candidate.decision || 'ambiguous';
  const manual = pipelineManualSets(line);
  const assessments = pipelineProviderAssessmentMap(line);
  const assessment = assessments.get(candidateKey) || assessments.get(localId);
  if (manual.keptLocal.has(candidateKey) || manual.keptLocal.has(localId)) return localDecision;
  if (manual.accepted.has(candidateKey) || manual.accepted.has(localId)) return (assessment && assessment.proposed_decision) || localDecision;
  if (assessment && assessment.proposed_decision) return assessment.proposed_decision;
  if (assessment && assessment.assessment === 'accepted') return assessment.local_decision || localDecision;
  if (assessment && (assessment.assessment === 'disputed' || assessment.assessment === 'insufficient')) return 'ambiguous';
  return localDecision;
}

function formatMm(value) {
  if (value == null || value === '') return '—';
  const number = Number(value);
  if (!Number.isFinite(number)) return String(value);
  return number.toLocaleString('ru-RU', { maximumFractionDigits: 1 }) + ' мм';
}

function formatSeconds(value) {
  if (value == null || value === '') return '—';
  const number = Number(value);
  if (!Number.isFinite(number)) return String(value) + ' с';
  return number.toLocaleString('ru-RU', { maximumFractionDigits: 1 }) + ' с';
}

function decisionBadge(decision) {
  const normalized = String(decision || 'ambiguous').toLowerCase();
  const label = normalized === 'include' ? 'include'
    : normalized === 'exclude' ? 'exclude'
      : normalized === 'accepted' ? 'accepted'
        : normalized === 'disputed' ? 'disputed'
          : normalized === 'insufficient' ? 'insufficient'
            : normalized || 'ambiguous';
  return '<span class="decision-pill decision-' + esc(label.replace(/[^a-z0-9_-]/g, '-')) + '">' + esc(label) + '</span>';
}

function pipelineAdjacentSheetItems(line) {
  const analysis = line.analysis && line.analysis.pipeline_length;
  const provider = (analysis && analysis.provider_result) || {};
  const items = [];
  (Array.isArray(provider.intermediate_distances) ? provider.intermediate_distances : []).forEach(function (item, index) {
    items.push({ ...item, _kind: 'intermediate_distance', _index: index });
  });
  (Array.isArray(provider.cross_sheet_measurements) ? provider.cross_sheet_measurements : []).forEach(function (item) {
    items.push({ ...item, _kind: 'cross_sheet_measurement' });
  });
  (Array.isArray(provider.cross_sheet_links) ? provider.cross_sheet_links : []).forEach(function (item) {
    items.push({ ...item, _kind: 'cross_sheet_link' });
  });
  return items;
}

function pipelineAdjacentKindLabel(kind) {
  if (kind === 'intermediate_distance') return 'смежная длина';
  if (kind === 'cross_sheet_measurement') return 'размер с другого листа';
  if (kind === 'cross_sheet_link') return 'ссылка СМ ЛИСТ';
  return 'межлистовая связь';
}

function pipelineAdjacentPageLabel(item) {
  const from = item.from_page || item.page || '?';
  const to = item.to_page || item.target_page || item.sheet || item.target_sheet || '?';
  return String(from) + ' → ' + String(to);
}

function pipelineVertexCoordinates(line) {
  const analysis = line.analysis && line.analysis.pipeline_length;
  const provider = (analysis && analysis.provider_result) || {};
  return Array.isArray(provider.vertex_coordinates) ? provider.vertex_coordinates : [];
}

function pipelineVertexCoordinateKey(item) {
  if (item.vertex_key) return String(item.vertex_key);
  const vertexId = String(item.vertex_id || item.id || '');
  if (vertexId.includes(':')) return vertexId;
  const page = item.page || item.from_page;
  return page ? String(page) + ':' + vertexId : vertexId;
}

function pipelineVertexCoordinateMap(line) {
  const pages = line.provider_trace && line.provider_trace.payload && line.provider_trace.payload.pages;
  const providerRows = pipelineVertexCoordinates(line);
  const byKey = new Map(providerRows.map(function (item) {
    return [pipelineVertexCoordinateKey(item), item];
  }));
  const localByKey = new Map();
  (Array.isArray(pages) ? pages : []).forEach(function (page) {
    pipelineProviderPageVertexRows(page).forEach(function (vertex) {
      const local = vertex.local_coordinates || {};
      if (local.x != null && local.y != null && local.z != null) {
        localByKey.set(String(page.page) + ':' + String(vertex.id), local);
      }
    });
  });
  const invalidProviderKeys = new Set();
  (Array.isArray(pages) ? pages : []).forEach(function (page) {
    const dimensionsByKey = new Map((page.dimensions || []).map(function (dimension) {
      return [String(dimension.candidate_key || ''), dimension];
    }));
    const coordsFor = function (vertexId) {
      const key = String(page.page) + ':' + String(vertexId);
      const local = localByKey.get(key);
      if (local) return [Number(local.x), Number(local.y), Number(local.z)];
      const provider = byKey.get(key);
      if (!provider || provider.x == null || provider.y == null || provider.z == null) return null;
      return [Number(provider.x), Number(provider.y), Number(provider.z)];
    };
    (page.final_edges || []).forEach(function (edge) {
      const from = coordsFor(edge.from_vertex);
      const to = coordsFor(edge.to_vertex);
      if (!from || !to) return;
      const edgeCandidates = (edge.candidate_keys || []).map(function (key) {
        return dimensionsByKey.get(String(key));
      }).filter(Boolean);
      const value = edgeCandidates
        .filter(function (candidate) { return candidate.local_decision === 'include'; })
        .map(function (candidate) { return Number(candidate.value_mm ?? candidate.value ?? candidate.length_mm); })
        .find(Number.isFinite);
      if (!Number.isFinite(value) || value <= 0) return;
      const distance = Math.hypot(from[0] - to[0], from[1] - to[1], from[2] - to[2]);
      const tolerance = Math.max(8, value * 0.15);
      if (Math.abs(distance - value) > tolerance) {
        [edge.from_vertex, edge.to_vertex].forEach(function (vertexId) {
          const key = String(page.page) + ':' + String(vertexId);
          if (!localByKey.has(key) && byKey.has(key)) invalidProviderKeys.add(key);
        });
      }
    });
  });
  const map = new Map();
  providerRows.forEach(function (item) {
    const key = pipelineVertexCoordinateKey(item);
    const confidence = item.confidence == null ? null : Number(item.confidence);
    const hasXYZ = item.x != null && item.y != null && item.z != null;
    // A complete provider coordinate is usable for geometry even when its
    // confidence is below the review threshold. The UI marks it as uncertain;
    // it must not silently fall back to sheet pixels or Z=0.
    if (!hasXYZ || !key || invalidProviderKeys.has(key)) return;
    map.set(key, item);
  });
  return map;
}

function pipelineProviderPageVertexRows(page) {
  const reconstruction = page && page.coordinate_reconstruction;
  if (!reconstruction) return page && (page.final_vertices || page.vertices) || [];
  return ['known_vertices', 'partial_vertices', 'unknown_vertices'].flatMap(function (group) {
    return (reconstruction[group] || []).map(function (row) {
      return {
        id: row.vertex_id,
        vertex_key: row.vertex_key,
        point: row.sheet_point,
        role: row.role,
        handwheel_id: row.handwheel_id,
        coordinate_refs: row.coordinate_refs || [],
        local_coordinates: row,
      };
    });
  });
}

function pipelineLocalVertexCoordinateRows(line) {
  const pages = line.provider_trace && line.provider_trace.payload && line.provider_trace.payload.pages;
  if (Array.isArray(pages)) {
    return pages.flatMap(function (page) {
      return pipelineProviderPageVertexRows(page).map(function (vertex) {
        const row = vertex.local_coordinates || {};
        return Object.assign({ page: page.page }, row);
      });
    });
  }
  return (line.page_results || []).flatMap(function (page) {
    const snapshot = page.analysis && page.analysis.pipeline_length_local;
    return (snapshot && Array.isArray(snapshot.final_vertices) ? snapshot.final_vertices : []).map(function (vertex) {
      const point = Array.isArray(vertex.point) ? vertex.point : [];
      return {
        vertex_id: vertex.id,
        vertex_key: page.page_number + ':' + vertex.id,
        page: page.page_number,
        sheet_x: point[0],
        sheet_y: point[1],
        x: null,
        y: null,
        z: null,
        confidence: 0,
        method: 'не определено',
      };
    });
  });
}

function pipelineLengthSummary(line) {
  const analysis = line.analysis && line.analysis.pipeline_length;
  if (!analysis) return '';
  const local = analysis.local_result || {};
  const estimates = pipelineLengthEstimates(line);
  const localClean = local.clean_length_mm ?? estimates.filter(function (row) { return row.local_decision === 'include'; }).reduce(function (sum, row) { return sum + Number(row.value_mm || 0); }, 0);
  const localDirty = local.dirty_length_mm ?? estimates.filter(function (row) { return row.local_decision !== 'exclude'; }).reduce(function (sum, row) { return sum + Number(row.value_mm || 0); }, 0);
  const provider = analysis.provider_result || {};
  const manual = analysis.manual_confirmation || {};
  const lengths = provider.calculated_lengths || provider.lengths || {};
  const trace = line.provider_trace || {};
  const route = function (label, value) {
    value = value || {};
    return '<div class="pipeline-route-card ' + (label === 'Ответвления' ? 'branch' : 'main') + '"><span>' + esc(label) + '</span><strong>' + esc(formatMm(value.clean_length_mm)) + '</strong><small>грязная ' + esc(formatMm(value.dirty_length_mm)) + ' · сомнения ' + esc(formatMm(value.ambiguous_length_mm)) + '</small></div>';
  };
  const assessments = Array.isArray(provider.candidate_assessments) ? provider.candidate_assessments : [];
  const disputes = assessments.filter(function (item) { return item.assessment === 'disputed' || item.assessment === 'insufficient'; });
  const adjacentItems = pipelineAdjacentSheetItems(line);
  const intermediate = adjacentItems.filter(function (item) { return item._kind === 'intermediate_distance'; });
  const branches = Array.isArray(lengths.branches) ? lengths.branches : [];
  const manualAccepted = (manual.candidate_ids || []).length;
  const manualKept = (manual.keep_local_candidate_ids || []).length;
  const localCoordinateRows = pipelineLocalVertexCoordinateRows(line);
  const providerCoordinateMap = pipelineVertexCoordinateMap(line);
  const coordinateReady = localCoordinateRows.filter(function (row) {
    const key = String(row.vertex_key || (row.page + ':' + row.vertex_id));
    const localConfidence = Number(row.confidence || 0);
    const hasLocalXYZ = row.x != null && row.y != null && row.z != null
      && (!Number.isFinite(localConfidence) || localConfidence >= 0.7);
    return hasLocalXYZ || providerCoordinateMap.has(key);
  }).length;
  const coordinateUnknown = Math.max(0, localCoordinateRows.length - coordinateReady);
  const handwheelCount = (line.page_results || []).reduce(function (total, page) {
    const snapshot = page.analysis && page.analysis.pipeline_length_local;
    const annotations = snapshot && snapshot.handwheel_annotations;
    return total + (annotations && Array.isArray(annotations.glyphs) ? annotations.glyphs.length : 0);
  }, 0);
  return '<details class="pipeline-summary collapsible-result" open><summary>Расчет длины трубопровода</summary>'
    + '<div class="pipeline-summary-grid">'
    + '<div class="pipeline-total-card local"><small>Локальный расчет</small><strong>' + esc(formatMm(localClean)) + '</strong><span>грязная ' + esc(formatMm(localDirty)) + ' · сомнения ' + esc(formatMm(local.ambiguous_length_mm)) + '</span></div>'
    + '<div class="pipeline-total-card provider"><small>Подтверждение провайдера</small><strong>' + esc(formatMm(lengths.total_clean_length_mm)) + '</strong><span>грязная ' + esc(formatMm(lengths.total_dirty_length_mm)) + ' · сомнения ' + esc(formatMm(lengths.total_ambiguous_length_mm)) + '</span></div>'
    + '<div class="pipeline-total-card trace"><small>AI запрос</small><strong>' + esc(formatSeconds(trace.elapsed_seconds)) + '</strong><span>' + esc(trace.total_tokens != null ? trace.total_tokens + ' токенов' : 'токены: —') + '</span></div>'
    + '<div class="pipeline-total-card manual"><small>Ручные решения</small><strong>+' + esc(manualAccepted) + ' / −' + esc(manualKept) + '</strong><span>+ провайдер · − локальное</span></div>'
    + '</div>'
    + '<div class="pipeline-route-grid">' + route('Основная линия', lengths.main) + route('Ответвления', lengths.branch) + '</div>'
    + '<div class="pipeline-meta-row">'
    + '<span>Штурвалы: <b>' + esc(handwheelCount) + '</b></span>'
    + '<span>Спорные: <b>' + esc(disputes.length) + '</b></span>'
    + '<span>Смежные: <b>' + esc(intermediate.length) + '</b></span>'
    + '<span>3D координаты: <b>' + esc(coordinateReady) + '/' + esc(localCoordinateRows.length) + '</b></span>'
    + '<span>Автоприменение: <b>нет</b></span>'
    + '</div>'
    + (coordinateUnknown ? '<p class="pipeline-note"><b>Координаты / 3D готовность:</b> ' + esc(coordinateUnknown) + ' вершин требуют проверки координат.</p>' : '')
    + (branches.length ? '<div class="pipeline-chip-row">' + branches.map(function (branch) { return '<span class="pipeline-chip">' + esc(branch.branch_id || 'BR') + ' · ' + esc(formatMm(branch.clean_length_mm)) + '</span>'; }).join('') + '</div>' : '')
    + (disputes.length ? '<p class="pipeline-note"><b>Требуют внимания:</b> ' + esc(disputes.map(function (item) { return item.candidate_id; }).join(', ')) + '</p>' : '')
    + (intermediate.length ? '<p class="pipeline-note"><b>Смежные расстояния:</b> найдены провайдером, детали ниже.</p>' : '')
    + '</details>';
}

function pipelineAdjacentSheetBlock(line) {
  const analysis = line.analysis && line.analysis.pipeline_length;
  const manual = (analysis && analysis.manual_confirmation) || {};
  const accepted = new Set((manual.intermediate_distance_indices || []).map(function (item) { return Number(item); }));
  const items = pipelineAdjacentSheetItems(line);
  if (!items.length) return '';
  const rows = items.map(function (item) {
    const value = item.value_mm ?? item.length_mm ?? item.distance_mm;
    const hasValue = value != null && value !== '';
    const isIntermediate = item._kind === 'intermediate_distance';
    const isAccepted = isIntermediate && accepted.has(Number(item._index));
    const source = item.source || item.source_text || item.label || item.reference_text || item.candidate_id || '—';
    const reason = item.reason || item.explanation || item.comment || item.note || '—';
    const confidence = item.confidence != null ? item.confidence : (item.status || '—');
    return '<tr>'
      + '<td><b>' + esc(pipelineAdjacentKindLabel(item._kind)) + '</b><br><span class="muted">' + esc(pipelineAdjacentPageLabel(item)) + '</span></td>'
      + '<td>' + (hasValue ? '<b>' + esc(formatMm(value)) + '</b>' : '<span class="muted">без длины</span>') + '<br><span class="muted">' + esc(item.route_type || item.route || 'main') + '</span></td>'
      + '<td>' + esc(source) + '</td>'
      + '<td>' + esc(confidence) + '</td>'
      + '<td>' + esc(reason) + '</td>'
      + '<td>' + (isAccepted
        ? '<span class="manual-choice">учтено вручную</span>'
        : (isIntermediate && hasValue ? '<button type="button" class="provider-confirm-button compact" data-provider-line="' + esc(line.line_id) + '" data-provider-action="accept_distance" data-provider-distance-index="' + esc(item._index) + '">Учесть в расчете</button>' : '<span class="muted">только справочно</span>'))
      + '</td>'
      + '</tr>';
  }).join('');
  return '<details class="notes collapsible-result pipeline-adjacent-block" open><summary>Смежные длины / СМ ЛИСТ (' + items.length + ')</summary>'
    + '<div class="table-wrap"><table class="viewer-table pipeline-adjacent-table"><thead><tr><th>Тип</th><th>Длина</th><th>Источник</th><th>Уверенность</th><th>Пояснение</th><th>Решение</th></tr></thead><tbody>'
    + rows
    + '</tbody></table></div></details>';
}

function pipelineVertexCoordinatesBlock(line) {
  const rows = pipelineVertexCoordinates(line);
  if (!rows.length) return '';
  const html = rows.map(function (item) {
    const vertex = item.vertex_id || item.id || '—';
    const local = item.local_vertex_id && item.local_vertex_id !== vertex ? '<br><span class="muted">' + esc(item.local_vertex_id) + '</span>' : '';
    const xyz = [item.x, item.y, item.z].map(function (value) { return value == null || value === '' ? '—' : String(value); }).join(' / ');
    const sources = Array.isArray(item.source_coordinate_labels)
      ? item.source_coordinate_labels.join(', ')
      : (item.source_coordinate_labels || item.source || '—');
    const confidence = item.confidence != null ? item.confidence : '—';
    return '<tr>'
      + '<td><b>' + esc(vertex) + '</b>' + local + '<br><span class="muted">лист ' + esc(item.page || '—') + '</span></td>'
      + '<td class="mono">' + esc(xyz) + '</td>'
      + '<td>' + esc(item.method || '—') + '</td>'
      + '<td>' + esc(confidence) + '</td>'
      + '<td>' + esc(sources) + '</td>'
      + '<td>' + esc(item.reason || '—') + '</td>'
      + '</tr>';
  }).join('');
  return '<details class="notes collapsible-result pipeline-vertex-coordinates" open><summary>Координаты вершин от провайдера (' + rows.length + ')</summary>'
    + '<div class="table-wrap"><table class="viewer-table pipeline-coordinates-table"><thead><tr><th>Вершина</th><th>X / Y / Z</th><th>Метод</th><th>Уверенность</th><th>Источник</th><th>Пояснение</th></tr></thead><tbody>'
    + html
    + '</tbody></table></div></details>';
}

function pipelineProviderAssessment(provider, estimate) {
  const assessments = Array.isArray(provider && provider.candidate_assessments) ? provider.candidate_assessments : [];
  const key = String(estimate.candidate_key || '');
  const id = String(estimate.candidate_id || '');
  const item = assessments.find(function (candidate) {
    const candidateId = String(candidate.candidate_id || '');
    return candidateId === key || candidateId === id || candidateId.endsWith(':' + id);
  });
  if (!item) return '<span class="muted">нет ответа</span>';
  const proposed = item.proposed_decision ? ' · предложение: ' + item.proposed_decision : '';
  return decisionBadge(item.assessment || 'без оценки') + (proposed ? '<span class="provider-proposal">' + esc(proposed) + '</span>' : '')
    + (item.reason ? '<br><span class="muted">' + esc(item.reason) + '</span>' : '');
}

function pipelineLengthPageDetail(line, pageNumber) {
  const analysis = line.analysis && line.analysis.pipeline_length;
  if (!analysis) return '';
  const local = analysis.local_result || {};
  const provider = analysis.provider_result || {};
  const manual = analysis.manual_confirmation || {};
  const pageResult = (line.page_results || []).find(function (page) { return Number(page.page_number) === Number(pageNumber); });
  const diagnosticFile = pageResult && pageResult.files && pageResult.files.pipeline_length_diagnostic_pdf;
  const diagnosticMarkup = diagnosticFile
    ? '<details class="notes collapsible-result" open><summary>Изображение для проверки провайдера</summary><img class="viewer-frame" src="' + viewerImageUrl(state.runId, line.line_id, diagnosticFile) + '" alt="Диагностика расчета длины, лист ' + esc(pageNumber) + '"></details>'
    : '';
  const estimates = pipelineLengthEstimates(line).filter(function (row) { return Number(row.page) === Number(pageNumber); });
  const payloadPages = line.provider_trace && line.provider_trace.payload && line.provider_trace.payload.pages;
  const payloadPage = Array.isArray(payloadPages) ? payloadPages.find(function (page) { return Number(page.page) === Number(pageNumber); }) : null;
  const unresolvedLocal = payloadPage && Array.isArray(payloadPage.dimensions)
    ? payloadPage.dimensions.filter(function (row) { return row.is_unresolved; })
    : [];
  const unresolvedProvider = Array.isArray(provider.unresolved_reviews) ? provider.unresolved_reviews : [];
  const adjacentForPage = pipelineAdjacentSheetItems(line).filter(function (item) {
    return Number(item.page || item.from_page) === Number(pageNumber);
  });
  const pageSummary = (local.page_summaries || []).find(function (row) { return Number(row.page) === Number(pageNumber); }) || {
    clean_length_mm: estimates.filter(function (row) { return row.local_decision === 'include'; }).reduce(function (sum, row) { return sum + Number(row.value_mm || 0); }, 0),
  };
  const rows = estimates.map(function (row) {
    const matchingAssessment = (provider.candidate_assessments || []).find(function (item) {
      const candidateId = String(item.candidate_id || '');
      return candidateId === String(row.candidate_key) || candidateId.endsWith(':' + String(row.candidate_id));
    });
    const candidateKey = row.candidate_key || row.candidate_id;
    const providerCandidateId = matchingAssessment?.candidate_id || row.candidate_key || row.candidate_id;
    const providerAssessment = String(matchingAssessment?.assessment || '').toLowerCase();
    const providerProposal = matchingAssessment?.proposed_decision;
    const localDecision = row.local_decision || 'ambiguous';
    const manuallyAccepted = (manual.candidate_ids || []).map(String).includes(String(candidateKey)) || (manual.candidate_ids || []).some(function (item) { return String(item).endsWith(':' + String(row.candidate_id)); });
    const manuallyKept = (manual.keep_local_candidate_ids || []).map(String).includes(String(candidateKey)) || (manual.keep_local_candidate_ids || []).some(function (item) { return String(item).endsWith(':' + String(row.candidate_id)); });
    const manualChoice = manuallyAccepted
      ? 'принято решение провайдера'
      : (manuallyKept ? 'оставлено локальное решение' : '');
    const editKey = [line.line_id, providerCandidateId].join('|');
    const isEditingManualChoice = state.providerDecisionEdit.has(editKey);
    const actionableProposal = matchingAssessment && providerAssessment !== 'accepted' && providerProposal && providerProposal !== localDecision;
    const showDecisionButtons = actionableProposal && (!manualChoice || isEditingManualChoice);
    const manualMarkup = manualChoice
      ? '<br><span class="manual-choice">ручной фикс: ' + esc(manualChoice) + '</span>'
        + (!isEditingManualChoice ? '<button type="button" class="provider-edit-button" data-provider-edit="' + esc(editKey) + '">Изменить</button>' : '')
      : '';
    return '<tr><td><b>' + esc(row.candidate_key || row.candidate_id) + '</b><br><span class="muted">' + esc(row.edge_id || 'без ребра') + '</span></td>'
      + '<td><b>' + esc(formatMm(row.value_mm)) + '</b><br>' + decisionBadge(row.local_decision || 'ambiguous') + '<br><span class="muted">' + esc(row.reason || '') + '</span></td>'
      + '<td>' + pipelineProviderAssessment(provider, row) + manualMarkup
      + (showDecisionButtons ? '<div class="provider-actions"><button type="button" class="provider-confirm-button" data-provider-line="' + esc(line.line_id) + '" data-provider-action="accept" data-provider-candidate="' + esc(providerCandidateId) + '">Принять провайдера</button>'
        + '<button type="button" class="provider-confirm-button secondary" data-provider-line="' + esc(line.line_id) + '" data-provider-action="keep_local" data-provider-candidate="' + esc(providerCandidateId) + '">Оставить локальное</button></div>' : '')
      + '</td></tr>';
  }).join('');
  const adjacentMarkup = adjacentForPage.length
    ? '<details class="notes collapsible-result pipeline-page-adjacent"><summary>СМ ЛИСТ на этом листе (' + adjacentForPage.length + ')</summary>'
      + '<ul class="pipeline-adjacent-list">' + adjacentForPage.map(function (item) {
        const value = item.value_mm ?? item.length_mm ?? item.distance_mm;
        return '<li><b>' + esc(pipelineAdjacentKindLabel(item._kind)) + '</b>: ' + esc(pipelineAdjacentPageLabel(item)) + (value != null ? ' · ' + esc(formatMm(value)) : '') + (item.reason || item.explanation ? '<br><span class="muted">' + esc(item.reason || item.explanation) + '</span>' : '') + '</li>';
      }).join('') + '</ul></details>'
    : '';
  const unresolvedMarkup = unresolvedLocal.length
    ? '<details class="notes collapsible-result pipeline-unresolved-block" open><summary>Не попали на отрезки (' + unresolvedLocal.length + ')</summary>'
      + '<div class="table-wrap"><table class="viewer-table"><thead><tr><th>Кандидат</th><th>Локальная причина</th><th>Провайдер</th></tr></thead><tbody>'
      + unresolvedLocal.map(function (row) {
        const key = String(row.candidate_key || row.candidate_id || '');
        const review = unresolvedProvider.find(function (item) {
          return String(item.candidate_key || item.candidate_id || '') === key || String(item.candidate_id || '').endsWith(':' + String(row.candidate_id || ''));
        }) || {};
        return '<tr><td><b>' + esc(key || row.candidate_id || '—') + '</b><br><span class="muted">' + esc(formatMm(row.value_mm)) + '</span></td>'
          + '<td>' + esc(row.unresolved_reason || row.final_interval_reason || row.reason || '—') + '</td>'
          + '<td>' + esc(review.status || 'нет ответа') + '<br><span class="muted">' + esc(review.reason || review.suggested_action || '') + '</span></td></tr>';
      }).join('')
      + '</tbody></table></div></details>'
    : '';
  return diagnosticMarkup + adjacentMarkup + unresolvedMarkup + '<section class="pipeline-length-page">'
    + '<div class="pipeline-page-heading"><h2>Лист ' + esc(pageNumber) + '</h2><span>предварительная сумма ' + esc(formatMm(pageSummary.clean_length_mm ?? 0)) + '</span></div>'
    + '<div class="table-wrap pipeline-review-wrap"><table class="data-table pipeline-review-table"><thead><tr><th>Кандидат</th><th>Локальное решение</th><th>Подтверждение провайдера</th></tr></thead><tbody>'
    + (rows || '<tr><td colspan="3">Размерных кандидатов нет</td></tr>')
    + '</tbody></table></div></section>'
    + (pageResult ? viewerCard(line.line_id, pageResult, state.runId) : '');
}

function pipelineHandwheelBlock(line) {
  const rows = [];
  (line.page_results || []).forEach(function (page) {
    const annotations = page.analysis && page.analysis.pipeline_length_local && page.analysis.pipeline_length_local.handwheel_annotations;
    (annotations && annotations.handwheels || []).forEach(function (item) {
      rows.push('<tr><td>' + esc(page.page_number) + '</td><td>' + esc(item.id || 'HW') + '</td><td>' + esc(item.label || 'штурвал') + '</td><td>' + esc(item.arrow_found ? 'найден lead' : 'без lead') + '</td><td>' + esc(item.edge_id || '—') + '</td></tr>');
    });
  });
  return '<details class="notes collapsible-result" open><summary>Штурвалы (' + rows.length + ')</summary>'
    + (rows.length ? '<table class="viewer-table"><thead><tr><th>Лист</th><th>ID</th><th>Обозначение</th><th>Привязка</th><th>Базовое ребро</th></tr></thead><tbody>' + rows.join('') + '</tbody></table>' : '<p class="muted">Штурвалы не найдены.</p>')
    + '</details>';
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
  const primaryTabs = tabs.filter(function (tab) { return PRIMARY_VIEWER_KEYS.has(tab.key) || tab.key === 'ai_trace' || tab.key === 'graph' || tab.key === 'graph_3d'; });
  const otherTabs = tabs.filter(function (tab) { return !primaryTabs.includes(tab); });
  const initial = primaryTabs[0] || tabs[0];
  const renderButtons = function (items) { return items.map(function (tab, index) {
    return '<button type="button" data-tab="' + esc(tab.key) + '" class="viewer-tab' + (index === 0 ? ' on' : '') + '">'
      + esc(tab.label) + '</button>';
  }).join(''); };
  const initialBody = initial.key === 'ai_trace'
    ? '<p class="muted">Откройте вкладку «AI запрос».</p>'
    : initial.key === 'graph'
      ? renderGraph(pageResult.analysis.graph)
    : initial.key === 'graph_3d'
      ? '<div class="graph-3d-loading"><span class="spinner"></span> Загружаю 3D-сцену...</div>'
    : '<img class="viewer-frame" src="' + viewerImageUrl(runId, lineId, files[initial.key]) + '" alt="">';
  return '<div class="viewer" data-line="' + esc(lineId) + '" data-page="' + pageResult.page_number + '">'
    + '<div class="row viewer-tabs">' + renderButtons(primaryTabs) + (otherTabs.length ? '<button type="button" class="viewer-tab viewer-more" data-show-other-tabs>Показать прочие</button>' : '') + '</div>'
    + (otherTabs.length ? '<div class="row viewer-tabs viewer-other-tabs" hidden>' + renderButtons(otherTabs) + '</div>' : '')
    + '<div class="viewer-frame-wrap">' + initialBody + '</div>'
    + '</div>';
}

function bindViewer(card) {
  card.querySelectorAll('.viewer-tab').forEach(function (button) {
    if (!button.dataset.tab) return;
    button.addEventListener('click', function () { showViewerTab(card, button.dataset.tab); });
  });
  const more = card.querySelector('[data-show-other-tabs]');
  if (more) {
    more.addEventListener('click', function () {
      const other = card.querySelector('.viewer-other-tabs');
      other.hidden = !other.hidden;
      more.textContent = other.hidden ? 'Показать прочие' : 'Скрыть прочие';
    });
  }
}

function copyViewerText(button, text) {
  navigator.clipboard.writeText(text).then(function () {
    const original = button.textContent;
    button.textContent = 'Скопировано';
    setTimeout(function () { button.textContent = original; }, 1200);
  });
}

function copyableBlock(text, className) {
  return '<div class="copyable-text"><button type="button" class="copy-text-button">Копировать</button><pre class="' + (className || 'raw-json') + '">' + esc(text) + '</pre></div>';
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

function renderTrace(trace, imageMarkup) {
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
    + (trace.elapsed_seconds != null ? 'время: ' + esc(trace.elapsed_seconds) + ' с · ' : '')
    + (trace.total_tokens != null ? 'токены: ' + esc(trace.total_tokens) : '');
  const section = function (title, text) {
    return '<details class="trace-section"><summary>' + esc(title) + '</summary><div class="trace-content"><button type="button" class="copy-text-button trace-copy">Копировать</button><pre class="raw-json">' + esc(text) + '</pre></div></details>';
  };
  return '<div class="trace-block">'
    + '<p class="muted">' + meta + '</p>'
    + (trace.error ? '<div class="badge error trace-error">Ошибка запроса: ' + esc(trace.error) + '</div>' : '')
    + ((trace.errors || []).length ? '<div class="badge error trace-error">Ошибки по листам:<br>' + trace.errors.map(function (item) { return 'стр. ' + esc(item.page) + ': ' + esc(item.error); }).join('<br>') + '</div>' : '')
    + section('Prompt', trace.prompt || '')
    + section('Payload', payloadPretty)
    + section('Ответ (raw)', responsePretty)
    + (answerPretty ? section('Ответ (parsed)', answerPretty) : '')
    + (imageMarkup || '')
    + '<button type="button" class="trace-back-button" aria-label="Вернуться к выбору AI запроса">↑ AI запрос</button>'
    + '</div>';
}

function bindTraceControls(card, wrap) {
  wrap.querySelectorAll('.trace-copy').forEach(function (button) {
    button.addEventListener('click', function () {
      const content = button.parentElement.querySelector('.raw-json');
      copyViewerText(button, content ? content.textContent : '');
    });
  });
  const backButton = wrap.querySelector('.trace-back-button');
  if (backButton) {
    backButton.addEventListener('click', function () {
      const tab = card.querySelector('.viewer-tab[data-tab="ai_trace"]');
      if (tab) {
        tab.scrollIntoView({ behavior: 'smooth', block: 'center' });
        tab.focus({ preventScroll: true });
      }
    });
  }
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
  const providerCoordinates = pipelineVertexCoordinateMap(line);
  const applyProviderCoordinates = function (node, pageNumber, localVertexId) {
    const localId = String(localVertexId || node.local_vertex_id || node.id || '');
    const coordinate = providerCoordinates.get(String(pageNumber) + ':' + localId) || providerCoordinates.get(localId);
    if (!coordinate) return node;
    const next = Object.assign({}, node, {
      x: coordinate.x ?? node.x,
      y: coordinate.y ?? node.y,
      z: coordinate.z ?? node.z,
      coordinate_source: 'provider',
      coordinate_confidence: coordinate.confidence,
      coordinate_reason: coordinate.reason,
      source_coordinate_labels: coordinate.source_coordinate_labels,
    });
    return next;
  };
  const rememberDimension = function (segment) {
    if (!segment || typeof segment !== 'object') return;
    const from = segment.from || segment.from_node_id || segment.start || segment.start_node_id;
    const to = segment.to || segment.to_node_id || segment.end || segment.end_node_id;
    const value = segment.value ?? segment.length_mm ?? segment.length;
    if (from != null && to != null && value != null) dimensions.set(String(from) + '->' + String(to), value);
  };
  const payloadPages = line.provider_trace && line.provider_trace.payload && line.provider_trace.payload.pages;
  let usedFinalPayloadGraph = false;
  if (Array.isArray(payloadPages)) {
    payloadPages.forEach(function (page) {
      const pageNumber = page.page;
      pipelineProviderPageVertexRows(page).forEach(function (vertex) {
        const localId = String(vertex.id || vertex.vertex_id || '');
        if (!localId) return;
        const id = String(pageNumber) + ':' + localId;
        const localCoordinates = vertex.local_coordinates || {};
        const providerCoordinate = providerCoordinates.get(id) || providerCoordinates.get(localId);
        const hasProviderXYZ = providerCoordinate && providerCoordinate.x != null && providerCoordinate.y != null && providerCoordinate.z != null;
        const localConfidence = Number(localCoordinates.confidence || 0);
        const hasLocalXYZ = localCoordinates.x != null && localCoordinates.y != null && localCoordinates.z != null
          && (!Number.isFinite(localConfidence) || localConfidence >= 0.7);
        const useProviderXYZ = !!hasProviderXYZ && !hasLocalXYZ;
        const hasKnownXYZ = !!hasProviderXYZ || !!hasLocalXYZ;
        const node = Object.assign({}, vertex, {
          id: id,
          local_vertex_id: localId,
          page: pageNumber,
          pages: [pageNumber],
          // Sheet coordinates are drawing pixels, not model coordinates.
          // Keep them available as metadata, but never use them as a partial
          // 3D point when a provider/local XYZ tuple is absent.
          x: hasLocalXYZ ? localCoordinates.x : (useProviderXYZ ? providerCoordinate.x : null),
          y: hasLocalXYZ ? localCoordinates.y : (useProviderXYZ ? providerCoordinate.y : null),
          z: hasLocalXYZ ? localCoordinates.z : (useProviderXYZ ? providerCoordinate.z : null),
          sheet_x: vertex.sheet_x,
          sheet_y: vertex.sheet_y,
          coordinate_source: hasLocalXYZ ? (localCoordinates.method || 'local') : (useProviderXYZ ? 'provider' : 'sheet_position'),
          coordinate_confidence: hasLocalXYZ ? (localCoordinates.confidence || 0) : (useProviderXYZ ? providerCoordinate.confidence : 0),
          coordinate_unknown: !hasKnownXYZ,
          coordinate_uncertain: (hasLocalXYZ && Number(localCoordinates.confidence || 0) < 0.5)
            || (useProviderXYZ && Number(providerCoordinate.confidence) < 0.5),
          coordinate_reason: hasLocalXYZ ? (localCoordinates.reason || 'локальные координаты') : (useProviderXYZ ? providerCoordinate.reason : 'координаты X/Y/Z не подтверждены'),
        });
        nodes.set(id, node);
      });
      (Array.isArray(page.final_edges || page.edges) ? (page.final_edges || page.edges) : []).forEach(function (edge) {
        const fromLocal = String(edge.from_vertex || '');
        const toLocal = String(edge.to_vertex || '');
        if (!fromLocal || !toLocal) return;
        const from = String(pageNumber) + ':' + fromLocal;
        const to = String(pageNumber) + ':' + toLocal;
        if (!nodes.has(from) || !nodes.has(to) || from === to) return;
        const key = from + '->' + to;
        if (edges.has(key)) {
          duplicates.push({ from_node_id: from, to_node_id: to, page: pageNumber });
          return;
        }
        const dimensionsByKey = new Map((page.dimensions || []).map(function (dimension) {
          return [String(dimension.candidate_key || ''), dimension];
        }));
        const edgeCandidates = Array.isArray(edge.candidates)
          ? edge.candidates
          : (edge.candidate_keys || []).map(function (key) { return dimensionsByKey.get(String(key)); }).filter(Boolean);
        const includedCandidates = edgeCandidates.filter(function (candidate) {
          return pipelineEffectiveCandidateDecision(line, candidate) === 'include';
        });
        const activeDimension = includedCandidates.length
          ? includedCandidates.map(function (candidate) { return Number(candidate.value_mm ?? candidate.value ?? candidate.length_mm); }).find(Number.isFinite)
          : null;
        edges.set(key, Object.assign({}, edge, {
          id: String(pageNumber) + ':' + String(edge.id || edge.edge_id || ('F' + (edges.size + 1))),
          from_node_id: from,
          to_node_id: to,
          page: pageNumber,
          dimension_mm: activeDimension ?? edge.length_mm ?? edge.value,
          active_candidate_ids: includedCandidates.map(function (candidate) { return candidate.candidate_key || candidate.candidate_id || candidate.id; }).filter(Boolean),
          is_handwheel_segment: !!edge.is_handwheel_segment,
        }));
      });
    });
    usedFinalPayloadGraph = nodes.size > 0 || edges.size > 0;
  }
  if (usedFinalPayloadGraph) {
    const connected = new Set();
    edges.forEach(function (edge) { connected.add(edge.from_node_id); connected.add(edge.to_node_id); });
    nodes.forEach(function (_node, id) { if (!connected.has(id)) isolated.add(id); });
    return { nodes: Array.from(nodes.values()), edges: Array.from(edges.values()), duplicate_edges: duplicates, isolated_nodes: Array.from(isolated) };
  }
  (line.page_results || []).forEach(function (pageResult) {
    const graph = pageResult.analysis && pageResult.analysis.graph;
    const analysis = pageResult.analysis || {};
    const localSnapshot = analysis.pipeline_length_local || {};
    const pageNumber = pageResult.page_number;
    if (!graph && localSnapshot) {
      const edgeDimensions = new Map();
      const localVertices = Array.isArray(localSnapshot.base_vertices || localSnapshot.vertices) ? (localSnapshot.base_vertices || localSnapshot.vertices) : [];
      const fallbackNodeId = function (localId, point) {
        if (String(localId || '').startsWith('V')) return String(localId);
        if (!Array.isArray(point) || point.length < 2) return String(localId || '');
        let best = null;
        localVertices.forEach(function (vertex) {
          const dx = Number(vertex.x) - Number(point[0]);
          const dy = Number(vertex.y) - Number(point[1]);
          const distance = Math.hypot(dx, dy);
          if (Number.isFinite(distance) && (!best || distance < best.distance)) best = { id: String(vertex.id), distance: distance, vertex: vertex };
        });
        return best && best.distance <= 10 ? best.id : String(localId || '');
      };
      const ensureFallbackNode = function (localId, point) {
        if (!localId) return '';
        const id = String(pageNumber) + ':' + String(localId);
        if (nodes.has(id)) return id;
        const sourceVertex = localVertices.find(function (vertex) { return String(vertex.id) === String(localId); });
        const base = sourceVertex || {};
        nodes.set(id, applyProviderCoordinates(Object.assign({}, base, {
          id: id,
          local_vertex_id: String(localId),
          page: pageNumber,
          pages: [pageNumber],
          x: base.x ?? (Array.isArray(point) ? point[0] : null),
          y: base.y ?? (Array.isArray(point) ? point[1] : null),
          z: base.z ?? 0,
          role: base.role || 'edge_endpoint',
        }), pageNumber, localId));
        return id;
      };
      (Array.isArray(localSnapshot.dimensions) ? localSnapshot.dimensions : []).forEach(function (dimension) {
        const edgeId = String(dimension.edge_id || '');
        if (!edgeId) return;
        const decision = dimension.local_filter_decision || ((dimension.local_decision || {}).decision);
        if (decision === 'include' && dimension.value != null) edgeDimensions.set(edgeId, dimension.value);
      });
      localVertices.forEach(function (vertex, index) {
        const localId = String(vertex.id || ('V' + (index + 1)));
        const id = String(pageNumber) + ':' + localId;
        if (!nodes.has(id)) {
          nodes.set(id, applyProviderCoordinates(Object.assign({}, vertex, {
            id: id,
            local_vertex_id: localId,
            page: pageNumber,
            pages: [pageNumber],
            z: vertex.z ?? 0,
          }), pageNumber, localId));
        }
      });
      (Array.isArray(localSnapshot.base_edges || localSnapshot.edges) ? (localSnapshot.base_edges || localSnapshot.edges) : []).forEach(function (edge, index) {
        const fromLocal = fallbackNodeId(edge.from_vertex || edge.from_node_id || edge.from || edge.start_vertex, edge.start);
        const toLocal = fallbackNodeId(edge.to_vertex || edge.to_node_id || edge.to || edge.end_vertex, edge.end);
        if (!fromLocal || !toLocal) return;
        const from = ensureFallbackNode(fromLocal, edge.start);
        const to = ensureFallbackNode(toLocal, edge.end);
        if (!from || !to || from === to) return;
        const key = from + '->' + to;
        if (edges.has(key)) {
          duplicates.push({ from_node_id: from, to_node_id: to, page: pageNumber });
          return;
        }
        const edgeId = edge.id || ('E' + (edges.size + 1));
        edges.set(key, Object.assign({}, edge, {
          id: String(pageNumber) + ':' + edgeId,
          from_node_id: from,
          to_node_id: to,
          page: pageNumber,
          dimension_mm: edge.length_mm ?? edge.value ?? edgeDimensions.get(String(edgeId)),
        }));
      });
      return;
    }
    if (!graph) return;
    ((analysis.main_chain && analysis.main_chain.segments) || []).forEach(rememberDimension);
    (analysis.branches || []).forEach(function (branch) {
      (branch.segments || []).forEach(rememberDimension);
    });
    (Array.isArray(graph.nodes) ? graph.nodes : []).forEach(function (node, index) {
      const id = String(node.id || ('P' + pageResult.page_number + '-N' + index));
      const withCoordinates = applyProviderCoordinates(Object.assign({}, node), pageResult.page_number, id);
      if (nodes.has(id)) {
        const existing = nodes.get(id);
        if (existing.x == null && withCoordinates.x != null) Object.assign(existing, withCoordinates);
        existing.pages = Array.from(new Set((existing.pages || []).concat(pageResult.page_number)));
      } else {
        nodes.set(id, Object.assign({}, withCoordinates, { id: id, page: pageResult.page_number, pages: [pageResult.page_number] }));
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
  const values = [node.x, node.y, node.z == null || node.z === '' ? 0 : node.z].map(Number);
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

    const pageColors = [0x2563eb, 0xdc2626, 0x16a34a, 0x9333ea, 0xea580c, 0x0891b2, 0xca8a04];
    const pages = Array.from(new Set(graph.nodes.map(function (node) { return String(node.page || ''); }).filter(Boolean)));
    const multiplePages = pages.length > 1;
    const pageColor = function (page) {
      const index = Math.max(0, pages.indexOf(String(page || '')));
      return pageColors[index % pageColors.length];
    };
    const nodeLabel = function (node) {
      const localId = String(node.local_vertex_id || node.id || '').split(':').pop();
      return multiplePages && node.page ? localId + '_' + node.page : localId;
    };
    if (multiplePages) {
      const legend = document.createElement('span');
      legend.className = 'graph-3d-page-legend';
      legend.innerHTML = pages.map(function (page) {
        return '<span><i style="background:#' + pageColor(page).toString(16).padStart(6, '0') + '"></i>лист ' + esc(page) + '</span>';
      }).join('');
      toolbar.insertBefore(legend, toolbar.firstChild);
    }

    const numeric = graph.nodes.map(function (node) { return [node.x, node.y, node.z == null || node.z === '' ? 0 : node.z].map(Number); }).filter(function (values) { return values.every(Number.isFinite); });
    const min = [0, 0, 0].map(function (_, axis) { return numeric.length ? Math.min.apply(null, numeric.map(function (v) { return v[axis]; })) : 0; });
    const max = [0, 0, 0].map(function (_, axis) { return numeric.length ? Math.max.apply(null, numeric.map(function (v) { return v[axis]; })) : 1; });
    const bounds = { min: min, span: max.map(function (value, axis) { return Math.max(value - min[axis], 1); }), count: graph.nodes.length };
    const positions = new Map();
    graph.nodes.forEach(function (node, index) { positions.set(String(node.id), graphPoint(node, index, THREE, bounds)); });

    const edgePositionsByPage = new Map();
    graph.edges.forEach(function (edge) {
      const from = positions.get(String(edge.from_node_id));
      const to = positions.get(String(edge.to_node_id));
      const fromNode = graph.nodes.find(function (node) { return String(node.id) === String(edge.from_node_id); });
      const toNode = graph.nodes.find(function (node) { return String(node.id) === String(edge.to_node_id); });
      // Do not draw a misleading 3D segment through an unresolved vertex.
      if (from && to && fromNode && toNode && !fromNode.coordinate_unknown && !toNode.coordinate_unknown) {
        const edgePage = String(edge.page || fromNode.page || '');
        if (!edgePositionsByPage.has(edgePage)) edgePositionsByPage.set(edgePage, []);
        edgePositionsByPage.get(edgePage).push(from.x, from.y, from.z, to.x, to.y, to.z);
        const direction = to.clone().sub(from);
        const length = direction.length();
        if (length > 0.001) {
          scene.add(new THREE.ArrowHelper(direction.normalize(), from, length, pageColor(edgePage), 0.28, 0.16));
        }
        if (edge.dimension_mm != null && edge.dimension_mm !== '') {
          const label = floatingLabel(String(edge.dimension_mm) + ' мм', '#' + pageColor(edgePage).toString(16).padStart(6, '0'), THREE);
          label.position.copy(from).add(to).multiplyScalar(0.5);
          label.position.y += 0.3;
          scene.add(label);
        }
      }
    });
    edgePositionsByPage.forEach(function (edgePositions, edgePage) {
      const geometry = new THREE.BufferGeometry();
      geometry.setAttribute('position', new THREE.Float32BufferAttribute(edgePositions, 3));
      scene.add(new THREE.LineSegments(geometry, new THREE.LineBasicMaterial({ color: pageColor(edgePage) })));
    });
    const nodeGeometry = new THREE.SphereGeometry(0.16, 18, 12);
    const unknownGeometry = new THREE.SphereGeometry(0.195, 18, 12);
    const unknownMaterial = new THREE.MeshStandardMaterial({ color: 0xf59e0b, roughness: 0.5, emissive: 0x7c2d12, emissiveIntensity: 0.4 });
    let unknownCount = 0;
    let uncertainCount = 0;
    graph.nodes.forEach(function (node) {
      const isUnknown = !!node.coordinate_unknown;
      if (isUnknown) unknownCount += 1;
      if (node.coordinate_uncertain) uncertainCount += 1;
      const nodeMaterial = new THREE.MeshStandardMaterial({ color: pageColor(node.page), roughness: 0.55 });
      const mesh = new THREE.Mesh(isUnknown ? unknownGeometry : nodeGeometry, isUnknown ? unknownMaterial : nodeMaterial);
      mesh.position.copy(positions.get(String(node.id)));
      mesh.userData.label = String(node.id);
      mesh.userData.coordinate_unknown = isUnknown;
      scene.add(mesh);
      if (nodeLabel(node)) {
        const label = floatingLabel(nodeLabel(node), '#' + pageColor(node.page).toString(16).padStart(6, '0'), THREE);
        label.position.copy(mesh.position);
        label.position.y += 0.34;
        label.scale.set(0.9, 0.22, 1);
        scene.add(label);
      }
      if (node.coordinate_uncertain && !isUnknown) {
        const marker = floatingLabel('?', '#b45309', THREE);
        marker.position.copy(mesh.position);
        marker.position.y += 0.42;
        marker.scale.set(0.45, 0.45, 1);
        scene.add(marker);
      }
    });
    if (unknownCount || uncertainCount) {
      const note = document.createElement('span');
      note.className = 'graph-3d-unknown-note';
      note.textContent = 'Без координат: ' + unknownCount + ' · требуют проверки confidence: ' + uncertainCount;
      toolbar.insertBefore(note, toolbar.firstChild);
    }
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
    const overviewFile = files['local_markup_overview_pdf'];
    const imageMarkup = overviewFile
      ? '<details class="trace-section" open><summary>Локальная разметка (изображение)</summary><div class="trace-content"><img class="viewer-frame" src="' + viewerImageUrl(runId, lineId, overviewFile) + '" alt="Локальная разметка листа ' + esc(pageNumber) + '"></div></details>'
      : '';
    wrap.innerHTML = renderTrace(state.tracesByLine[key], imageMarkup);
    bindTraceControls(card, wrap);
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
  if (!filename) {
    wrap.innerHTML = '<p class="muted">Для этой вкладки файл не создан в текущем прогоне.</p>';
    return;
  }
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
      wrap.innerHTML = '<button type="button" class="copy-text-button numbers-copy">Копировать исходный TXT</button>' + table(sections.numbers, 'Числа') + table(sections.coordinates, 'Координаты') + table(sections.vertices, 'Вершины');
      wrap.querySelector('.numbers-copy').addEventListener('click', function () { copyViewerText(this, text); });
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
      const pretty = JSON.stringify(data, null, 2);
      wrap.innerHTML = copyableBlock(pretty);
      wrap.querySelector('.copy-text-button').addEventListener('click', function () { copyViewerText(this, pretty); });
    } catch (error) { wrap.innerHTML = '<div class="badge error">Ошибка: ' + esc(error.message) + '</div>'; }
    return;
  }
  if (tabKey === 'dimension_review_json') {
    wrap.innerHTML = '<span class="spinner"></span> Читаю решение провайдера...';
    try {
      const response = await fetch(viewerImageUrl(runId, lineId, filename));
      const data = await response.json();
      const pretty = JSON.stringify(data, null, 2);
      wrap.innerHTML = copyableBlock(pretty);
      wrap.querySelector('.copy-text-button').addEventListener('click', function () { copyViewerText(this, pretty); });
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
    const legacyDimensionMap = analysis.dimension_map || {};
    const mappingHasFinalGroups = Array.isArray(mapping.final_edge_candidate_groups)
      || Array.isArray(mapping.edge_candidate_groups)
      || !!mapping.final_assignment_summary;
    const finalMapping = mappingHasFinalGroups ? mapping : legacyDimensionMap;
    const dimensions = mapping.dimensions || [];
    const handwheels = mapping.handwheels || [];
    const review = analysis.dimension_review;
    const providerAvailable = !!(review && review.answer && Array.isArray(review.answer.candidate_decisions));
    const manualEditing = state.manualEdit && providerAvailable;
    const manualAdjustments = analysis.manual_adjustments || {};
    const localCandidates = dimensions.map(function (item, index) {
      return {
        id: item.id || ('D' + (index + 1)),
        text: item.text || '—',
        kind: item.kind || item.status || 'dimension',
        status: item.status || 'mapped',
        hints: Array.isArray(item.hints) ? item.hints : [],
        edge_id: item.edge_id || '—',
        selected_edge_id: item.selected_edge_id || item.final_interval_id || '',
        final_interval_id: item.final_interval_id || '',
        final_interval_status: item.final_interval_status || '',
        final_interval_reason: item.final_interval_reason || '',
        final_from_vertex: item.final_from_vertex || '',
        final_to_vertex: item.final_to_vertex || '',
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
      ? '<details class="notes collapsible-result"><summary>Локальные кандидаты (' + localCandidates.length + ')</summary><table class="data-table"><thead><tr><th>ID</th><th>Текст</th><th>Ключ</th><th>Статус</th><th>Ребро</th></tr></thead><tbody>'
        + localCandidates.map(function (item) {
          return '<tr class="candidate-' + esc(item.status) + '"><td>' + esc(item.id) + '</td><td>' + esc(item.text) + '</td><td>' + esc((item.hints || []).join(', ') || item.kind) + '</td><td>' + esc(item.status) + '</td><td>' + esc(item.edge_id || '—') + '</td></tr>';
        }).join('') + '</tbody></table></details>'
      : '';
    const finalEdgeGroups = Array.isArray(finalMapping.final_edge_candidate_groups)
      ? finalMapping.final_edge_candidate_groups
      : (Array.isArray(finalMapping.edge_candidate_groups) ? finalMapping.edge_candidate_groups : []);
    const unresolvedFinalCandidates = Array.isArray(finalMapping.unresolved_final_candidates)
      ? finalMapping.unresolved_final_candidates
      : [];
    const finalSummary = finalMapping.final_assignment_summary || {};
    const finalEdgeGroupsBlock = finalEdgeGroups.length
      ? '<details class="notes collapsible-result" open><summary>Карта финальных отрезков (' + esc(finalSummary.groups_with_candidates ?? finalEdgeGroups.length) + ' с размерами / всего ' + esc(finalSummary.final_edge_count ?? finalEdgeGroups.length) + ')</summary><table class="data-table"><thead><tr><th>Финальный отрезок</th><th>Тип</th><th>Размеры</th><th>Локально</th><th>Доказательство</th></tr></thead><tbody>'
        + finalEdgeGroups.map(function (group) {
          const from = group.from_vertex || '?';
          const to = group.to_vertex || '?';
          const candidates = Array.isArray(group.candidates) ? group.candidates : [];
          const values = candidates.length
            ? candidates.map(function (candidate) {
                return (candidate.candidate_id || 'D?') + '=' + (candidate.value_mm ?? candidate.text ?? '—');
              }).join(', ')
            : '—';
          const statuses = candidates.length
            ? candidates.map(function (candidate) {
                return (candidate.local_decision || candidate.status || '—');
              }).join(', ')
            : '—';
          const proof = candidates.length
            ? candidates.map(function (candidate) {
                const origin = candidate.final_ray_origin_kind ? 'луч: ' + candidate.final_ray_origin_kind : '';
                const reason = candidate.final_interval_reason || '';
                return [origin, reason].filter(Boolean).join(' · ');
              }).join(' | ')
            : 'нет кандидатов';
          const kind = group.is_handwheel_segment ? 'valve / штурвал' : 'труба';
          return '<tr><td><b>' + esc(from + ' — ' + to) + '</b><br><span class="muted">' + esc(group.edge_id || (group.edge_ids || []).join(', ') || '—') + '</span></td><td>' + esc(kind) + '</td><td>' + esc(values) + '</td><td>' + esc(statuses) + '</td><td class="candidate-reason">' + esc(proof) + '</td></tr>';
        }).join('') + '</tbody></table></details>'
      : '';
    const unresolvedFinalBlock = unresolvedFinalCandidates.length
      ? '<details class="notes collapsible-result"><summary>Не привязаны к финальному отрезку (' + unresolvedFinalCandidates.length + ')</summary><table class="data-table"><thead><tr><th>ID</th><th>Размер</th><th>Статус</th><th>Причина</th></tr></thead><tbody>'
        + unresolvedFinalCandidates.map(function (item) {
          return '<tr><td><b>' + esc(item.candidate_id || 'D?') + '</b></td><td>' + esc(item.value_mm ?? item.text ?? '—') + '</td><td>' + esc(item.final_interval_status || item.status || 'unresolved') + '</td><td class="candidate-reason">' + esc(item.final_interval_reason || '—') + '</td></tr>';
        }).join('') + '</tbody></table></details>'
      : '';
    const handwheelRows = handwheels.length ? handwheels : localHandwheels.map(function (item) {
      return { id: item.id, label: item.text, arrow_found: false, edge_id: item.edge_id || null };
    });
    const handwheelBlock = handwheelRows.length
      ? '<details class="notes collapsible-result"><summary>Штурвалы / рукоятки / маховики (' + handwheelRows.length + ')</summary><table class="data-table"><thead><tr><th>ID</th><th>Текст</th><th>Статус</th><th>Ребро</th></tr></thead><tbody>'
        + handwheelRows.map(function (item) {
          const arrow = item.arrow_found ? 'найдена' : 'не найдена';
          const edge = item.edge_id || 'не определено';
          const edgeNote = item.edge_created ? ' (создано для штурвала)' : '';
          return '<tr class="candidate-handwheel"><td>' + esc(item.id) + '</td><td>' + esc(item.label || item.text || 'ШТУРВАЛ') + '</td><td>стрелка: ' + arrow + '</td><td>' + esc(edge + edgeNote) + '</td></tr>';
        }).join('') + '</tbody></table></details>'
      : '';
    const providerTable = '<table class="data-table provider-result-table"><thead><tr><th>Размер</th><th>Отрезок</th><th>Статус</th><th>Пояснение модели</th></tr></thead><tbody>'
      + (dimensions.map(function (item) {
        const reviewDecision = review && review.answer && (review.answer.candidate_decisions || []).find(function (row) { return row.candidate_id === item.id; });
        const status = reviewDecision ? reviewDecision.decision : item.status;
        const manuallyChanged = !!manualAdjustments[item.id];
        const statusControl = manualEditing
          ? '<select class="manual-status-select" data-line="' + esc(lineId) + '" data-page="' + pr.page_number + '" data-candidate="' + esc(item.id) + '">'
            + ['include', 'exclude', 'ambiguous'].map(function (option) { return '<option value="' + option + '"' + (option === status ? ' selected' : '') + '>' + option + '</option>'; }).join('')
            + '</select>'
          : esc(status);
        const feedbackKey = [state.runId, lineId, pr.page_number, item.id].join('|');
        const rating = state.feedback[feedbackKey] || '';
        const feedbackButtons = '<span class="feedback-buttons" data-feedback-key="' + esc(feedbackKey) + '" data-line="' + esc(lineId) + '" data-page="' + pr.page_number + '" data-candidate="' + esc(item.id) + '">'
          + '<button type="button" class="feedback-btn feedback-up' + (rating === 'up' ? ' selected' : '') + '" title="Полезный результат" aria-label="Палец вверх">👍</button>'
          + '<button type="button" class="feedback-btn feedback-down' + (rating === 'down' ? ' selected' : '') + '" title="Неверный результат" aria-label="Палец вниз">👎</button>'
          + '</span> ';
        return '<tr class="candidate-' + esc(status) + '"><td>' + feedbackButtons + (manuallyChanged ? '<span class="manual-change-badge" title="Статус изменён вручную">!</span> ' : '') + esc(item.text) + '</td><td>' + esc(item.edge_id || '—') + '</td><td>' + statusControl + (item.leader_attached ? ' · стрелка' : '') + (item.conflict_with ? ' → ' + esc(item.conflict_with) : '') + '</td><td class="candidate-reason">' + esc(reviewDecision ? reviewDecision.reason : '') + '</td></tr>';
      }).join('') || '<tr><td colspan="4" class="muted">Размеров нет</td></tr>')
      + '</tbody></table>';
    const providerBlock = providerAvailable
      ? '<details class="notes collapsible-result provider-result" data-provider-key="' + esc(lineId + '::' + pr.page_number) + '"><summary>Решение провайдера</summary>'
        + '<div class="manual-edit-toolbar"><button type="button" class="manual-edit-toggle' + (manualEditing ? ' active' : '') + '" data-manual-edit-toggle>'
        + (manualEditing ? 'Завершить ручную правку' : 'Подправить вручную') + '</button>'
        + (manualEditing ? '<span class="muted">Статусы размеров доступны для изменения</span>' : '') + '</div>'
        + reviewBlock + providerTable + '</details>'
      : '<details class="notes collapsible-result provider-result provider-result-disabled" data-provider-key="' + esc(lineId + '::' + pr.page_number) + '"><summary>Решение провайдера · недоступно</summary><p class="muted">Для этого прогона решение провайдера не запускалось.</p></details>';

    return '<h4>Лист ' + pr.page_number + ' — привязка размеров</h4>'
      + '<p class="muted">Рёбер графа: ' + (mapping.edges || []).length + ' · размеров: ' + dimensions.length + '</p>'
      + localCandidatesBlock
      + finalEdgeGroupsBlock
      + unresolvedFinalBlock
      + handwheelBlock
      + providerBlock + downloads + viewer;
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

function bindFeedbackButtons(root) {
  $$('.feedback-buttons', root).forEach(function (container) {
    container.querySelectorAll('.feedback-btn').forEach(function (button) {
      button.addEventListener('click', async function () {
        const rating = button.classList.contains('feedback-up') ? 'up' : 'down';
        const payload = {
          run_id: state.runId,
          line_id: container.dataset.line,
          page_number: Number(container.dataset.page),
          candidate_id: container.dataset.candidate,
          rating: rating,
        };
        button.disabled = true;
        try {
          await api('/api/feedback', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(payload),
          });
          state.feedback[container.dataset.feedbackKey] = rating;
          container.querySelectorAll('.feedback-btn').forEach(function (item) {
            item.classList.toggle('selected', item === button);
          });
          const prompts = await api('/api/prompts');
          promptWorkspace.list = prompts;
          if (!$('#prompt-section').hidden) renderPromptCatalog();
        } catch (error) {
          alert('Не удалось сохранить оценку: ' + error.message);
        } finally {
          button.disabled = false;
        }
      });
    });
  });
}

function bindManualEditing(root) {
  const toggle = $('[data-manual-edit-toggle]', root);
  if (toggle) {
    toggle.addEventListener('click', function () {
      state.manualEdit = !state.manualEdit;
      renderResults(state.currentRun);
    });
  }
  $$('.manual-status-select', root).forEach(function (select) {
    select.addEventListener('change', async function () {
      select.disabled = true;
      try {
        const updated = await api('/api/manual-status', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            run_id: state.runId,
            line_id: select.dataset.line,
            page_number: Number(select.dataset.page),
            candidate_id: select.dataset.candidate,
            status: select.value,
          }),
        });
        state.currentRun = updated;
        await refreshFeedback();
        renderResults(updated);
      } catch (error) {
        alert('Не удалось сохранить ручную правку: ' + error.message);
        select.disabled = false;
      }
    });
  });
}

function renderResults(run) {
  state.runId = run.run_id;
  state.currentRun = run;
  collectMaps(run);
  $('#result-section').hidden = false;
  $('#result-title').textContent = run.run_id;
  $('#export-json').href = '/api/runs/' + encodeURIComponent(run.run_id) + '/export/json';
  $('#export-excel').href = '/api/runs/' + encodeURIComponent(run.run_id) + '/export/excel';
  $('#export-review-data').href = '/api/runs/' + encodeURIComponent(run.run_id) + '/export/review-data';
  const body = $('#result-body');
  const openProviderState = openProviderKeys(body);
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
    const lineProviderTrace = line.provider_trace
      ? '<details class="notes collapsible-result line-provider-result"><summary>Ответ провайдера / AI запрос</summary><div class="line-provider-trace" data-line-provider-trace="' + esc(line.line_id) + '"><p class="muted">Загрузка ответа…</p></div></details>'
      : '';
    return '<div class="line-card">'
      + '<h3>' + esc(line.line_id) + ' <span class="group-item-meta">стр. ' + (line.pages || []).join(', ') + '</span></h3>'
      + (state.graphsByLine[line.line_id]
        ? '<section class="result-3d-block"><div class="panel-title"><span>ТОПОЛОГИЯ / ' + esc(line.line_id) + '</span><h2>3D-граф трубы</h2></div><div class="line-graph-3d" data-graph-3d-line="' + esc(line.line_id) + '"><div class="graph-3d-loading"><span class="spinner"></span> Загружаю 3D-сцену...</div></div></section>'
        : '')
      + (hasDimensionReview
        ? '<div class="kpi">'
          + '<div><span class="kpi-label">Основная линия · чистая / грязная</span><strong>' + reviewCleanMain + ' / ' + reviewDirtyMain + ' мм</strong></div>'
          + '<div><span class="kpi-label">Ответвления · чистая / грязная</span><strong>' + reviewCleanBranch + ' / ' + reviewDirtyBranch + ' мм</strong></div>'
          + '<div><span class="kpi-label">Рёбер</span><strong>' + edgeCount + '</strong></div>'
          + '<div><span class="kpi-label">Листов</span><strong>' + pages.length + '</strong></div>'
          + '</div>'
        : '')
      + lineProviderTrace
      + (line.analysis && line.analysis.pipeline_length ? pipelineLengthSummary(line) + pipelineAdjacentSheetBlock(line) + pipelineVertexCoordinatesBlock(line) + pipelineHandwheelBlock(line) : '')
      + '<div class="page-nav"><span class="page-nav-label">Листы</span><div class="row page-selector-row">' + pageButtons + '</div></div>'
      + '<div class="page-content" data-line="' + esc(line.line_id) + '">'
      + (firstPage
        ? (line.analysis && line.analysis.pipeline_length ? pipelineLengthPageDetail(line, firstPage.page_number) : pageDetailHtml(line.line_id, firstPage))
        : '<p class="muted">Страниц нет</p>')
      + '</div>'
      + '</div>';
  }).join('<hr>');
  $('#result-body').querySelectorAll('[data-line-provider-trace]').forEach(function (wrap) {
    const lineId = wrap.getAttribute('data-line-provider-trace') || '';
    const line = run.lines.find(function (item) { return item.line_id === lineId; });
    if (!line || !line.provider_trace) return;
    const overviewImages = (line.page_results || []).map(function (pr) {
      const file = pr.files && pr.files.local_markup_overview_pdf;
      if (!file) return '';
      return '<figure class="trace-image"><figcaption>Лист ' + esc(pr.page_number) + '</figcaption>'
        + '<img class="viewer-frame" src="' + viewerImageUrl(state.runId, line.line_id, file) + '" alt="Локальная разметка листа ' + esc(pr.page_number) + '"></figure>';
    }).join('');
    const overviewMarkup = overviewImages
      ? '<details class="trace-section" open><summary>Наша локальная разметка (изображение)</summary><div class="trace-content">' + overviewImages + '</div></details>'
      : '';
    wrap.innerHTML = renderTrace(line.provider_trace, overviewMarkup);
    bindTraceControls(wrap.closest('.line-card'), wrap);
  });
  $('#result-body').querySelectorAll('[data-graph-3d-line]').forEach(function (container) {
    const lineId = container.getAttribute('data-graph-3d-line') || '';
    render3DGraph(container, state.graphsByLine[lineId]);
  });
  bindPageSelectors();
  bindPipelineProviderConfirmations();
  $('#result-body').querySelectorAll('.viewer').forEach(bindViewer);
  bindFeedbackButtons($('#result-body'));
  bindManualEditing($('#result-body'));
  restoreProviderKeys(body, openProviderState);
}

function bindPipelineProviderConfirmations() {
  $('#result-body').querySelectorAll('.provider-edit-button').forEach(function (button) {
    button.addEventListener('click', function () {
      state.providerDecisionEdit.add(button.dataset.providerEdit || '');
      renderResults(state.currentRun);
    });
  });
  $('#result-body').querySelectorAll('.provider-confirm-button').forEach(function (button) {
    button.addEventListener('click', async function () {
      const lineCard = button.closest('.line-card');
      const heading = lineCard && lineCard.querySelector('h3');
      const lineId = button.dataset.providerLine || (heading ? heading.textContent.trim().split(/\s+/)[0] : '');
      button.disabled = true;
      try {
        const updated = await api('/api/pipeline-length/confirm', {
          method: 'POST',
          headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({
            run_id: state.runId,
            line_id: lineId,
            candidate_ids: button.dataset.providerAction === 'accept' ? [button.dataset.providerCandidate] : [],
            keep_local_candidate_ids: button.dataset.providerAction === 'keep_local' ? [button.dataset.providerCandidate] : [],
            intermediate_distance_indices: button.dataset.providerAction === 'accept_distance' ? [Number(button.dataset.providerDistanceIndex)] : [],
            edge_ids: [],
          }),
        });
        if (button.dataset.providerCandidate) state.providerDecisionEdit.delete([lineId, button.dataset.providerCandidate].join('|'));
        state.currentRun = updated;
        renderResults(updated);
      } catch (error) {
        button.disabled = false;
        alert('Не удалось принять решение провайдера: ' + error.message);
      }
    });
  });
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
        content.innerHTML = line.analysis && line.analysis.pipeline_length
          ? pipelineLengthPageDetail(line, pageNumber)
          : pageDetailHtml(lineId, pr);
        content.querySelectorAll('.viewer').forEach(bindViewer);
        bindFeedbackButtons(content);
        bindManualEditing(content);
        bindPipelineProviderConfirmations();
      }
    });
  });
}

function openProviderKeys(root) {
  const keys = new Set();
  root.querySelectorAll('details[data-provider-key][open]').forEach(function (details) {
    keys.add(details.dataset.providerKey);
  });
  return keys;
}

function restoreProviderKeys(root, keys) {
  root.querySelectorAll('details[data-provider-key]').forEach(function (details) {
    details.open = keys.has(details.dataset.providerKey);
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
      const manual = Number(run.manual_adjustment_count || 0) ? ' · ручных правок: ' + run.manual_adjustment_count : '';
      const providerTime = run.provider_elapsed_seconds != null ? formatSeconds(run.provider_elapsed_seconds) : '—';
      const providerTokens = run.provider_total_tokens != null ? String(run.provider_total_tokens) : '—';
      const providerFixes = (Number(run.provider_fixes || 0) || Number(run.local_kept || 0))
        ? ' · решения: +' + Number(run.provider_fixes || 0) + ' / −' + Number(run.local_kept || 0)
        : '';
      const excludedPages = Array.isArray(run.excluded_pages) && run.excluded_pages.length
        ? ' · исключены листы: ' + run.excluded_pages.join(', ')
        : '';
      const feedback = run.feedback || {};
      const feedbackStats = Number(feedback.total || 0)
        ? ' · <span class="prompt-feedback prompt-feedback-up">↑ ' + Number(feedback.positive || 0) + '</span>'
          + ' <span class="prompt-feedback prompt-feedback-down">↓ ' + Number(feedback.negative || 0) + '</span>'
        : '';
      return '<div class="line-card history-row" data-run-id="' + esc(run.run_id) + '">'
        + '<div class="row"><b>' + esc(run.source_name || '') + '</b> <span class="badge ' + badge + '">' + esc(run.status) + '</span>'
        + '<span class="badge kind">' + esc(kindLabel) + '</span>'
        + '<span class="group-item-meta">' + esc(when) + '</span></div>'
        + '<div class="history-metrics">'
        + '<span><small>Линии</small><b>' + esc((run.line_ids || []).join(', ') || '—') + '</b></span>'
        + '<span><small>Провайдер</small><b>' + esc((run.provider || '—') + (run.model ? ' · ' + run.model : '')) + '</b></span>'
        + '<span class="history-provider-time"><small>Запрос провайдера</small><b>' + esc(providerTime) + '</b></span>'
        + '<span><small>Токены</small><b>' + esc(providerTokens) + '</b></span>'
        + '<span><small>Решения</small><b>+' + esc(Number(run.provider_fixes || 0)) + ' / −' + esc(Number(run.local_kept || 0)) + '</b></span>'
        + '</div>'
        + '<div class="muted">' + esc(run.run_id) + esc(manual) + esc(providerFixes) + esc(excludedPages) + feedbackStats + '</div>'
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
      state.pollToken += 1;
      pollRun(state.pollToken);
    } else {
      await refreshFeedback();
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
    const menu = $('#provider-menu');
    if (menu && !menu.contains(event.target)) {
      $('#provider-popover').hidden = true;
      $('#provider-toggle').setAttribute('aria-expanded', 'false');
    }
  });
  $('#pdf-file').addEventListener('change', function () {
    const file = $('#pdf-file').files[0];
    if (file) $('#document-summary').textContent = 'Файл выбран: ' + file.name;
  });
  $('#load-pdf').addEventListener('click', loadPdf);
  $('#start-analysis').addEventListener('click', startAnalysis);
  $('#provider-toggle').addEventListener('click', function (event) {
    event.stopPropagation();
    const popover = $('#provider-popover');
    popover.hidden = !popover.hidden;
    $('#provider-toggle').setAttribute('aria-expanded', String(!popover.hidden));
    updateProviderConnectionStatus();
  });
  $('#provider-popover').addEventListener('click', function (event) {
    event.stopPropagation();
  });
  $('#provider-select').addEventListener('change', function () {
    state.provider = $('#provider-select').value;
    $('#codex-login-panel').hidden = state.provider !== 'codex_cli';
    $('#codex-login-command').hidden = true;
    updateProviderConnectionStatus();
    if (state.provider === 'codex_cli') refreshCodexLogin();
  });
  $('#codex-login-check').addEventListener('click', refreshCodexLogin);
  $('#codex-login-start').addEventListener('click', startCodexLogin);
  $('#history-button').addEventListener('click', showHistory);
  $('#prompts-button').addEventListener('click', showPrompts);
  $('#eval-button').addEventListener('click', showEval);
  $('#save-prompt').addEventListener('click', saveCurrentPrompt);
  $('#groups-search').addEventListener('input', function (event) {
    const query = String(event.currentTarget.value).trim().toUpperCase().replace(/-/g, '_');
    $$('#groups-list .group-item').forEach(function (row) {
      const name = (row.querySelector('.name')?.textContent ?? '').toUpperCase();
      row.style.display = (!query || name.includes(query)) ? '' : 'none';
    });
  });
  init();
});
