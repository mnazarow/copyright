/* Корректор — клиентская логика: загрузка, ход проверки, работа с замечаниями, настройки. */
function $(s, r) { return (r || document).querySelector(s); }
function $$(s, r) { return Array.from((r || document).querySelectorAll(s)); }
function esc(s) { return String(s).replace(/[&<>"]/g, c => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;'}[c])); }

/* ---------- загрузка ---------- */
(function () {
  const drop = $('#drop'); if (!drop) return;
  const input = $('#fileinput'), list = $('#filelist'), btn = $('#upbtn'), hint = $('#uphint');
  const dt = new DataTransfer();
  function render() {
    list.innerHTML = '';
    Array.from(dt.files).forEach(f => {
      const li = document.createElement('li');
      li.innerHTML = `<span>${esc(f.name)}</span><span class="muted">${(f.size / 1048576).toFixed(1)} МБ</span>`;
      list.appendChild(li);
    });
    input.files = dt.files;
    btn.disabled = dt.files.length === 0;
    hint.textContent = dt.files.length ? `Файлов: ${dt.files.length}` + (dt.files.length > 1 ? ' — будет создан пакет' : '') : 'Выберите файл';
  }
  function add(files) { Array.from(files).forEach(f => { if (/\.(pdf|zip|docx?|odt|rtf)$/i.test(f.name)) dt.items.add(f); }); render(); }
  drop.addEventListener('click', e => { if (e.target.tagName !== 'INPUT') input.click(); });
  input.addEventListener('change', () => { const fs = Array.from(input.files); dt.items.clear(); add(fs); });
  ['dragenter', 'dragover'].forEach(ev => drop.addEventListener(ev, e => { e.preventDefault(); drop.classList.add('over'); }));
  ['dragleave', 'drop'].forEach(ev => drop.addEventListener(ev, e => { e.preventDefault(); drop.classList.remove('over'); }));
  drop.addEventListener('drop', e => add(e.dataTransfer.files));
  // «Дополнительно» свернуто: если профиль или режим выбраны не «как в настройках» или заполнена строка в шапку
  // отчета, рядом с заголовком — «изменено»
  const more = $('#upmore'), mark = $('#upmore-changed');
  if (more && mark) {
    const sel = $$('select, input', more);
    const upd = () => { mark.hidden = !sel.some(s => s.value.trim()); };
    sel.forEach(s => { s.addEventListener('change', upd); s.addEventListener('input', upd); });
    upd();                                       // браузер мог восстановить выбор при возврате на страницу
  }
  $('#upform').addEventListener('submit', e => {
    e.preventDefault();
    const fd = new FormData(e.target), xhr = new XMLHttpRequest(), bar = $('#upprog');
    bar.style.display = 'block'; btn.disabled = true; hint.textContent = 'Загрузка…';
    xhr.upload.onprogress = ev => { if (ev.lengthComputable) $('i', bar).style.width = (ev.loaded / ev.total * 100) + '%'; };
    xhr.onload = () => {
      if (xhr.status < 400) { location.href = xhr.responseURL; return; }
      let msg = xhr.responseText;
      try { const j = JSON.parse(xhr.responseText); msg = j.error || j.detail || msg; } catch (e) {}
      if (xhr.status === 413 && !/МБ|файл/i.test(msg)) msg = 'файл слишком большой для сервера (ограничение веб-сервера или настройки «Максимальный размер файла»)';
      hint.textContent = 'Ошибка: ' + String(msg).slice(0, 400); btn.disabled = false; bar.style.display = 'none';
    };
    xhr.onerror = () => { hint.textContent = 'Нет связи с сервером — проверьте сеть и повторите загрузку'; btn.disabled = false; bar.style.display = 'none'; };
    xhr.open('POST', '/upload'); xhr.send(fd);
  });
})();

/* ---------- список заданий ---------- */
function pollJobsTable(reloadWhenDone) {
  const rows = $$('#jobs-table tr[data-job]');
  const active = rows.filter(r => ['queued', 'running', 'uploading'].includes(r.dataset.status));
  if (!active.length) return;
  setTimeout(async () => {
    let changed = false;
    for (const r of active) {
      const j = await fetch('/api/jobs/' + r.dataset.job).then(x => x.json()).catch(() => null);
      if (!j) continue;
      if (j.status !== r.dataset.status) changed = true;
      $('.js-status', r).innerHTML = j.status === 'running' ? `<span class="badge warn" title="${esc(j.step || '')}">идет · ${Math.round(j.progress * 100)}%</span>` : $('.js-status', r).innerHTML;
      if (j.status === 'done' || j.status === 'failed') changed = true;
    }
    if (changed) location.reload(); else pollJobsTable(reloadWhenDone);
  }, 2500);
}

/* ---------- страница задания ---------- */
function markCell(td) {
  const raw = td.textContent, mark = td.dataset.mark || '';
  const parts = mark.split(' … ').map(s => s.trim()).filter(s => s && !['—', '-', '–'].includes(s));
  let html = esc(raw);
  parts.forEach(p => { const e = esc(p); const i = html.indexOf(e); if (i >= 0) html = html.slice(0, i) + '<mark>' + e + '</mark>' + html.slice(i + e.length); });
  td.innerHTML = html;
}
function closeViewer() { $('#viewer').classList.remove('on'); }
function closeEditor() { $('#editor').classList.remove('on'); }
/* журнал обработки: строки с подсветкой по значкам (▶ этап, ✓ готово, … ожидание, ⚠ предупреждение, ✗ ошибка) */
function logClass(t) {
  if (/✗|⛔|ОШИБКА/.test(t)) return 'l-err';
  if (/⚠/.test(t)) return 'l-warn';
  if (/^\d\d:\d\d:\d\d (▶|■)/.test(t)) return 'l-stage';
  if (/✓/.test(t)) return 'l-ok';
  if (/ … /.test(t)) return 'l-beat';
  return '';
}
function renderLog(pre, text) {
  const frag = document.createDocumentFragment(), lines = text.split('\n');
  if (lines[lines.length - 1] === '') lines.pop();
  lines.forEach(ln => { const sp = document.createElement('span'); sp.className = logClass(ln); sp.textContent = ln + '\n'; frag.appendChild(sp); });
  pre.appendChild(frag);
  while (pre.childNodes.length > 6000) pre.removeChild(pre.firstChild);    // браузер не тормозит на длинных журналах
}
function initJob(id) {
  const pre = $('#joblog');
  if (pre) { const t = pre.textContent; pre.textContent = ''; renderLog(pre, t); pre.scrollTop = pre.scrollHeight; }
  const run = $('#running');
  if (run) {
    let offset = pre ? parseInt(pre.dataset.offset || '0', 10) : 0, fails = 0;
    const info = $('#run-info');
    const tick = async () => {
      let j = null;
      try {
        const r = await fetch(`/api/jobs/${id}/log?offset=${offset}`, {headers: {'Accept': 'application/json'}});
        if (r.status === 404) { location.href = '/jobs'; return; }
        if (r.status === 401) { location.reload(); return; }
        if (r.ok) j = await r.json();
      } catch (e) { j = null; }
      if (!j) {
        fails++;
        info.innerHTML = `<span class="warn-t">Нет связи с сервером (попытка ${fails})</span> — страница продолжит обновляться сама.`;
        setTimeout(tick, Math.min(15000, 1500 * fails)); return;
      }
      fails = 0;
      if (pre) {
        if (j.reset) pre.textContent = '';
        if (j.text) {
          const follow = !$('#log-follow') || $('#log-follow').checked;
          renderLog(pre, j.text);
          if (follow) pre.scrollTop = pre.scrollHeight;
        }
      }
      offset = j.offset;
      const pct = Math.round((j.progress || 0) * 100);
      $('#bar').style.width = pct + '%';
      $('#step').textContent = (j.step || 'В очереди') + (j.status === 'running' ? ' · ' + pct + '%' : '');
      if (j.stopping) info.innerHTML = '<span class="warn-t">Останавливается…</span> Запрос к модели прерывается, это занимает 1–2 секунды.';
      else if (j.status === 'queued') info.textContent = 'В очереди: проверка начнется, когда освободится обработчик.';
      else if (j.idle != null && j.idle > 90) info.innerHTML = `<span class="warn-t">Нет признаков работы ${Math.round(j.idle)} с.</span> ` +
        'Если так продолжится, остановите задание и проверьте документ заново; причины — в журнале ниже.';
      else info.textContent = j.idle != null ? `Выполняется · последний признак работы ${Math.round(j.idle)} с назад` : '';
      if (j.status === 'done' || j.status === 'failed') { setTimeout(() => location.reload(), 800); return; }
      setTimeout(tick, document.hidden ? 5000 : 1500);
    };
    tick();
  }
  $$('#findings td.f-src, #findings td.f-fix').forEach(markCell);
  const apply = () => {
    const t = $('#flt-type').value, s = $('#flt-status').value, src = $('#flt-src').value, q = $('#flt-q').value.toLowerCase();
    const groups = {rules: r => !['spell', 'ai', 'table', 'toc', 'ref', 'requisites', 'batch'].includes(r), spell: r => r === 'spell' || r === 'lt',
      ai: r => r === 'ai', data: r => ['table', 'toc', 'ref', 'requisites', 'batch'].includes(r)};
    let n = 0;
    $$('#findings tr[data-id]').forEach(tr => {
      const ok = (!t || tr.dataset.type === t) && (!s || tr.dataset.status === s) && (!src || groups[src](tr.dataset.rule)) &&
        (!q || tr.textContent.toLowerCase().includes(q));
      tr.style.display = ok ? '' : 'none'; if (ok) n++;
    });
    $('#flt-n').textContent = 'Показано: ' + n;
  };
  ['#flt-type', '#flt-status', '#flt-src'].forEach(s => $(s) && $(s).addEventListener('change', apply));
  $('#flt-q') && $('#flt-q').addEventListener('input', apply);
  if ($('#findings')) apply();
  $$('#findings .pg, .pgx').forEach(a => a.addEventListener('click', e => {
    e.preventDefault();
    $('#viewer-img').src = `/job/${id}/page/${a.dataset.page}.png`;
    $('#viewer-title').textContent = 'Страница ' + a.dataset.page;
    $('#viewer').classList.add('on');
  }));
  $('#viewer') && $('#viewer').addEventListener('click', e => { if (e.target.id === 'viewer') closeViewer(); });
  let editing = null;
  $$('#findings .acts button').forEach(b => b.addEventListener('click', async () => {
    const tr = b.closest('tr'), fid = tr.dataset.id, act = b.dataset.act;
    if (act === 'edit') {
      editing = tr;
      $('#e-type').value = tr.dataset.type; $('#e-fix').value = $('.f-fix', tr).textContent;
      $('#e-good').value = $('.f-fix', tr).dataset.mark; $('#e-note').value = $('.f-note', tr).textContent;
      $('#editor').classList.add('on'); return;
    }
    const status = tr.dataset.status === act ? 'new' : act;
    const resp = await fetch(`/api/jobs/${id}/findings/${fid}`, {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({status})})
      .catch(() => null);
    if (!resp || !resp.ok) {
      const e = resp ? await resp.json().catch(() => ({})) : {};
      alert('Отметка не сохранена: ' + (e.error || e.detail || 'нет связи с сервером')); return;
    }
    tr.dataset.status = status; tr.className = status;
  }));
  $('#e-save') && $('#e-save').addEventListener('click', async () => {
    const tr = editing, body = {type: $('#e-type').value, fix: $('#e-fix').value, good: $('#e-good').value, note: $('#e-note').value, status: 'accepted'};
    if (body.type !== 'Требует проверки автором' && (!body.good || !body.fix.includes(body.good))) {
      alert('Исправленный фрагмент должен дословно входить в текст «Как правильно» — иначе отчет не пройдет машинную проверку.'); return;
    }
    if (body.type !== 'Требует проверки автором' && body.fix.trim() === $('.f-src', tr).textContent.trim()) {
      alert('«Как правильно» совпадает с цитатой — исправления нет. Выберите тип «Требует проверки автором» или измените текст.'); return;
    }
    if (!body.note.trim()) { alert('Напишите пояснение: почему это ошибка.'); return; }
    if (body.type === 'Требует проверки автором') { body.fix = '—'; body.good = '—'; }
    const resp = await fetch(`/api/jobs/${id}/findings/${tr.dataset.id}`, {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body)});
    if (!resp.ok) { const e = await resp.json().catch(() => ({})); alert(e.detail || e.error || 'Не сохранено'); return; }
    tr.dataset.type = body.type; tr.children[2].textContent = body.type;
    const fx = $('.f-fix', tr); fx.textContent = body.fix; fx.dataset.mark = body.good; markCell(fx);
    $('.f-note', tr).textContent = body.note; tr.dataset.status = 'accepted'; tr.className = 'accepted';
    closeEditor();
  });
  $('#rebuild') && $('#rebuild').addEventListener('click', async () => {
    const msg = $('#rebuild-msg'); msg.textContent = 'Сборка отчета и 25 проверок…';
    const sm = $('#summary'), body = {};
    if (sm && sm.value !== sm.dataset.orig) body.summary = sm.value;     // итог отправляется, только если его изменили
    let resp = null, r = {};
    try {
      resp = await fetch(`/api/jobs/${id}/rebuild`, {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body)});
      r = await resp.json().catch(() => ({}));
    } catch (e) { msg.textContent = 'Нет связи с сервером — отчет не пересобран'; return; }
    if (!resp.ok) { msg.textContent = 'Отчет не пересобран: ' + (r.error || r.detail || resp.status); return; }
    msg.textContent = r.ok ? `Готово: замечаний в отчете — ${r.count}, проверка ${r.passed}/${r.total} PASS` : `Проверка ${r.passed}/${r.total}: есть нарушения — см. список проверок`;
    setTimeout(() => location.reload(), 1200);
  });
}

/* ---------- настройки ---------- */
function initSettings() {
  $$('.setting .toggle').forEach(b => b.addEventListener('click', () => {
    const s = b.closest('.setting'); s.classList.toggle('open'); b.textContent = s.classList.contains('open') ? 'Свернуть ▴' : 'Подробнее ▾';
  }));
  const form = $('#sform');
  form.addEventListener('input', e => { const s = e.target.closest('.setting'); if (s) s.classList.add('changed'); $('#dirty').textContent = 'Есть несохраненные изменения'; });
  $('#s-search').addEventListener('input', e => {
    const q = e.target.value.toLowerCase().trim();
    $$('.setting').forEach(s => { s.style.display = !q || s.dataset.search.includes(q) ? '' : 'none'; if (q && s.dataset.search.includes(q)) s.classList.add('open'); });
    $$('.group').forEach(g => { const any = $$('.setting', g).some(s => s.style.display !== 'none'); g.style.display = any || !q || !$$('.setting', g).length ? '' : 'none'; });
  });
  const links = $$('.side a[data-g]');
  window.addEventListener('scroll', () => {
    let cur = null; $$('.group').forEach(g => { if (g.getBoundingClientRect().top < 140) cur = g.id; });
    links.forEach(a => a.classList.toggle('on', a.dataset.g === cur));
  });
  window.addEventListener('beforeunload', e => { if ($('#dirty').textContent) { e.preventDefault(); e.returnValue = ''; } });
  form.addEventListener('submit', () => { $('#dirty').textContent = ''; });
  const h = decodeURIComponent(location.hash || '');
  if (h.startsWith('#set-')) {
    const el = document.getElementById(h.slice(1));
    if (el) { el.classList.add('open', 'focus'); setTimeout(() => el.scrollIntoView({block: 'center'}), 50); }
  }
}
async function testLdap(btn) {
  const out = btn.nextElementSibling, fd = new FormData();
  fd.append('login', $('#ldap-login').value); fd.append('password', $('#ldap-pw').value);
  out.textContent = 'Проверка…';
  const r = await fetch('/settings/test-ldap', {method: 'POST', body: fd}).then(x => x.json()).catch(e => ({ok: false, error: String(e)}));
  out.textContent = r.ok ? '✅ ' + r.answer : '❌ ' + r.error;
}
async function testConn(url, btn, out) {
  out = out || btn.nextElementSibling;
  out.textContent = url.startsWith('/settings/test') ? 'Проверка… (используются сохраненные настройки)' : 'Выполняется…';
  btn.disabled = true;
  let r;
  try {
    const resp = await fetch(url, {method: 'POST', headers: {'Accept': 'application/json'}});
    r = await resp.json().catch(() => ({ok: false, error: `ответ сервера ${resp.status} ${resp.statusText}`}));
    if (!resp.ok && r.ok === undefined) r = {ok: false, error: r.error || r.detail || `ошибка ${resp.status}`};
  } catch (e) { r = {ok: false, error: 'нет связи с сервером: ' + e}; }
  btn.disabled = false;
  out.textContent = r.ok ? '✅ ' + (r.answer || 'связь есть') : '❌ ' + (r.error || r.detail || 'ошибка');
}
async function restoreBackup(name, btn) {
  const word = prompt('Восстановить данные из копии ' + name + '?\n\nЗадания, пользователи, настройки и журнал будут заменены ' +
    'содержимым копии. Текущие данные сначала будут сохранены отдельной копией.\n\nДля подтверждения введите слово ВОССТАНОВИТЬ:');
  if (word === null) return;
  const out = btn.parentElement.querySelector('.bk-out'); out.textContent = 'Восстановление… не закрывайте страницу';
  const fd = new FormData(); fd.append('confirm', word.trim());
  const r = await fetch('/settings/backups/' + encodeURIComponent(name) + '/restore', {method: 'POST', body: fd})
    .then(x => x.json()).catch(e => ({ok: false, error: String(e)}));
  out.textContent = r.ok ? '✅ ' + r.answer : '❌ ' + r.error;
}
async function verifyBackup(name, btn) {
  const out = btn.parentElement.querySelector('.bk-out'); out.textContent = 'Проверка целостности…';
  const r = await fetch('/settings/backups/' + encodeURIComponent(name) + '/verify', {method: 'POST'})
    .then(x => x.json()).catch(e => ({ok: false, answer: String(e)}));
  out.textContent = (r.ok ? '✅ ' : '❌ ') + r.answer;
}
async function genToken(btn) {
  if (!confirm('Создать новый токен API? Старый перестанет работать.')) return;
  const r = await fetch('/settings/token', {method: 'POST'}).then(x => x.json());
  prompt('Новый токен (скопируйте, он больше не будет показан):', r.token);
  location.reload();
}

/* ---------- тема ---------- */
(function () {
  const b = document.getElementById('theme-tg'); if (!b) return;
  const order = [null, 'dark', 'light'], names = {null: 'как в системе', dark: 'темная', light: 'светлая'};
  let cur = null; try { cur = localStorage.getItem('korrektor-theme'); } catch (e) {}
  b.title = 'Тема: ' + names[cur] + ' (нажмите, чтобы сменить)';
  b.addEventListener('click', () => {
    const next = order[(order.indexOf(cur) + 1) % order.length];
    try { next ? localStorage.setItem('korrektor-theme', next) : localStorage.removeItem('korrektor-theme'); } catch (e) {}
    location.reload();
  });
})();
