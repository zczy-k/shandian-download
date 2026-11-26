const $ = (id) => document.getElementById(id);
const listEl = $('list');
const emptyEl = $('empty');
const urlEl = $('url');
const addBtn = $('add-btn');
const hintEl = $('hint');

let STATE = null;
let TAB = 'all';
let lastSig = '';
let settingsDirty = false;
let pendingPick = null;

const STATUS_TEXT = {
  waiting: '排队中', downloading: '下载中', paused: '已暂停', done: '已完成', error: '失败',
};

/* ---------- 工具函数 ---------- */

function fmtSize(n) {
  if (!n || n <= 0) return '';
  const u = ['B', 'KB', 'MB', 'GB', 'TB'];
  let i = 0, v = n;
  while (v >= 1024 && i < u.length - 1) { v /= 1024; i++; }
  return v.toFixed(v >= 100 || i === 0 ? 0 : 1) + ' ' + u[i];
}

const fmtSpeed = (n) => (n > 0 ? fmtSize(n) + '/s' : '');

function el(tag, cls, text) {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (text !== undefined) e.textContent = text;
  return e;
}

function hint(msg, kind) {
  hintEl.textContent = msg || '';
  hintEl.className = 'hint' + (kind ? ' ' + kind : '');
}

async function api(path, body, timeoutMs = 90000, retries = 1) {
  const ctrl = new AbortController();
  const timer = setTimeout(() => ctrl.abort(), timeoutMs);
  try {
    const r = await fetch(path, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body || {}),
      signal: ctrl.signal,
    });
    const text = await r.text();
    return text ? JSON.parse(text) : {};
  } catch (e) {
    if (e.name === 'AbortError') {
      throw new Error('请求超时（' + Math.round(timeoutMs / 1000) + ' 秒）');
    }
    // 连接被回收 / 服务刚重启时重试一次
    if (retries > 0) {
      await new Promise(r => setTimeout(r, 250));
      return api(path, body, timeoutMs, retries - 1);
    }
    throw new Error('连不上本机服务，请确认程序仍在运行（' + e.message + '）');
  } finally {
    clearTimeout(timer);
  }
}

function guessKind(u) {
  const s = (u || '').trim().toLowerCase();
  if (s.startsWith('magnet:?')) return 'magnet';
  if (s.startsWith('ed2k://')) return 'ed2k';
  if (s.endsWith('.torrent')) return 'torrent';
  return 'other';
}

/* ---------- 任务渲染 ---------- */

function taskCard(t) {
  const card = el('div', 'card ' + t.status);

  const head = el('div', 'card-head');
  const title = el('div', 'title');
  if (t.site) title.appendChild(el('span', 'badge', t.site));
  if (t.protocol === 'bt') title.appendChild(el('span', 'badge alt', 'BT'));
  if (t.kind === 'video') title.appendChild(el('span', 'badge alt', '视频'));
  title.appendChild(el('span', 'name', t.name || '未命名'));
  head.appendChild(title);

  const pct = t.size > 0 ? Math.min(100, (t.downloaded / t.size) * 100)
    : (t.status === 'done' ? 100 : 0);
  let label = pct > 0 ? pct.toFixed(1) + '%' : (STATUS_TEXT[t.status] || '');
  if (!pct && t.note && !t.size) label = '找种子中';
  head.appendChild(el('span', 'pct', label));
  card.appendChild(head);

  const bar = el('div', 'bar');
  const fill = el('div', 'fill');
  fill.style.width = Math.max(pct, t.status === 'done' ? 100 : 0) + '%';
  bar.appendChild(fill);
  card.appendChild(bar);

  const meta = el('div', 'meta');
  const left = el('span', '');
  const bits = [];
  if (t.downloaded > 0) bits.push(fmtSize(t.downloaded) + (t.size > 0 ? ' / ' + fmtSize(t.size) : ''));
  if (t.speed > 0) bits.push(fmtSpeed(t.speed));
  if (t.note) bits.push(t.note);
  if (!bits.length) bits.push(STATUS_TEXT[t.status] || '');
  left.textContent = bits.join(' · ');
  meta.appendChild(left);

  const actions = el('div', 'actions');
  const mk = (label, fn, cls) => {
    const b = el('button', cls || 'ghost', label);
    b.onclick = fn;
    return b;
  };
  if (t.status === 'downloading' || t.status === 'waiting') actions.appendChild(mk('暂停', () => act(t.id, 'pause')));
  if (t.status === 'paused') actions.appendChild(mk('继续', () => act(t.id, 'resume')));
  if (t.status === 'error') actions.appendChild(mk('重试', () => act(t.id, 'resume')));
  if (t.status === 'done' || t.status === 'downloading') {
    actions.appendChild(mk('打开目录', () => openDir(t.id)));
  }
  actions.appendChild(mk('删除', () => act(t.id, 'delete'), 'ghost danger'));
  meta.appendChild(actions);
  card.appendChild(meta);

  if (t.error) card.appendChild(el('div', 'err', t.error));
  return card;
}

