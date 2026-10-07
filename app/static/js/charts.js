/* Корректор — разделы «Аналитика» и «Тренды».
   Графики строятся на Chart.js (локальная копия). Правила оформления:
   цвет закреплен за сущностью (тип, источник) и не зависит от порядка; статусы — только со значком и подписью;
   одна ось Y на график; тонкие линии 2 px; у каждого графика есть табличный вид и выгрузка. */

/* ---------- палитра ---------- */
const DARK = document.documentElement.classList.contains('dark');
const ROLE = document.body.dataset.role || 'admin';      // роль пользователя: кнопки действий показываются по правам
/* категориальная палитра: свои ступени для светлой и темной темы (проверены валидатором, обе — PASS) */
const PAL = DARK ? ['#3987e5', '#d95926', '#199e70', '#c98500', '#d55181', '#008300', '#9085e9', '#e66767']
  : ['#2a78d6', '#eb6834', '#1baf7a', '#eda100', '#e87ba4', '#008300', '#4a3aa7', '#e34948'];
const OTHER = DARK ? '#6b7686' : '#9aa3ad';
const TYPES_ORDER = ['Орфография', 'Пунктуация', 'Грамматика', 'Стилистика', 'Типографика', 'Единообразие', 'Логика',
  'Требует проверки автором'];
const TYPE_COLOR = Object.fromEntries(TYPES_ORDER.map((t, i) => [t, PAL[i]]));
const SRC_ORDER = ['typo', 'codes', 'house', 'data', 'spell', 'lt', 'ai', 'batch'];
const SRC_COLOR = Object.fromEntries(SRC_ORDER.map((t, i) => [t, PAL[i]]));
const STATUS = {accepted: {c: '#0ca30c', i: '✓', l: 'Принято'}, rejected: {c: '#d03b3b', i: '✕', l: 'Отклонено'},
  new: {c: OTHER, i: '○', l: 'Не просмотрено'}};
const SEQ = ['#cde2fb', '#b7d3f6', '#9ec5f4', '#86b6ef', '#6da7ec', '#5598e7', '#3987e5', '#2a78d6', '#256abf',
  '#1c5cab', '#184f95', '#104281', '#0d366b'];
const INK = DARK ? '#e6eaf0' : '#1d2433', MUTED = DARK ? '#9aa6b6' : '#6b7280', GRID = DARK ? '#2c3545' : '#e9edf2',
  TREND = DARK ? '#8a96a8' : '#7b8594', SURFACE = DARK ? '#1b2230' : '#ffffff';
const srcColor = k => SRC_COLOR[k] || OTHER;

/* ---------- форматирование ---------- */
const nf = (v, d) => v === null || v === undefined || Number.isNaN(v) ? '—'
  : Number(v).toLocaleString('ru-RU', {maximumFractionDigits: d === undefined ? 1 : d});
const short = (s, n) => (s = String(s || '')).length > n ? s.slice(0, n - 1) + '…' : s;
function seqColor(v, max) {
  if (!v) return DARK ? '#232c3b' : '#f4f6f9';
  const i = Math.min(SEQ.length - 1, Math.floor((v / (max || 1)) * (SEQ.length - 1)));
  return SEQ[i];
}
const seqInk = (v, max) => v && v / (max || 1) > 0.5 ? '#fff' : (DARK && v ? '#0b1019' : INK);

/* ---------- Chart.js: общие настройки ---------- */
if (window.Chart) {
  Chart.defaults.font.family = 'Roboto, "Segoe UI", Arial, sans-serif';
  Chart.defaults.font.size = 12;
  Chart.defaults.color = MUTED;
  Chart.defaults.borderColor = GRID;
  Chart.defaults.maintainAspectRatio = false;
  Chart.defaults.animation.duration = 300;
  Chart.defaults.plugins.legend.labels.boxWidth = 12;
  Chart.defaults.plugins.legend.labels.boxHeight = 12;
  Chart.defaults.plugins.legend.labels.color = INK;
  Chart.defaults.plugins.tooltip.backgroundColor = DARK ? '#0b1019' : '#0e1a2b';
  Chart.defaults.plugins.tooltip.padding = 10;
  Chart.defaults.plugins.tooltip.cornerRadius = 4;
  Chart.defaults.elements.bar.borderRadius = 4;
  Chart.defaults.elements.bar.borderSkipped = 'start';
  Chart.defaults.elements.line.borderWidth = 2;
  Chart.defaults.elements.line.tension = 0.25;
  Chart.defaults.elements.point.hitRadius = 10;
  Chart.defaults.elements.point.hoverRadius = 6;
  // белый фон для выгрузки PNG
  Chart.register({id: 'bgWhite', beforeDraw(c) {
    const x = c.ctx; x.save(); x.globalCompositeOperation = 'destination-over'; x.fillStyle = '#fff';
    x.fillStyle = SURFACE; x.fillRect(0, 0, c.width, c.height); x.restore();
  }});
  // подписи значений на конце столбцов (только у столбчатых графиков с опцией endLabels)
  Chart.register({id: 'endLabels', afterDatasetsDraw(c, _, opts) {
    if (!opts || !opts.on) return;
    const x = c.ctx; x.save(); x.fillStyle = INK; x.font = '12px Roboto, Arial'; x.textBaseline = 'middle';
    const horiz = c.options.indexAxis === 'y';
    c.data.datasets.forEach((ds, di) => {
      const meta = c.getDatasetMeta(di); if (meta.hidden) return;
      meta.data.forEach((el, i) => {
        const v = ds.data[i]; if (!v) return;
        const t = (opts.fmt || nf)(v, i);
        if (horiz) { x.textAlign = 'left'; x.fillText(t, el.x + 6, el.y); }
        else { x.textAlign = 'center'; x.fillText(t, el.x, el.y - 9); }
      });
    });
    x.restore();
  }});
  // опорная линия (среднее) — сплошная тонкая
  Chart.register({id: 'refLine', afterDatasetsDraw(c, _, opts) {
    const lines = (opts && opts.lines) || (opts && opts.value !== undefined && opts.value !== null ? [opts] : []);
    const y = c.scales.y, xs = c.scales.x; if (!y || !lines.length) return;
    const x = c.ctx; x.save();
    lines.forEach((ln, i) => {
      if (ln.value === undefined || ln.value === null || ln.value > y.max || ln.value < y.min) return;
      const py = y.getPixelForValue(ln.value);
      x.strokeStyle = ln.color || INK; x.lineWidth = ln.width || 1; x.beginPath(); x.moveTo(xs.left, py); x.lineTo(xs.right, py); x.stroke();
      x.fillStyle = ln.color || INK; x.font = '12px Roboto, Arial'; x.textAlign = i % 2 ? 'left' : 'right'; x.textBaseline = 'bottom';
      x.fillText(ln.label, i % 2 ? xs.left + 4 : xs.right - 4, py - 3);
    });
    x.restore();
  }});
}

/* ---------- карточки графиков ---------- */
const CH = {};

function cardEl(id) { return document.getElementById('c-' + id); }

function setData(id, head, rows) {
  CH[id] = Object.assign(CH[id] || {}, {head, rows});
  const el = cardEl(id); if (!el) return;
  $('.ch-empty', el).hidden = rows.length > 0;
  const body = $('.ch-body', el); if (body) body.style.display = rows.length && !CH[id].tableView ? '' : 'none';
  if (CH[id].tableView || el.dataset.kind === 'html') renderTable(id);
}

function renderTable(id) {
  const el = cardEl(id), c = CH[id]; if (!el || !c) return;
  if (el.dataset.kind === 'html') return;          // для HTML-карточек таблица — это и есть содержимое
  const box = $('.ch-table', el);
  box.innerHTML = '<div class="tscroll"><table class="t compact"><thead><tr>' + c.head.map(h => `<th>${esc(h)}</th>`).join('')
    + '</tr></thead><tbody>' + c.rows.map(r => '<tr>' + r.map(v => `<td>${typeof v === 'number' ? nf(v, 2) : esc(v ?? '—')}</td>`).join('')
      + '</tr>').join('') + '</tbody></table></div>';
}

function mkChart(id, cfg, head, rows) {
  const el = cardEl(id); if (!el) return;
  if (CH[id] && CH[id].chart) CH[id].chart.destroy();
  cfg.options = cfg.options || {};
  cfg.options.plugins = Object.assign({legend: {display: false}}, cfg.options.plugins || {});
  const chart = new Chart($('canvas', el), cfg);
  CH[id] = Object.assign(CH[id] || {}, {chart});
  setData(id, head, rows);
}

