const $ = (id) => document.getElementById(id);
const listEl = $('list');
const emptyEl = $('empty');
const urlEl = $('url');
const addBtn = $('add-btn');
const hintEl = $('hint');

let lastSignature = '';
let settingsTouched = false;

function fmtSize(n) {
  if (!n || n <= 0) return '';
  const u = ['B', 'KB', 'MB', 'GB', 'TB'];
  let i = 0, v = n;
  while (v >= 1024 && i < u.length - 1) { v /= 1024; i++; }
  return v.toFixed(v >= 100 || i === 0 ? 0 : 1) + ' ' + u[i];
}

function fmtSpeed(n) {
  return n > 0 ? fmtSize(n) + '/s' : '';
}

const STATUS_TEXT = {
  waiting: '等待中',
  downloading: '下载中',
  paused: '已暂停',
  done: '已完成',
  error: '失败',
};

function el(tag, cls, text) {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (text !== undefined) e.textContent = text;
  return e;
}

function renderTask(t) {
  const card = el('div', 'card ' + t.status);

  const head = el('div', 'card-head');
  const title = el('div', 'title');
  if (t.site) title.appendChild(el('span', 'badge', t.site));
  if (t.kind === 'video') title.appendChild(el('span', 'badge alt', '视频'));
  title.appendChild(el('span', 'name', t.name || '未命名'));
  head.appendChild(title);

  const pct = t.size > 0 ? Math.min(100, (t.downloaded / t.size) * 100) : (t.status === 'done' ? 100 : 0);
  head.appendChild(el('span', 'pct', pct > 0 ? pct.toFixed(1) + '%' : STATUS_TEXT[t.status] || ''));
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
  if (bits.length === 0) bits.push(STATUS_TEXT[t.status] || '');
  left.textContent = bits.join(' · ');
  meta.appendChild(left);

  const actions = el('div', 'actions');
  const mkBtn = (label, action, cls) => {
    const b = el('button', cls || 'ghost', label);
    b.onclick = () => act(t.id, action);
    return b;
  };
  if (t.status === 'downloading' || t.status === 'waiting') actions.appendChild(mkBtn('暂停', 'pause'));
  if (t.status === 'paused') actions.appendChild(mkBtn('继续', 'resume'));
  if (t.status === 'done') {
    const b = el('button', 'ghost', '打开目录');
    b.onclick = () => openDir(t.id);
    actions.appendChild(b);
  }
  if (t.status === 'error') actions.appendChild(mkBtn('重试', 'resume'));
  actions.appendChild(mkBtn('删除', 'delete', 'ghost danger'));
  meta.appendChild(actions);
  card.appendChild(meta);

  if (t.error) card.appendChild(el('div', 'err', t.error));
  return card;
}

async function act(id, action) {
  try {
    const r = await fetch('/api/action', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ id, action }),
    });
    const j = await r.json();
    if (!j.ok) toast(j.error || '操作失败');
    poll();
  } catch (e) {
    toast('操作失败：' + e.message);
  }
}

async function openDir(id) {
  await fetch('/api/open', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ id }),
  });
}

async function add() {
  const url = urlEl.value.trim();
  if (!url) return;
  addBtn.disabled = true;
  addBtn.textContent = '添加中…';
  try {
    const r = await fetch('/api/add', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ url }),
    });
    const j = await r.json();
    if (j.ok) {
      urlEl.value = '';
      hintEl.textContent = '已添加：' + (j.task.name || url);
      hintEl.className = 'hint ok';
    } else {
      hintEl.textContent = j.error || '添加失败';
      hintEl.className = 'hint bad';
    }
  } catch (e) {
    hintEl.textContent = '添加失败：' + e.message;
    hintEl.className = 'hint bad';
  } finally {
    addBtn.disabled = false;
    addBtn.textContent = '添加';
    poll();
  }
}

function toast(msg) {
  hintEl.textContent = msg;
  hintEl.className = 'hint bad';
}

function applyState(s) {
  // 引擎状态
  const dot = $('engine-dot');
  dot.className = 'dot ' + (s.engine.alive ? 'on' : 'off');
  $('engine-text').textContent = s.engine.alive
    ? `内核 v${s.engine.version || '?'} 已连接`
    : '内核未连接';

  // 设置（不覆盖用户正在编辑的值）
  if (!settingsTouched) {
    $('concurrency').value = s.concurrency;
    $('cookies').value = s.cookies_from_browser || '';
  }
  $('dir-btn').textContent = s.download_dir;

  // 工具状态
  const missing = [];
  if (!s.tools.gopeed) missing.push('内核');
  if (!s.tools.ytdlp) missing.push('yt-dlp');
  const warn = $('tool-warn');
  warn.innerHTML = '';
  if (!s.tools.ffmpeg) {
    const b = el('button', 'ghost warn', '缺少 ffmpeg，点此下载');
    b.onclick = async () => {
      b.disabled = true;
      b.textContent = '下载中…';
      await fetch('/api/install-ffmpeg', { method: 'POST' });
    };
    warn.appendChild(b);
  } else if (missing.length) {
    warn.appendChild(el('span', 'warn-text', '缺少 ' + missing.join('、')));
  }

  // 任务列表
  const sig = JSON.stringify(s.tasks.map(t => [t.id, t.status, t.name, Math.round((t.downloaded / (t.size || 1)) * 1000), t.speed, t.error]));
  if (sig === lastSignature) return;
  lastSignature = sig;

  listEl.innerHTML = '';
  emptyEl.style.display = s.tasks.length ? 'none' : 'block';
  for (const t of s.tasks) listEl.appendChild(renderTask(t));
}

async function poll() {
  try {
    const r = await fetch('/api/state');
    applyState(await r.json());
  } catch (e) {
    $('engine-dot').className = 'dot off';
    $('engine-text').textContent = '界面与后端断开';
  }
}

addBtn.onclick = add;
urlEl.addEventListener('keydown', (e) => { if (e.key === 'Enter') add(); });
$('dir-btn').onclick = () => openDir('');
$('concurrency').addEventListener('change', async (e) => {
  await fetch('/api/settings', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ concurrency: Number(e.target.value) }),
  });
});
$('cookies').addEventListener('change', async (e) => {
  await fetch('/api/settings', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ cookies_from_browser: e.target.value }),
  });
});
['concurrency', 'cookies'].forEach(id => {
  $(id).addEventListener('focus', () => { settingsTouched = true; });
  $(id).addEventListener('blur', () => { settingsTouched = false; });
});

poll();
setInterval(poll, 1000);