function recycleCard(item) {
  const card = el('div', 'card');
  const head = el('div', 'card-head');
  const title = el('div', 'title');
  title.appendChild(el('span', 'name', item.name || item.url || '已删除任务'));
  head.appendChild(title);
  head.appendChild(el('span', 'pct', '已移除'));
  card.appendChild(head);
  const meta = el('div', 'meta');
  meta.appendChild(el('span', '', item.dir || ''));
  const actions = el('div', 'actions');
  const b = el('button', 'ghost', '重新下载');
  b.onclick = () => reAdd(item.url);
  actions.appendChild(b);
  meta.appendChild(actions);
  card.appendChild(meta);
  return card;
}

function visibleTasks() {
  const all = (STATE && STATE.tasks) || [];
  if (TAB === 'active') return all.filter(t => ['downloading', 'waiting', 'paused'].includes(t.status));
  if (TAB === 'done') return all.filter(t => t.status === 'done');
  if (TAB === 'error') return all.filter(t => t.status === 'error');
  return all;
}

/* ---------- 操作 ---------- */

async function act(id, action) {
  try {
    const j = await api('/api/action', { id, action }, 30000);
    if (!j.ok) hint(j.error || '操作失败', 'bad');
    poll();
  } catch (e) {
    hint('操作失败：' + e.message, 'bad');
  }
}

async function openDir(id) {
  try { await api('/api/open', { id }, 15000); } catch (e) { hint(e.message, 'bad'); }
}

async function reAdd(url) {
  urlEl.value = url || '';
  await add();
}

async function doAdd(url, selectFiles) {
  const j = await api('/api/add', { url, selectFiles }, 120000);
  if (j.ok) {
    hint('已添加', 'ok');
    urlEl.value = '';
    poll();
  } else {
    hint(j.error || '添加失败', 'bad');
  }
}

async function add() {
  const url = urlEl.value.trim();
  if (!url) return;
  addBtn.disabled = true;
  addBtn.textContent = '添加中…';
  try {
    const kind = guessKind(url);
    if (kind === 'magnet' || kind === 'torrent') {
      hint('解析元数据中…', '');
      let resolved = null;
      try {
        resolved = await api('/api/resolve', { url }, 180000);
      } catch (e) {
        resolved = null;
      }
      if (resolved && resolved.ok && (resolved.files || []).length > 1) {
        showFilePicker(url, resolved);
        return;
      }
    }
    await doAdd(url);
  } catch (e) {
    hint('添加失败：' + e.message, 'bad');
  } finally {
    addBtn.disabled = false;
    addBtn.textContent = '添加';
  }
}

/* ---------- 选文件 ---------- */

function showFilePicker(url, resolved) {
  pendingPick = { url, files: resolved.files };
  $('files-title').textContent = resolved.name || '选择要下载的文件';
  const box = $('files-list');
  box.innerHTML = '';
  resolved.files.forEach((f, i) => {
    const row = el('label', 'file-row');
    const cb = el('input');
    cb.type = 'checkbox';
    cb.checked = true;
    cb.dataset.index = String(f.index);
    cb.dataset.size = String(f.size || 0);
    row.appendChild(cb);
    row.appendChild(el('span', 'file-name', f.name || `文件 ${i + 1}`));
    row.appendChild(el('span', 'file-size', fmtSize(f.size)));
    box.appendChild(row);
  });
  $('files-all').checked = true;
  updateFileSum();
  $('files').classList.remove('hidden');
}