function setHtml(id, html, head, rows) {
  const el = cardEl(id); if (!el) return;
  $('.ch-table', el).innerHTML = html;
  CH[id] = Object.assign(CH[id] || {}, {head, rows});
  $('.ch-empty', el).hidden = rows.length > 0;
  $('.ch-table', el).hidden = !rows.length;
}

function downloadBlob(name, blob) {
  const a = document.createElement('a'); a.href = URL.createObjectURL(blob); a.download = name;
  document.body.appendChild(a); a.click(); setTimeout(() => { URL.revokeObjectURL(a.href); a.remove(); }, 500);
}

function toCsv(head, rows) {
  const q = v => { v = v === null || v === undefined ? '' : String(v); return /[;"\n]/.test(v) ? '"' + v.replace(/"/g, '""') + '"' : v; };
  return '﻿' + [head].concat(rows).map(r => r.map(q).join(';')).join('\r\n');
}

function initCards() {
  document.addEventListener('click', e => {
    const b = e.target.closest('.ch-tools button'); if (!b) return;
    const card = b.closest('.ch-card'); if (!card) return;
    const id = card.dataset.id, c = CH[id] || {};
    const title = $('h3', card).textContent.trim();
    if (b.dataset.view) {
      c.tableView = b.dataset.view === 'table';
      $$('[data-view]', card).forEach(x => x.classList.toggle('on', x === b));
      $('.ch-table', card).hidden = !c.tableView;
      const body = $('.ch-body', card); if (body) body.style.display = c.tableView ? 'none' : '';
      if (c.tableView) renderTable(id);
      CH[id] = c;
    } else if (b.dataset.act === 'png' && c.chart) {
      const a = document.createElement('a'); a.href = c.chart.toBase64Image('image/png', 1);
      a.download = title.replace(/[^\wа-яё]+/gi, '_') + '.png'; a.click();
    } else if (b.dataset.act === 'csv' && c.head) {
      downloadBlob(title.replace(/[^\wа-яё]+/gi, '_') + '.csv', new Blob([toCsv(c.head, c.rows)], {type: 'text/csv'}));
    } else if (b.dataset.act === 'help') {
      card.classList.toggle('help-on');
    }
  });
  document.addEventListener('click', e => {
    const m = e.target.closest('[data-menu]');
    $$('.menu').forEach(x => { if (!m || x !== m.parentElement) x.classList.remove('open'); });
    if (m) m.parentElement.classList.toggle('open');
  });
  const cl = $('#copylink');
  if (cl) cl.addEventListener('click', async () => {
    try { await navigator.clipboard.writeText(location.href); cl.textContent = 'Скопировано ✓'; }
    catch (e) { window.prompt('Ссылка на страницу с фильтрами:', location.href); }
    setTimeout(() => { cl.textContent = 'Ссылка'; }, 1600);
  });
}

/* ---------- фильтры (хранятся в адресной строке) ---------- */
const F = {};
const FILTER_KEYS = ['gran', 'period', 'from', 'to', 'q', 'family', 'batch', 'author', 'department', 'doc_type', 'type', 'source', 'status', 'rule', 'job'];
const CHIP_NAMES = {q: 'Документ', family: 'Семейство', batch: 'Пакет', author: 'Автор', department: 'Отдел', doc_type: 'Вид документа', type: 'Тип', source: 'Источник', status: 'Статус',
  rule: 'Правило', job: 'Задание'};
let FILTER_OPTS = null, onFilterChange = null, LABELS = {source: {}, status: {}, rule: {}, job: {}, batch: {}};

function readUrl(defaults) {
  const p = new URLSearchParams(location.search);
  FILTER_KEYS.forEach(k => { F[k] = p.get(k) || defaults[k] || ''; });
}
function qs(extra) {
  const p = new URLSearchParams();
  Object.entries(Object.assign({}, F, extra || {})).forEach(([k, v]) => { if (v) p.set(k, v); });
  return p.toString();
}
function writeUrl() { history.replaceState(null, '', location.pathname + (qs() ? '?' + qs() : '')); }

function setFilter(k, v, label) {
  F[k] = F[k] === v ? '' : v;
  if (label) LABELS[k][v] = label;
  syncForm(); writeUrl(); onFilterChange();
}

function fillOptions(o) {
  if (FILTER_OPTS) return; FILTER_OPTS = o;
  const form = $('#afilters');
  const add = (name, items) => {
    const s = $(`select[name=${name}]`, form); if (!s) return;
    items.forEach(([v, t]) => { const op = document.createElement('option'); op.value = v; op.textContent = t; s.appendChild(op); });
  };
  add('type', o.types.map(t => [t, t]));
  add('source', o.sources.map(s => [s.key, s.label]));
  add('status', o.statuses.map(s => [s.key, STATUS[s.key].i + ' ' + s.label]));
  add('family', o.families.filter(f => f.n > 1).map(f => [f.key, `${short(f.key, 40)} (${f.n})`]));
  add('author', (o.authors || []).map(a => [a, a]));
  add('department', (o.departments || []).map(a => [a, a]));
  add('doc_type', (o.doc_types || []).map(a => [a, a]));
  add('batch', o.batches.map(b => [b.id, `${b.date} · ${short(b.first, 26)} и еще ${b.n - 1}`]));
  o.sources.forEach(s => { LABELS.source[s.key] = s.label; });
  o.statuses.forEach(s => { LABELS.status[s.key] = s.label; });
  o.batches.forEach(b => { LABELS.batch[b.id] = `${b.date}, ${b.n} док.`; });
  syncForm();
}

function syncForm() {
  const form = $('#afilters'); if (!form) return;
  $$('.seg', form).forEach(seg => $$('button', seg).forEach(b => b.classList.toggle('on', b.dataset.v === F[seg.dataset.name])));
  $('.custom-dates', form).hidden = F.period !== 'custom';
  ['from', 'to', 'q', 'family', 'batch', 'author', 'department', 'doc_type', 'type', 'source', 'status'].forEach(k => {
    const el = form.elements[k]; if (el) el.value = F[k] || '';
  });
  const chips = $('#chips'); chips.innerHTML = '';
  Object.keys(CHIP_NAMES).forEach(k => {
    if (!F[k]) return;
    const v = (LABELS[k] && LABELS[k][F[k]]) || F[k];
    const c = document.createElement('button'); c.type = 'button'; c.className = 'chip';
    c.innerHTML = `${CHIP_NAMES[k]}: <b>${esc(short(v, 40))}</b> ✕`; c.title = 'Убрать фильтр';
    c.onclick = () => setFilter(k, F[k]); chips.appendChild(c);
  });
}

function initFilters(defaults, cb) {
  readUrl(defaults); onFilterChange = cb;
  const form = $('#afilters');
  $$('.seg', form).forEach(seg => seg.addEventListener('click', e => {
    const b = e.target.closest('button'); if (!b) return;
    F[seg.dataset.name] = b.dataset.v;
    if (seg.dataset.name === 'period' && b.dataset.v === 'custom' && !F.from) {
      const d = new Date(); F.to = d.toISOString().slice(0, 10); d.setDate(d.getDate() - 29); F.from = d.toISOString().slice(0, 10);
    }
    syncForm(); writeUrl(); cb();
  }));
  let tm;
  form.addEventListener('input', e => {
    const el = e.target; if (!el.name) return;
    F[el.name] = el.value;
    clearTimeout(tm); tm = setTimeout(() => { syncForm(); writeUrl(); cb(); }, el.tagName === 'INPUT' && el.type === 'text' ? 400 : 0);
  });
  $('#freset').addEventListener('click', () => {
    FILTER_KEYS.forEach(k => { F[k] = defaults[k] || ''; }); syncForm(); writeUrl(); cb();
  });
  $$('[data-export]').forEach(a => a.addEventListener('click', () => {
    const u = a.dataset.export; location.href = u + (u.includes('?') ? '&' : '?') + qs();
  }));
  syncForm();
}

async function getJson(url) {
  const r = await fetch(url, {headers: {Accept: 'application/json'}});
  if (!r.ok) throw new Error(r.status + ' ' + r.statusText);
  return r.json();
}
async function postJson(url, body) {
  const r = await fetch(url, {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body)});
  const j = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(j.detail || j.error || r.statusText);
  return j;
}

/* ---------- типовые графики ---------- */
function hbar(id, items, opt) {
  opt = opt || {};
  const labels = items.map(r => r.label), data = items.map(r => r.n);
  mkChart(id, {
    type: 'bar',
    data: {labels, datasets: [{label: opt.series || 'Замечаний', data, backgroundColor: opt.colors || PAL[0],
      barPercentage: 0.82, categoryPercentage: 0.9, maxBarThickness: 26}]},
    options: {
      indexAxis: 'y', layout: {padding: {right: 40}},
      scales: {x: {beginAtZero: true, grid: {color: GRID}, ticks: {precision: 0}, title: {display: !!opt.xTitle, text: opt.xTitle}},
        y: {grid: {display: false}, ticks: {color: INK, callback(v) { return short(this.getLabelForValue(v), window.innerWidth < 600 ? 16 : (opt.trim || 34)); }}}},
      plugins: {endLabels: {on: true, fmt: opt.fmt}, tooltip: {callbacks: opt.tooltip || {}}},
      onClick: opt.onClick ? (e, els) => { if (els.length) opt.onClick(items[els[0].index]); } : undefined,
      onHover: opt.onClick ? (e, els) => { e.native.target.style.cursor = els.length ? 'pointer' : 'default'; } : undefined,
    },
  }, opt.head || ['Значение', 'Количество'], opt.rows || items.map(r => [r.label, r.n]));
}

function vbar(id, labels, data, opt) {
  opt = opt || {};
  mkChart(id, {
    type: 'bar',
    data: {labels, datasets: [{label: opt.series || 'Количество', data, backgroundColor: opt.color || PAL[0],
      barPercentage: 0.86, categoryPercentage: 0.9, maxBarThickness: 46}]},
    options: {
      layout: {padding: {top: 18}},
      scales: {y: {beginAtZero: true, grid: {color: GRID}, ticks: {precision: 0}, title: {display: !!opt.yTitle, text: opt.yTitle}},
        x: {grid: {display: false}, ticks: {color: INK, maxRotation: opt.rot ?? 0, autoSkip: labels.length > 12,
          callback(v) { return short(this.getLabelForValue(v), opt.trim || 18); }}}},
      plugins: {endLabels: {on: opt.labels !== false && data.length <= 16, fmt: opt.fmt}, refLine: opt.ref || {},
        tooltip: {callbacks: opt.tooltip || {}}},
      onClick: opt.onClick ? (e, els) => { if (els.length) opt.onClick(els[0].index); } : undefined,
      onHover: opt.onClick ? (e, els) => { e.native.target.style.cursor = els.length ? 'pointer' : 'default'; } : undefined,
    },
  }, opt.head || ['Период', opt.series || 'Количество'], labels.map((l, i) => [l, data[i]]));
}

function line(id, labels, datasets, opt) {
  opt = opt || {};
  const many = labels.length > 30;
  datasets.forEach((d, i) => Object.assign(d, Object.assign({
    borderColor: d.color || PAL[i], backgroundColor: d.color || PAL[i], pointRadius: many ? 0 : 4,
    pointBackgroundColor: d.color || PAL[i], pointBorderColor: SURFACE, pointBorderWidth: 2, spanGaps: true, fill: false,
  }, d)));
  mkChart(id, {
    type: 'line', data: {labels, datasets},
    options: {
      interaction: {mode: 'index', intersect: false},
      scales: {y: {beginAtZero: opt.zero !== false, max: opt.max, grid: {color: GRID}, title: {display: !!opt.yTitle, text: opt.yTitle},
        ticks: {callback: v => nf(v) + (opt.unit || '')}},
        x: {grid: {display: false}, ticks: {color: INK, maxRotation: 0, autoSkip: true, autoSkipPadding: 12}}},
      plugins: {refLine: opt.goal ? {lines: [{value: opt.goal, label: 'цель ' + nf(opt.goal) + (opt.unit || ''), color: '#d03b3b', width: 1.5}]} : {},
        legend: {display: datasets.filter(d => !d.noLegend).length > 1, position: 'top', align: 'end',
        labels: {filter: (it, data) => !data.datasets[it.datasetIndex].noLegend}},
      tooltip: {callbacks: {label: c => c.raw === null ? null : `${c.dataset.label}: ${nf(c.raw, 2)}${opt.unit || ''}`}}},
    },
  }, ['Период'].concat(datasets.map(d => d.label)), labels.map((l, i) => [l].concat(datasets.map(d => d.data[i]))));
}

function stacked(id, labels, series, colorOf, opt) {
  opt = opt || {};
  const keys = Object.keys(series).filter(k => series[k].some(v => v));
  const share = !!opt.share;
  const tot = labels.map((_, i) => keys.reduce((a, k) => a + series[k][i], 0));
  const val = (k, i) => share ? (tot[i] ? Math.round(1000 * series[k][i] / tot[i]) / 10 : 0) : series[k][i];
  mkChart(id, {
    type: 'bar',
    data: {labels, datasets: keys.map(k => ({label: k, data: labels.map((_, i) => val(k, i)), backgroundColor: colorOf(k),
      borderColor: SURFACE, borderWidth: 1, borderRadius: 0, borderSkipped: false, barPercentage: 0.9, categoryPercentage: 0.9,
      maxBarThickness: 46}))},
    options: {
      interaction: {mode: 'index', intersect: false},
      scales: {x: {stacked: true, grid: {display: false}, ticks: {color: INK, maxRotation: 0, autoSkip: true}},
        y: {stacked: true, beginAtZero: true, max: share ? 100 : undefined, grid: {color: GRID},
          ticks: {callback: v => nf(v) + (share ? ' %' : '')}}},
      plugins: {legend: {display: true, position: 'top', align: 'end'},
        tooltip: {callbacks: {label: c => `${c.dataset.label}: ${nf(c.raw)}${share ? ' %' : ''}`,
          footer: items => share ? '' : 'Всего: ' + nf(tot[items[0].dataIndex])}}},
    },
  }, ['Период'].concat(keys), labels.map((l, i) => [l].concat(keys.map(k => val(k, i)))));
}

function addShareToggle(id, cb) {
  const card = cardEl(id); if (!card || $('.share-tg', card)) return;
  const lab = document.createElement('label'); lab.className = 'share-tg';
  lab.innerHTML = '<input type="checkbox"> доли, %';
  $('.ch-tools', card).prepend(lab);
  $('input', lab).addEventListener('change', e => cb(e.target.checked));
}

function markLatin(s, rule) {
  // смешение алфавитов не видно глазом: подчеркиваются буквы «чужого» алфавита (которых в слове меньше)
  s = String(s ?? '');
  if (rule !== 'mixed' && rule !== 'latin') return esc(s);
  const lat = (s.match(/[A-Za-z]/g) || []).length, cyr = (s.match(/[А-Яа-яЁё]/g) || []).length;
  const re = rule === 'latin' || lat <= cyr ? /[A-Za-z]/ : /[А-Яа-яЁё]/;
  const what = re.source.includes('A-Z') ? 'латинская буква' : 'кириллическая буква';
  return Array.from(s).map(c => re.test(c) ? `<u class="lat" title="${what}">${esc(c)}</u>` : esc(c)).join('');
}

function statusPill(s) { const x = STATUS[s] || STATUS.new; return `<span class="spill" style="--c:${x.c}">${x.i} ${x.l}</span>`; }

function setKpi(id, v, d, suffix) {
  const el = document.getElementById('k-' + id); if (!el) return;
  $('b', el).textContent = nf(v, d) + (v !== null && v !== undefined && suffix ? suffix : '');
}

const GOAL_SIGN = {density: '≤', author_share: '≤', precision: '≥', verify_rate: '≥'};
function setGoal(id, goal, state) {
  // цель под значением плитки: значок + подпись, цвет только вместе с текстом
  const el = document.getElementById('k-' + id); if (!el || !goal) return;
  const i = $('i', el);
  const mark = state === 'ok' ? '<span class="delta good">✓ в пределах цели</span>' : state === 'bad' ? '<span class="delta bad">! цель не выполнена</span>' : '';
  i.innerHTML = `<span class="muted">цель ${GOAL_SIGN[id]} ${nf(goal)}</span> ${mark}`;
}

function loadingState(on) { document.body.classList.toggle('a-loading', on); }

/* ==================================================================== АНАЛИТИКА */
let EX = {offset: 0, limit: 50, text: '', bad: '', rows: []};

async function initAnalytics() {
  initCards();
  initFilters({period: 'all'}, () => { EX.offset = 0; loadAnalytics(); });
  const t = $('#ex-text'); let tm;
  t.addEventListener('input', () => { clearTimeout(tm); tm = setTimeout(() => { EX.text = t.value; EX.offset = 0; loadExplorer(); }, 350); });
  $('#ex-prev').onclick = () => { EX.offset = Math.max(0, EX.offset - EX.limit); loadExplorer(); };
  $('#ex-next').onclick = () => { EX.offset += EX.limit; loadExplorer(); };
  $('#ex-all').onchange = e => { $$('#ex-table tbody input').forEach(c => { c.checked = e.target.checked; }); bulkState(); };
  $('#ex-table').addEventListener('change', bulkState);
  $$('[data-bulk]').forEach(b => b.onclick = () => bulk(b.dataset.bulk));
  $$('[data-ex]').forEach(b => b.onclick = () => {
    location.href = '/api/analytics/export?format=findings&as=' + b.dataset.ex + '&' + qs({text: EX.text, bad: EX.bad});
  });
  loadAnalytics();
}

async function loadAnalytics() {
  loadingState(true);
  let d;
  try { d = await getJson('/api/analytics?' + qs()); }
  catch (e) { alert('Не удалось получить данные аналитики: ' + e.message); loadingState(false); return; }
  fillOptions(d.options);
  renderAnalytics(d);
  loadExplorer();
  loadingState(false);
}

function renderAnalytics(d) {
  const k = d.kpi;
  setKpi('docs', k.docs, 0); setKpi('pages', k.pages, 0); setKpi('findings', k.findings, 0); setKpi('density', k.density, 2);
  setKpi('author_share', k.author_share); setKpi('precision', k.precision); setKpi('reviewed_share', k.reviewed_share);
  setKpi('verify_rate', k.verify_rate); setKpi('avg_duration', k.avg_duration); setKpi('ai_share', k.ai_share);
  $('#kpi-foot').innerHTML = [
    `мест с ошибками с учетом системных строк: <b>${nf(k.occurrences, 0)}</b>`,
    `замечаний на документ: <b>${nf(k.per_doc)}</b>`,
    `${STATUS.accepted.i} принято <b>${nf(k.accepted, 0)}</b> · ${STATUS.rejected.i} отклонено <b>${nf(k.rejected, 0)}</b> · ${STATUS.new.i} не просмотрено <b>${nf(k.new, 0)}</b>`,
    `отредактировано: <b>${nf(k.edited, 0)}</b>`,
    k.auto_hidden ? `скрыто по опыту: <b>${nf(k.auto_hidden, 0)}</b>` : '',
    `кандидатов отсеяно машинно: <b>${nf(k.auto_rejected, 0)}</b>`,
    `медиана времени: <b>${nf(k.median_duration)}</b> с, <b>${nf(k.sec_per_page, 2)}</b> с/стр.`,
    k.failed ? `<span class="warn-t">⚠ заданий с ошибкой: <b>${k.failed}</b></span>` : '',
  ].filter(Boolean).join(' · ');
  const empty = !k.docs;
  document.body.classList.toggle('a-empty', empty);

  hbar('types', d.by_type, {colors: d.by_type.map(r => TYPE_COLOR[r.label] || OTHER), head: ['Тип', 'Замечаний'],
    onClick: r => setFilter('type', r.key)});
  hbar('sources', d.by_source, {colors: d.by_source.map(r => srcColor(r.key)), head: ['Источник', 'Замечаний'],
    onClick: r => setFilter('source', r.key, r.label)});
  hbar('status', d.by_status.map(r => ({...r, label: STATUS[r.key].i + ' ' + r.label})),
    {colors: d.by_status.map(r => STATUS[r.key].c), head: ['Статус', 'Замечаний'], onClick: r => setFilter('status', r.key, STATUS[r.key].l)});
  hbar('systemic', [{label: 'Единичные строки', n: d.systemic.single}, {label: 'Системные строки', n: d.systemic.systemic}],
    {head: ['Вид', 'Строк'], rows: [['Единичные строки', d.systemic.single], ['Системные строки', d.systemic.systemic],
      ['Мест в системных строках', d.systemic.systemic_occ]],
     tooltip: {afterLabel: c => c.dataIndex === 1 ? `мест в документах: ${nf(d.systemic.systemic_occ, 0)}` : ''}});
  hbar('rules', d.top_rules, {head: ['Правило', 'Замечаний'], trim: 40,
    tooltip: {afterLabel: c => 'Источник: ' + (LABELS.source[d.top_rules[c.dataIndex].source] || '')},
    onClick: r => setFilter('rule', r.key, r.label)});
  d.rules.forEach(r => { LABELS.rule[r.rule] = r.label; });

  const dens = d.density.slice(0, 30);
  dens.forEach(r => { LABELS.job[r.id] = r.name; });
  vbar('density', dens.map(r => r.name), dens.map(r => r.density), {series: 'На 10 страниц', rot: 40, trim: 22, labels: false,
    ref: {lines: [k.density ? {value: k.density, label: 'в среднем ' + nf(k.density, 2)} : null,
      d.goals.density ? {value: d.goals.density, label: 'цель ' + nf(d.goals.density, 1), color: '#d03b3b', width: 1.5} : null].filter(Boolean)},
    tooltip: {title: it => dens[it[0].dataIndex].name,
      afterLabel: c => { const r = dens[c.dataIndex]; return `${r.findings} замеч. на ${r.pages} стр. · ${r.created} · ${r.verify_ok ? '25/25 ✓' : 'проверка не пройдена ✕'}`; }},
    head: ['№', 'Документ', 'Дата', 'Страниц', 'Замечаний', 'На 10 страниц'],
    onClick: i => { location.href = '/job/' + dens[i].id; }});
  CH.density.rows = d.density.map(r => [r.id, r.name, r.created, r.pages, r.findings, r.density]);
  if (CH.density.tableView) renderTable('density');

  renderHeat(d.heatmap);
  vbar('position', d.position.map(r => r.label), d.position.map(r => r.n), {series: 'Замечаний', head: ['Часть документа', 'Замечаний'], trim: 10});
  vbar('perdoc', d.per_doc_hist.map(r => r.label), d.per_doc_hist.map(r => r.n), {series: 'Документов', head: ['Замечаний', 'Документов']});
  vbar('pages', d.pages_hist.map(r => r.label), d.pages_hist.map(r => r.n), {series: 'Документов', head: ['Страниц', 'Документов']});

  mkChart('duration', {
    type: 'scatter',
    data: {datasets: [{label: 'Документ', data: d.duration, backgroundColor: PAL[0] + 'cc', borderColor: SURFACE, borderWidth: 1.5,
      pointRadius: 5, pointHoverRadius: 7}]},
    options: {
      scales: {x: {beginAtZero: true, title: {display: true, text: 'страниц'}, grid: {color: GRID}},
        y: {beginAtZero: true, title: {display: true, text: 'секунд'}, grid: {color: GRID}}},
      plugins: {tooltip: {callbacks: {label: c => `${short(c.raw.name, 40)}: ${c.raw.x} стр., ${nf(c.raw.y)} с`}}},
      onClick: (e, els) => { if (els.length) location.href = '/job/' + d.duration[els[0].index].id; },
    },
  }, ['№', 'Документ', 'Страниц', 'Секунд'], d.duration.map(r => [r.id, r.name, r.x, r.y]));

  hbar('stages', d.stages, {series: 'Секунд', head: ['Этап', 'Секунд в среднем'], fmt: v => nf(v) + ' с', trim: 26,
    tooltip: {title: it => d.stages[it[0].dataIndex].label},
    rows: d.stages.map(r => [r.label, r.n])});
  hbar('preflight', d.preflight.map(r => ({...r, n: r.docs})), {series: 'Документов', head: ['Проблема', 'Документов', 'Случаев'],
    rows: d.preflight.map(r => [r.label, r.docs, r.items]),
    tooltip: {afterLabel: c => 'случаев: ' + nf(d.preflight[c.dataIndex].items, 0)}});
  if (d.verify_fail.length) hbar('verify', d.verify_fail, {series: 'Документов', head: ['Требование', 'Документов'], trim: 26,
    tooltip: {title: it => d.verify_fail[it[0].dataIndex].label}});
  else {
    if (CH.verify && CH.verify.chart) { CH.verify.chart.destroy(); CH.verify.chart = null; }
    const el = cardEl('verify'); $('.ch-body', el).style.display = 'none';
    $('.ch-empty', el).hidden = false;
    $('.ch-empty', el).innerHTML = k.docs ? '<span class="spill" style="--c:#0ca30c">✓ Все отчеты прошли 25 из 25 проверок</span>' : 'Нет данных';
    CH.verify = {head: ['Требование', 'Документов'], rows: []};
  }
  renderPagesTop(d.pages_top);
  renderRuleTab(d.rules);
  renderRecurring(d.recurring);
  renderUnknown(d.unknown);
  Object.entries(d.goals).forEach(([key, g]) => setGoal(key, g, d.goal_state[key]));
  renderRecs(d.recommendations);
  renderAuthors(d.by_author, d.goals);
  hbar('doctypes', d.by_doc_type.map(r => ({...r, n: r.density || 0})), {series: 'На 10 страниц', head: ['Вид документа', 'Документов', 'Замечаний', 'На 10 страниц'],
    rows: d.by_doc_type.map(r => [r.label, r.docs, r.findings, r.density]), fmt: v => nf(v, 2),
    tooltip: {afterLabel: c => { const r = d.by_doc_type[c.dataIndex]; return `${r.docs} док., ${r.findings} замеч.`; }},
    onClick: r => { if (r.key) setFilter('doc_type', r.key); }});
  hbar('depts', d.by_department.map(r => ({...r, n: r.density || 0})), {series: 'На 10 страниц', head: ['Отдел', 'Документов', 'Замечаний', 'На 10 страниц'],
    rows: d.by_department.map(r => [r.label, r.docs, r.findings, r.density]), fmt: v => nf(v, 2),
    tooltip: {afterLabel: c => { const r = d.by_department[c.dataIndex]; return `${r.docs} док., ${r.findings} замеч.`; }},
    onClick: r => { if (r.key) setFilter('department', r.key); }});
}

function renderRecs(recs) {
  const ICON = {critical: ['!', '#d03b3b', 'важно'], warning: ['▲', '#c47a00', 'внимание'], info: ['i', '#2a78d6', 'совет']};
  const html = recs.length ? '<div class="recs">' + recs.map((r, i) => {
    const [ic, col, lab] = ICON[r.level];
    const a = r.action || {};
    const allowed = a.type === 'link' || (a.type === 'setting' ? ROLE === 'admin' : ROLE !== 'author');
    const btn = !allowed ? '<span class="muted small" title="Действие доступно корректору или администратору">нет прав</span>' : a.type === 'link' ? `<a class="btn small ghost" href="${esc(a.href)}">${esc(a.label)}</a>`
      : `<button type="button" class="btn small" data-rec="${i}">${esc(a.label)}</button>`;
    return `<div class="rec" style="--c:${col}"><span class="rec-ic" title="${lab}">${ic}</span><div><b>${esc(r.title)}</b>`
      + `<div class="muted small">${esc(r.text)}</div></div><div class="nowrap">${a.label ? btn : ''}</div></div>`;
  }).join('') + '</div>' : '<p class="muted">✓ Рекомендаций нет: цели выполняются, правила работают точно.</p>';
  setHtml('recs', html, ['Важность', 'Что', 'Подробности'], recs.map(r => [r.level, r.title, r.text]));
  if (!recs.length) { $('#c-recs .ch-table').hidden = false; $('#c-recs .ch-empty').hidden = true; }
  $$('#c-recs [data-rec]').forEach(b => b.onclick = async () => {
    const r = recs[b.dataset.rec], a = r.action;
    if (!confirm(r.title + '\n\n' + a.label + '?')) return;
    try {
      if (a.type === 'setting') await postJson('/settings/apply', {key: a.key, value: a.value});
      else if (a.type === 'whitelist') await postJson('/api/analytics/whitelist', {words: a.words});
      else if (a.type === 'glossary') await postJson('/api/analytics/glossary', {good: a.good, bad: a.bad});
      b.textContent = 'Сделано ✓'; b.disabled = true;
    } catch (e) { alert('Не выполнено: ' + e.message); }
  });
}

function renderAuthors(rows, goals) {
  const max = Math.max(1, ...rows.map(r => r.density || 0), goals.density || 0);
  const gx = goals.density ? Math.round(160 * goals.density / max) : null;
  const html = rows.length ? '<div class="tscroll"><table class="t compact"><thead><tr><th>Автор</th><th>Документов</th><th>Страниц</th>'
    + '<th>Замечаний</th><th>На 10 страниц</th><th>Точность</th><th>«Автору»</th><th>25/25</th></tr></thead><tbody>'
    + rows.map(r => {
      const over = goals.density && r.density > goals.density;
      return `<tr><td>${r.key ? `<a href="#" data-author="${esc(r.key)}">${esc(r.label)}</a>` : `<span class="muted">${esc(r.label)}</span>`}</td>`
        + `<td>${r.docs}</td><td>${r.pages}</td><td>${r.findings}</td>`
        + `<td class="nowrap"><span class="gbar"><span class="ibar" style="width:${Math.round(160 * (r.density || 0) / max)}px"></span>`
        + (gx ? `<i style="left:${gx}px" title="цель ${nf(goals.density)}"></i>` : '') + `</span> ${nf(r.density, 2)}`
        + (over ? ' <span class="delta bad">!</span>' : '') + '</td>'
        + `<td>${r.precision === null ? '—' : nf(r.precision) + ' %'}</td><td>${nf(r.author_share)} %</td><td>${nf(r.verify_rate)} %</td></tr>`;
    }).join('') + '</tbody></table></div>'
    + (gx ? '<div class="muted small" style="margin-top:6px">Красная риска — цель по плотности ошибок; «!» — цель не выполнена.</div>' : '')
    : '<p class="muted">Авторы не указаны. Поле «Автор документа» заполняется при загрузке; сделать его обязательным можно в настройках «Цели качества и загрузка».</p>';
  setHtml('authors', html, ['Автор', 'Документов', 'Страниц', 'Замечаний', 'На 10 страниц', 'Точность, %', '«Автору», %', '25/25, %'],
    rows.map(r => [r.label, r.docs, r.pages, r.findings, r.density, r.precision, r.author_share, r.verify_rate]));
  if (!rows.length) { $('#c-authors .ch-table').hidden = false; $('#c-authors .ch-empty').hidden = true; }
  $$('#c-authors [data-author]').forEach(a => a.onclick = e => { e.preventDefault(); setFilter('author', a.dataset.author); });
}

function renderHeat(h) {
  const max = Math.max(1, ...h.rows.flatMap(r => r.values));
  const html = '<div class="tscroll"><table class="heat"><thead><tr><th>Документ</th>' + h.cols.map(c => `<th title="${esc(c)}">${esc(short(c, 12))}</th>`).join('')
    + '<th>Всего</th></tr></thead><tbody>' + h.rows.map(r => `<tr><td class="hname"><a href="/job/${r.id}" title="${esc(r.name)}">${esc(short(r.name, 42))}</a></td>`
      + r.values.map((v, i) => `<td class="hc" data-job="${r.id}" data-type="${esc(h.cols[i])}" style="background:${seqColor(v, max)};color:${seqInk(v, max)}" title="${esc(r.name)} · ${esc(h.cols[i])}: ${v}">${v || ''}</td>`).join('')
      + `<td class="hsum">${r.values.reduce((a, b) => a + b, 0)}</td></tr>`).join('') + '</tbody></table></div>'
    + `<div class="legend-seq"><span>0</span>${SEQ.map(c => `<i style="background:${c}"></i>`).join('')}<span>${max}</span></div>`;
  setHtml('heat', html, ['№', 'Документ'].concat(h.cols), h.rows.map(r => [r.id, r.name].concat(r.values)));
  $$('#c-heat .hc').forEach(td => td.onclick = () => {
    LABELS.job[td.dataset.job] = $('a', td.parentElement).title;
    F.job = td.dataset.job; F.type = td.dataset.type; syncForm(); writeUrl(); onFilterChange();
    setTimeout(() => $('#explorer').scrollIntoView({behavior: 'smooth'}), 400);
  });
}

function renderPagesTop(rows) {
  const max = Math.max(1, ...rows.map(r => r.n));
  const html = '<table class="t compact"><thead><tr><th>Документ</th><th>Стр.</th><th>Замечаний</th></tr></thead><tbody>'
    + rows.map(r => `<tr><td><a href="/job/${r.id}">${esc(short(r.name, 44))}</a></td><td><a href="/job/${r.id}/page/${r.page}.png" target="_blank">стр. ${r.page}</a></td>`
      + `<td><span class="ibar" style="width:${Math.round(70 * r.n / max)}px"></span> ${r.n}</td></tr>`).join('') + '</tbody></table>';
  setHtml('pagestop', html, ['№', 'Документ', 'Страница', 'Замечаний'], rows.map(r => [r.id, r.name, r.page, r.n]));
}

function renderRuleTab(rules) {
  const html = '<div class="tscroll"><table class="t compact"><thead><tr><th>Правило</th><th>Источник</th><th>Замечаний</th><th>Док.</th>'
    + '<th>Решения корректора</th><th>Точность</th><th>Настройка</th></tr></thead><tbody>'
    + rules.map(r => {
      const dec = r.accepted + r.rejected, tot = r.n || 1;
      const warn = r.precision !== null && r.precision < 50 && dec >= 10;
      const bar = ['accepted', 'rejected', 'new'].map(s => r[s] ? `<i style="width:${(100 * r[s] / tot).toFixed(1)}%;background:${STATUS[s].c}" title="${STATUS[s].l}: ${r[s]}"></i>` : '').join('');
      return `<tr><td><a href="#" data-rule="${esc(r.rule)}">${esc(r.label)}</a> <span class="key">${esc(r.rule)}</span></td>`
        + `<td><span class="dot" style="background:${srcColor(r.source)}"></span>${esc(r.source_label)}</td><td>${r.n}</td><td>${r.docs}</td>`
        + `<td><div class="sbar">${bar}</div><span class="muted small">${STATUS.accepted.i} ${r.accepted} · ${STATUS.rejected.i} ${r.rejected} · ${STATUS.new.i} ${r.new}</span></td>`
        + `<td>${r.precision === null ? '<span class="muted">нет решений</span>' : (warn ? `<span class="spill" style="--c:#d03b3b">⚠ ${nf(r.precision)} %</span>` : nf(r.precision) + ' %')}</td>`
        + `<td>${r.setting ? `<a href="/settings#set-${esc(r.setting)}">настроить →</a>` : ''}</td></tr>`;
    }).join('') + '</tbody></table></div>';
  setHtml('ruletab', html, ['Правило', 'Код', 'Источник', 'Замечаний', 'Документов', 'Принято', 'Отклонено', 'Не просмотрено', 'Точность, %'],
    rules.map(r => [r.label, r.rule, r.source_label, r.n, r.docs, r.accepted, r.rejected, r.new, r.precision]));
  $$('#c-ruletab [data-rule]').forEach(a => a.onclick = e => { e.preventDefault(); setFilter('rule', a.dataset.rule, a.textContent); });
}

const NO_GLOSS = ['glossary', 'phone', 'latin', 'mixed', 'range', 'dot', 'dspace', 'hanging', 'thousands', 'decimal', 'pct',
  'unit', 'times', 'equals', 'degree', 'inch', 'mwc', 'digitword', 'quotes', 'std', 'table', 'toc', 'ref', 'requisites', 'candidate', 'batch'];
function canGloss(r) {
  // в глоссарий — только пары терминов: одинаковое число слов, без знаков препинания, не логика и не типографика
  if (!r.good || r.good === '—' || NO_GLOSS.includes(r.rule) || r.type === 'Логика') return false;
  if (/[:,;.!?()«»"]/.test(r.good + r.bad) || r.good.toLowerCase() === r.bad.toLowerCase()) return false;
  return r.good.trim().split(/\s+/).length === r.bad.trim().split(/\s+/).length;
}

function renderRecurring(rows) {
  const html = '<div class="tscroll"><table class="t compact"><thead><tr><th>Ошибка</th><th>Как правильно</th><th>Тип</th><th>Правило</th>'
    + '<th>Мест</th><th>Документов</th><th></th></tr></thead><tbody>'
    + rows.map((r, i) => `<tr><td class="bad-t">${esc(r.bad)}</td><td class="good-t">${esc(r.good || '—')}</td>`
      + `<td><span class="dot" style="background:${TYPE_COLOR[r.type] || OTHER}"></span>${esc(r.type)}</td><td>${esc(r.rule_label)}</td>`
      + `<td>${r.n}</td><td>${r.docs}</td><td class="nowrap"><button type="button" class="btn small ghost" data-show="${i}">Показать</button> `
      + (canGloss(r) ? `<button type="button" class="btn small ghost" data-gl="${i}" title="Добавить пару в глоссарий компании">В глоссарий</button>` : '')
      + '</td></tr>').join('') + '</tbody></table></div>';
  setHtml('recurring', html, ['Ошибка', 'Как правильно', 'Тип', 'Правило', 'Мест', 'Документов'],
    rows.map(r => [r.bad, r.good, r.type, r.rule_label, r.n, r.docs]));
  $$('#c-recurring [data-show]').forEach(b => b.onclick = () => {
    EX.bad = rows[b.dataset.show].bad; EX.offset = 0; loadExplorer(); $('#explorer').scrollIntoView({behavior: 'smooth'});
  });
  $$('#c-recurring [data-gl]').forEach(b => b.onclick = async () => {
    const r = rows[b.dataset.gl];
    if (!confirm(`Добавить в глоссарий: «${r.good}» — правильно, «${r.bad}» — неправильно?\nСледующие проверки будут находить это написание всегда.`)) return;
    try { const j = await postJson('/api/analytics/glossary', {good: r.good, bad: r.bad}); b.textContent = j.added ? 'Добавлено ✓' : 'Уже есть'; b.disabled = true; }
    catch (e) { alert(e.message); }
  });
}

function renderUnknown(rows) {
  const html = `<div class="filters"><button type="button" class="btn small" id="wl-add" disabled>Добавить выбранные в белый список</button>
    <span class="muted">Слова из белого списка больше не считаются ошибками (Настройки → Орфография).</span></div>
    <div class="tscroll"><table class="t compact"><thead><tr><th><input type="checkbox" id="wl-all"></th><th>Слово</th><th>Раз</th><th>Документов</th>
    <th>Решения</th><th></th></tr></thead><tbody>`
    + rows.map((r, i) => `<tr><td>${r.whitelisted ? '' : `<input type="checkbox" data-w="${i}">`}</td><td><b>${esc(r.word)}</b></td><td>${r.n}</td><td>${r.docs}</td>`
      + `<td class="muted small">${STATUS.accepted.i} ${r.accepted} · ${STATUS.rejected.i} ${r.rejected}</td>`
      + `<td>${r.whitelisted ? '<span class="spill" style="--c:#0ca30c">✓ в белом списке</span>' : `<button type="button" class="btn small ghost" data-w1="${i}">+ в белый список</button>`}</td></tr>`).join('')
    + '</tbody></table></div>';
  setHtml('unknown', html, ['Слово', 'Раз', 'Документов', 'Принято', 'Отклонено', 'В белом списке'],
    rows.map(r => [r.word, r.n, r.docs, r.accepted, r.rejected, r.whitelisted ? 'да' : 'нет']));
  const box = cardEl('unknown');
  const sel = () => $$('[data-w]', box).filter(c => c.checked).map(c => rows[c.dataset.w].word);
  const upd = () => { $('#wl-add').disabled = !sel().length; };
  box.querySelectorAll('[data-w]').forEach(c => c.onchange = upd);
  const all = $('#wl-all'); if (all) all.onchange = () => { $$('[data-w]', box).forEach(c => { c.checked = all.checked; }); upd(); };
  const add = async words => {
    try { const j = await postJson('/api/analytics/whitelist', {words}); loadAnalytics(); return j; }
    catch (e) { alert(e.message); }
  };
  $('#wl-add').onclick = () => add(sel());
  $$('[data-w1]', box).forEach(b => b.onclick = () => add([rows[b.dataset.w1].word]));
}

async function loadExplorer() {
  let d;
  try { d = await getJson(`/api/analytics/findings?limit=${EX.limit}&offset=${EX.offset}&` + qs({text: EX.text, bad: EX.bad})); }
  catch (e) { return; }
  EX.rows = d.rows;
  const tb = $('#ex-table tbody');
  tb.innerHTML = d.rows.map((r, i) => `<tr class="${r.status}"><td><input type="checkbox" data-i="${i}"></td>`
    + `<td><a href="/job/${r.job}">${esc(short(r.name, 34))}</a><div class="muted small">${r.created}</div></td>`
    + `<td><a href="/job/${r.job}/page/${r.page}.png" target="_blank">${r.page}</a></td>`
    + `<td class="nowrap"><span class="dot" style="background:${TYPE_COLOR[r.type] || OTHER}"></span>${esc(r.type)}</td>`
    + `<td>${esc(r.rule_label)}<div class="muted small">${esc(r.source_label)}</div></td>`
    + `<td class="bad-t">${markLatin(r.bad, r.rule)}${r.systemic ? `<div class="muted small">системно: ${r.systemic} мест</div>` : ''}</td>`
    + `<td class="good-t">${esc(r.good)}</td><td>${statusPill(r.status)}${r.edited ? '<div class="muted small">✎ изменено</div>' : ''}</td></tr>`).join('')
    || '<tr><td colspan="8" class="muted">Замечаний нет</td></tr>';
  const to = Math.min(d.total, EX.offset + EX.limit);
  $('#ex-count').textContent = `найдено: ${nf(d.total, 0)}` + (EX.bad ? ` · ошибка «${EX.bad}»` : '');
  $('#ex-sub').innerHTML = EX.bad ? `только «${esc(EX.bad)}» · <a href="#" id="ex-clear">показать все</a>` : 'все замечания с учетом фильтров';
  const c = $('#ex-clear'); if (c) c.onclick = e => { e.preventDefault(); EX.bad = ''; loadExplorer(); };
  $('#ex-page').textContent = d.total ? `${EX.offset + 1}–${to} из ${nf(d.total, 0)}` : '';
  $('#ex-prev').disabled = EX.offset <= 0; $('#ex-next').disabled = to >= d.total;
  $('#ex-all').checked = false; bulkState();
}

function bulkState() {
  const n = $$('#ex-table tbody input:checked').length;
  $$('[data-bulk]').forEach(b => { b.disabled = !n; });
}

async function bulk(status) {
  const items = $$('#ex-table tbody input:checked').map(c => ({job: EX.rows[c.dataset.i].job, id: EX.rows[c.dataset.i].id}));
  if (!items.length) return;
  if (!confirm(`Отметить выбранные замечания (${items.length}) как «${STATUS[status].l}» и пересобрать отчеты затронутых документов?`)) return;
  loadingState(true);
  try { await postJson('/api/analytics/bulk', {items, status}); } catch (e) { alert(e.message); }
  loadAnalytics();
}

/* ==================================================================== ТРЕНДЫ */
let TD = null, typeShare = false;

function initTrends() {
  initCards();
  initFilters({period: '180', gran: 'week'}, loadTrends);
  addShareToggle('t-types', on => { typeShare = on; renderTypes(); });
  loadTrends();
}

async function loadTrends() {
  loadingState(true);
  try { TD = await getJson('/api/trends?' + qs()); }
  catch (e) { alert('Не удалось получить данные трендов: ' + e.message); loadingState(false); return; }
  fillOptions(TD.options);
  renderTrends(TD);
  loadingState(false);
}

const CMP_FMT = {docs: 0, pages: 0, density: 2, per_doc: 1, author_share: 1, precision: 1, reviewed_share: 1, verify_rate: 1,
  avg_duration: 1, sec_per_page: 2};
const PCT_KEYS = ['author_share', 'precision', 'reviewed_share', 'verify_rate'];

function renderCompare(t) {
  Object.entries(CMP_FMT).forEach(([k, d]) => {
    const el = document.getElementById('k-' + k); if (!el) return;
    const c = t.compare[k];
    $('b', el).textContent = nf(c.cur, d);
    const unit = PCT_KEYS.includes(k) ? ' п. п.' : '';
    let txt = `ранее ${nf(c.prev, d)}`;
    if (c.delta !== null) {
      const arrow = c.delta > 0 ? '▲' : c.delta < 0 ? '▼' : '=';
      const rel = PCT_KEYS.includes(k) ? `${c.delta > 0 ? '+' : ''}${nf(c.delta, 1)}${unit}` : (c.pct !== null ? `${c.pct > 0 ? '+' : ''}${nf(c.pct, 1)} %` : '');
      const v = {better: ['good', '✓ лучше'], worse: ['bad', '! хуже'], same: ['', 'без изменений'], up: ['', ''], down: ['', '']}[c.verdict];
      txt = `<span class="delta ${v[0]}">${arrow} ${rel}${v[1] ? ' · ' + v[1] : ''}</span> <span class="muted">ранее ${nf(c.prev, d)}</span>`;
    }
    $('i', el).innerHTML = txt;
  });
  $('#prev-period').textContent = `(${t.filters.from} — ${t.filters.to} к ${t.prev_period.from} — ${t.prev_period.to}, документов ранее: ${t.prev_period.docs})`;
}

function insight(t) {
  const parts = [], c = t.compare;
  if (c.density.cur !== null && c.density.prev !== null && c.density.pct !== null) {
    parts.push(`Плотность ошибок ${c.density.delta < 0 ? 'снизилась' : c.density.delta > 0 ? 'выросла' : 'не изменилась'}`
      + (c.density.delta ? ` на ${nf(Math.abs(c.density.pct))} % (с ${nf(c.density.prev, 2)} до ${nf(c.density.cur, 2)} на 10 страниц)` : '') + '.');
  } else if (c.density.cur !== null) parts.push(`Плотность ошибок за период — ${nf(c.density.cur, 2)} на 10 страниц; в предыдущем периоде документов нет.`);
  if (t.density_slope !== null) parts.push(`Линейный тренд: ${t.density_slope < 0 ? '↓ снижение' : t.density_slope > 0 ? '↑ рост' : 'ровно'} на ${nf(Math.abs(t.density_slope), 3)} за ${({day: 'день', week: 'неделю', month: 'месяц'})[t.gran]}.`);
  const up = t.rules.filter(r => r.delta_pct !== null && r.delta_pct > 30 && r.cur >= 5).slice(0, 2);
  if (up.length) parts.push('Заметно чаще срабатывают: ' + up.map(r => `«${r.label}» (+${nf(r.delta_pct, 0)} %)`).join(', ') + '.');
  if (t.new_rules.length && t.has_before) parts.push('Впервые в периоде: ' + t.new_rules.slice(0, 3).map(r => `«${r.label}»`).join(', ') + '.');
  const box = $('#t-insight'); box.hidden = !parts.length; box.innerHTML = '<b>Коротко:</b> ' + parts.join(' ');
}

function renderTypes() {
  if (!TD) return;
  stacked('t-types', TD.labels, TD.by_type, k => TYPE_COLOR[k] || OTHER, {share: typeShare});
}

function renderTrends(t) {
  renderCompare(t); insight(t);
  const L = t.labels, s = t.series;
  // плотность: факт, скользящее среднее, тренд, прогноз
  const n = L.length, all = L.concat(t.future_labels), pad = a => a.concat(Array(t.future_labels.length).fill(null));
  const fc = Array(n).fill(null); if (n && t.density_trend[n - 1] !== null) fc[n - 1] = t.density_trend[n - 1];
  line('t-density', all, [
    {label: 'На 10 страниц', data: pad(s.density), color: PAL[0]},
    {label: 'Скользящее среднее (3)', data: pad(t.density_ma), color: PAL[1], pointRadius: 0},
    {label: 'Линейный тренд', data: pad(t.density_trend), color: TREND, borderWidth: 1.5, pointRadius: 0},
    {label: 'Прогноз', data: fc.concat(t.density_forecast), color: TREND, borderDash: [6, 5], borderWidth: 1.5, pointRadius: 3, pointStyle: 'circle'},
  ], {yTitle: 'замечаний на 10 стр.'});
  if (t.goals.density) { CH['t-density'].chart.options.plugins.refLine = {lines: [{value: t.goals.density, label: 'цель ' + nf(t.goals.density, 1), color: '#d03b3b', width: 1.5}]}; CH['t-density'].chart.update('none'); }
  vbar('t-docs', L, s.docs, {series: 'Документов', labels: L.length <= 16});
  vbar('t-pages', L, s.pages, {series: 'Страниц', labels: L.length <= 16});
  renderTypes();
  stacked('t-sources', L, t.by_source, k => srcColor(Object.keys(SRC_COLOR).find(x => LABELS.source[x] === k)) , {});
  line('t-author', L, [{label: 'Доля «автору»', data: s.author_share, color: PAL[0]}], {unit: ' %', max: 100, goal: t.goals.author_share});
  line('t-precision', L, [{label: 'Точность', data: s.precision, color: PAL[0]}], {unit: ' %', max: 100, goal: t.goals.precision});
  line('t-verify', L, [{label: 'Прошли 25/25', data: s.verify_rate, color: PAL[0]}], {unit: ' %', max: 100, goal: t.goals.verify_rate});
  line('t-duration', L, [{label: 'Секунд на документ', data: s.avg_duration, color: PAL[0]}], {unit: ' с'});
  renderRuleTrends(t);
  renderNew(t);
  renderFamilies(t);
  renderCalendar(t);
  renderWeekHour(t);
}

function spark(vals, color) {
  const w = 120, h = 28, max = Math.max(1, ...vals), n = vals.length;
  if (n < 2) return '';
  const pts = vals.map((v, i) => `${(i * (w - 4) / (n - 1) + 2).toFixed(1)},${(h - 3 - (v / max) * (h - 6)).toFixed(1)}`);
  const last = pts[pts.length - 1].split(',');
  return `<svg class="spark" width="${w}" height="${h}" viewBox="0 0 ${w} ${h}" aria-hidden="true"><polyline fill="none" stroke="${color}" stroke-width="1.6" points="${pts.join(' ')}"/>`
    + `<circle cx="${last[0]}" cy="${last[1]}" r="2.5" fill="${color}"/></svg>`;
}

function deltaCell(p, lowerBetter) {
  if (p === null || p === undefined) return '<span class="muted">нет базы</span>';
  if (Math.abs(p) < 0.05) return '<span class="muted">= 0 %</span>';
  const good = lowerBetter ? p < 0 : p > 0;
  return `<span class="delta ${good ? 'good' : 'bad'}">${p > 0 ? '▲ +' : '▼ '}${nf(p, 1)} % · ${good ? '✓ лучше' : '! хуже'}</span>`;
}

function renderRuleTrends(t) {
  const html = '<div class="tscroll"><table class="t compact"><thead><tr><th>Правило</th><th>Динамика</th><th>За период</th><th>Ранее</th>'
    + '<th>На 10 стр.</th><th>Изменение</th></tr></thead><tbody>'
    + t.rules.map(r => `<tr><td><span class="dot" style="background:${srcColor(r.source)}"></span>${esc(r.label)}<div class="muted small">${esc(r.source_label)}</div></td>`
      + `<td>${spark(r.spark, PAL[0])}</td><td>${r.cur}</td><td>${r.prev}</td><td>${nf(r.cur_density, 3)}</td><td>${deltaCell(r.delta_pct, true)}</td></tr>`).join('')
    + '</tbody></table></div>';
  setHtml('t-rules', html, ['Правило', 'Код', 'За период', 'Ранее', 'На 10 стр.', 'На 10 стр. ранее', 'Изменение, %'].concat(t.labels),
    t.rules.map(r => [r.label, r.rule, r.cur, r.prev, r.cur_density, r.prev_density, r.delta_pct].concat(r.spark)));
}

function renderNew(t) {
  const li = (arr, f) => arr.length ? '<ul class="plain">' + arr.map(f).join('') + '</ul>' : '<p class="muted">нет</p>';
  const note = t.has_before ? '' : '<p class="notice info">До начала периода документов нет — все сочтено новым. Расширьте период или выберите более поздний.</p>';
  const html = note + `<div class="grid g3">
    <div><h4>Новые правила</h4>${li(t.new_rules, r => `<li><span class="dot" style="background:${srcColor(r.source)}"></span>${esc(r.label)} <b>${r.n}</b></li>`)}
      ${t.gone_rules.length ? '<h4>Больше не срабатывают</h4>' + li(t.gone_rules, r => `<li>✓ ${esc(r.label)}</li>`) : ''}</div>
    <div><h4>Новые ошибки</h4>${li(t.new_pairs.slice(0, 15), r => `<li><span class="bad-t">${esc(short(r.bad, 40))}</span> → <span class="good-t">${esc(short(r.good, 40))}</span> <span class="muted">${r.n}</span></li>`)}</div>
    <div><h4>Новые слова вне словаря</h4>${li(t.new_words.slice(0, 20), r => `<li><b>${esc(r.word)}</b> <span class="muted">${r.n}</span></li>`)}</div></div>`;
  const rows = t.new_rules.map(r => ['правило', r.label, '', r.n]).concat(t.new_pairs.map(r => ['ошибка', r.bad, r.good, r.n]),
    t.new_words.map(r => ['слово', r.word, '', r.n]));
  setHtml('t-new', html, ['Вид', 'Что', 'Как правильно', 'Раз'], rows.length ? rows : [['—', 'нет', '', 0]]);
}

function renderFamilies(t) {
  const fams = t.families.slice(0, PAL.length);
  const maxV = Math.max(0, ...fams.map(f => f.versions.length));
  const labels = Array.from({length: maxV}, (_, i) => 'версия ' + (i + 1));
  if (!fams.length) {
    if (CH['t-families'] && CH['t-families'].chart) CH['t-families'].chart.destroy();
    CH['t-families'] = {head: ['Семейство'], rows: []};
    setData('t-families', ['Семейство'], []);
  } else {
    line('t-families', labels, fams.map((f, i) => ({label: short(f.family, 34), data: f.versions.map(v => v.density), color: PAL[i]})),
      {yTitle: 'замечаний на 10 стр.'});
    CH['t-families'].rows = fams.flatMap(f => f.versions.map((v, i) => [f.family, i + 1, v.name, v.created, v.findings, v.density]));
    CH['t-families'].head = ['Семейство', 'Версия', 'Документ', 'Дата', 'Замечаний', 'На 10 стр.'];
  }
  const html = '<div class="tscroll"><table class="t compact"><thead><tr><th>Семейство</th><th>Версий</th><th>Первая</th><th>Последняя</th><th>Изменение</th><th>Версии</th></tr></thead><tbody>'
    + t.families.map(f => `<tr><td>${esc(f.family)}</td><td>${f.versions.length}</td><td>${nf(f.first, 2)}</td><td>${nf(f.last, 2)}</td><td>${deltaCell(f.change_pct, true)}</td>`
      + `<td>${f.versions.map((v, i) => `<a href="/job/${v.id}" title="${esc(v.name)} · ${v.created}">${i + 1}</a>`).join(' → ')}`
      + ` <a class="small" href="/compare?a=${f.versions[0].id}&b=${f.versions[f.versions.length - 1].id}" title="Что исправлено между первой и последней версией">сравнить</a></td></tr>`).join('')
    + '</tbody></table></div>';
  setHtml('t-famtab', html, ['Семейство', 'Версий', 'Первая, на 10 стр.', 'Последняя, на 10 стр.', 'Изменение, %'],
    t.families.map(f => [f.family, f.versions.length, f.first, f.last, f.change_pct]));
}

function renderCalendar(t) {
  const days = t.calendar, max = Math.max(1, ...days.map(d => d.docs));
  const weeks = []; days.forEach((d, i) => { const w = Math.floor(i / 7); (weeks[w] = weeks[w] || []).push(d); });
  const MON = ['янв', 'фев', 'мар', 'апр', 'май', 'июн', 'июл', 'авг', 'сен', 'окт', 'ноя', 'дек'];
  let lastM = -1;
  const head = weeks.map(w => { const m = new Date(w[0].d).getMonth(); const s = m !== lastM ? MON[m] : ''; lastM = m; return `<span>${s}</span>`; }).join('');
  const grid = weeks.map(w => '<div class="cw">' + w.map(d => `<i data-d="${d.d}" style="background:${seqColor(d.docs, max)}" title="${d.d}: документов ${d.docs}, замечаний ${d.findings}"></i>`).join('') + '</div>').join('');
  const total = days.reduce((a, d) => a + d.docs, 0), active = days.filter(d => d.docs).length;
  const html = `<div class="cal"><div class="cal-days"><span>пн</span><span></span><span>ср</span><span></span><span>пт</span><span></span><span>вс</span></div>`
    + `<div><div class="cal-head">${head}</div><div class="cal-grid">${grid}</div></div></div>`
    + `<div class="legend-seq"><span>меньше</span>${SEQ.filter((_, i) => i % 2 === 0).map(c => `<i style="background:${c}"></i>`).join('')}<span>больше</span>`
    + `<span class="muted" style="margin-left:16px">за год: ${total} документов, активных дней: ${active} · нажмите на день — откроется аналитика за этот день</span></div>`;
  setHtml('t-calendar', html, ['Дата', 'Документов', 'Замечаний'], days.filter(d => d.docs).map(d => [d.d, d.docs, d.findings]));
  $$('#c-t-calendar i[data-d]').forEach(i => i.onclick = () => { location.href = `/analytics?period=custom&from=${i.dataset.d}&to=${i.dataset.d}`; });
}

function renderWeekHour(t) {
  const W = ['Пн', 'Вт', 'Ср', 'Чт', 'Пт', 'Сб', 'Вс'], m = t.weekday_hour, max = Math.max(1, ...m.flat());
  const html = '<div class="tscroll"><table class="heat wh"><thead><tr><th></th>' + Array.from({length: 24}, (_, h) => `<th>${h}</th>`).join('')
    + '<th>Всего</th></tr></thead><tbody>' + m.map((r, wi) => `<tr><td class="hname">${W[wi]}</td>` + r.map((v, h) =>
      `<td style="background:${seqColor(v, max)};color:${seqInk(v, max)}" title="${W[wi]}, ${h}:00–${h}:59 — ${v} док.">${v || ''}</td>`).join('')
      + `<td class="hsum">${r.reduce((a, b) => a + b, 0)}</td></tr>`).join('') + '</tbody></table></div>';
  setHtml('t-weekhour', html, ['День'].concat(Array.from({length: 24}, (_, h) => h + ':00')), m.map((r, i) => [W[i]].concat(r)));
}