function updateFileSum() {
  const boxes = [...$('files-list').querySelectorAll('input[type=checkbox]')];
  const picked = boxes.filter(b => b.checked);
  const total = picked.reduce((s, b) => s + Number(b.dataset.size || 0), 0);
  $('files-sum').textContent = `已选 ${picked.length} / ${boxes.length} 个，共 ${fmtSize(total)}`;
}

async function confirmFiles() {
  if (!pendingPick) return;
  const boxes = [...$('files-list').querySelectorAll('input[type=checkbox]')];
  const picked = boxes.filter(b => b.checked).map(b => Number(b.dataset.index));
  const all = boxes.length;
  $('files').classList.add('hidden');
  addBtn.disabled = true;
  addBtn.textContent = '添加中…';
  try {
    await doAdd(pendingPick.url, picked.length === all ? null : picked);
  } catch (e) {
    hint('添加失败：' + e.message, 'bad');
  } finally {
    addBtn.disabled = false;
    addBtn.textContent = '添加';
    pendingPick = null;
  }
}

/* ---------- 设置 ---------- */

const TEXT_FIELDS = ['dir_movie', 'dir_video', 'dir_other', 'cookies_from_browser', 'language'];
const NUM_FIELDS = ['max_running', 'limit_kb', 'concurrency', 'schedule_from', 'schedule_to'];
const BOOL_FIELDS = ['remember_dir', 'notify', 'done_sound', 'watch_clipboard', 'confirm_new',
  'prevent_sleep', 'launch_at_login', 'auto_update_check', 'schedule_on'];

function fillSettings(cfg) {
  if (settingsDirty) return;
  TEXT_FIELDS.forEach(k => { if ($(k)) $(k).value = cfg[k] ?? ''; });
  NUM_FIELDS.forEach(k => { if ($(k)) $(k).value = cfg[k] ?? 0; });
  BOOL_FIELDS.forEach(k => { if ($(k)) $(k).checked = !!cfg[k]; });
}

async function saveSettings() {
  const body = {};
  TEXT_FIELDS.forEach(k => { if ($(k)) body[k] = $(k).value; });
  NUM_FIELDS.forEach(k => { if ($(k)) body[k] = Number($(k).value) || 0; });
  BOOL_FIELDS.forEach(k => { if ($(k)) body[k] = $(k).checked; });
  try {
    const j = await api('/api/settings', body, 30000);
    if (j.ok) hint('设置已保存', 'ok');
    else hint(j.error || '保存失败', 'bad');
    settingsDirty = false;
  } catch (e) {
    hint('保存失败：' + e.message, 'bad');
  }
}

/* ---------- 状态应用 ---------- */

function applyState(s) {
  STATE = s;

  $('engine-dot').className = 'dot ' + (s.engine.alive ? 'on' : 'off');
  $('engine-text').textContent = s.engine.alive ? `内核 v${s.engine.version || '?'} 已连接` : '内核未连接';

  const gw = $('gw-badge');
  gw.className = 'badge ' + (s.gateway.running ? '' : 'alt');
  gw.textContent = s.gateway.running ? '扩展 :6800' : '扩展未开';

  if (s.clipboard && s.clipboard.length) {
    $('clip-text').textContent = `检测到 ${s.clipboard.length} 个链接：${s.clipboard[0].slice(0, 70)}`;
    $('clip').classList.remove('hidden');
    $('clip').dataset.links = JSON.stringify(s.clipboard);
  } else {
    $('clip').classList.add('hidden');
  }

  fillSettings(s.config);
  $('ver').textContent = `v${s.version} · ${s.tools.ffmpeg ? 'ffmpeg 已就绪' : '缺 ffmpeg'}`;

  const list = visibleTasks();
  const recycle = TAB === 'recycle' ? (s.recycle || []) : [];
  const sig = JSON.stringify([TAB, list.map(t => [t.id, t.status, t.name,
    Math.round((t.downloaded / (t.size || 1)) * 1000), t.speed, t.error, t.note]), recycle.length]);
  if (sig === lastSig) return;
  lastSig = sig;

  listEl.innerHTML = '';
  const count = list.length + recycle.length;
  emptyEl.style.display = count ? 'none' : 'block';
  if (TAB === 'recycle') {
    recycle.forEach(item => listEl.appendChild(recycleCard(item)));
  } else {
    list.forEach(t => listEl.appendChild(taskCard(t)));
  }
}

async function poll() {
  const ctrl = new AbortController();
  const timer = setTimeout(() => ctrl.abort(), 8000);
  try {
    const r = await fetch('/api/state', { signal: ctrl.signal, cache: 'no-store' });
    applyState(await r.json());
  } catch (e) {
    $('engine-dot').className = 'dot off';
    $('engine-text').textContent = '界面与后端断开';
  } finally {
    clearTimeout(timer);
  }
}

/* ---------- 事件绑定 ---------- */

addBtn.onclick = add;
urlEl.addEventListener('keydown', e => { if (e.key === 'Enter') add(); });

document.querySelectorAll('.tab').forEach(btn => {
  btn.onclick = () => {
    document.querySelectorAll('.tab').forEach(b => b.classList.remove('active'));
    btn.classList.add('active');
    TAB = btn.dataset.tab;
    lastSig = '';
    poll();
  };
});

$('settings-btn').onclick = () => $('panel').classList.add('open');
$('panel-close').onclick = () => $('panel').classList.remove('open');

[...TEXT_FIELDS, ...NUM_FIELDS, ...BOOL_FIELDS].forEach(k => {
  const e = $(k);
  if (!e) return;
  e.addEventListener('focus', () => { settingsDirty = true; });
  e.addEventListener('change', () => { settingsDirty = true; saveSettings(); });
});

$('files-close').onclick = () => { $('files').classList.add('hidden'); pendingPick = null; };
$('files-all').onchange = (e) => {
  $('files-list').querySelectorAll('input[type=checkbox]').forEach(b => { b.checked = e.target.checked; });
  updateFileSum();
};
$('files-list').addEventListener('change', updateFileSum);
$('files-ok').onclick = confirmFiles;

$('clip-add').onclick = async () => {
  const links = JSON.parse($('clip').dataset.links || '[]');
  if (!links.length) return;
  try {
    const j = await api('/api/add-many', { urls: links }, 60000);
    if (j.added) hint(`已添加 ${j.added} 个任务`, 'ok');
    else hint((j.errors && j.errors[0]) || '没有可添加的链接', 'bad');
    await api('/api/clipboard', {}, 8000);
    poll();
  } catch (e) {
    hint(e.message, 'bad');
  }
};
$('clip-close').onclick = async () => {
  await api('/api/clipboard', {}, 8000);
  $('clip').classList.add('hidden');
};

$('diag-btn').onclick = async () => {
  try {
    const j = await api('/api/diagnostics', {}, 30000);
    hint(j.ok ? '诊断已导出：' + j.path : (j.error || '导出失败'), j.ok ? 'ok' : 'bad');
  } catch (e) { hint(e.message, 'bad'); }
};

$('update-btn').onclick = async () => {
  hint('检查中…');
  try {
    const r = await fetch('/api/update', { cache: 'no-store' });
    const j = await r.json();
    if (j.ok) hint(`当前 v${j.latest}，Release 已发布${j.url ? '，可在 GitHub 下载' : ''}`, 'ok');
    else hint('检查失败：' + (j.error || ''), 'bad');
  } catch (e) { hint(e.message, 'bad'); }
};

$('ffmpeg-btn').onclick = async () => {
  try {
    const j = await api('/api/install-ffmpeg', {}, 15000);
    hint(j.ok ? '已开始处理 ffmpeg，完成后刷新页面' : '失败', j.ok ? 'ok' : 'bad');
  } catch (e) { hint(e.message, 'bad'); }
};

$('gw-info').onclick = () => {
  const tok = (STATE && STATE.engine_token) || '';
  hint(`扩展接口 http://127.0.0.1:6800 ｜ LinkSwift/aria2 密钥：${tok}`, 'ok');
};

poll();
setInterval(poll, 1200);
