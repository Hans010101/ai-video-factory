'use strict';

const S = { tools: [], summary: null, current: null, jobs: [], keys: [] };
const $ = (s, r = document) => r.querySelector(s);
const $$ = (s, r = document) => [...r.querySelectorAll(s)];
const esc = (s) => String(s ?? '').replace(/[&<>"']/g, c =>
  ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

function toast(msg, isErr) {
  const t = $('#toast');
  t.textContent = msg;
  t.className = 'toast on' + (isErr ? ' err' : '');
  clearTimeout(t._t);
  t._t = setTimeout(() => (t.className = 'toast'), 3200);
}

async function api(path, opts) {
  const r = await fetch(path, opts);
  if (!r.ok) {
    let detail = r.statusText;
    try { detail = (await r.json()).detail || detail; } catch (e) {}
    throw new Error(detail);
  }
  return r.json();
}

/* ---------------- tabs ---------------- */
$('#tabs').addEventListener('click', e => {
  const b = e.target.closest('button[data-tab]');
  if (!b) return;
  $$('#tabs button').forEach(x => x.classList.toggle('on', x === b));
  $$('.tab').forEach(s => s.classList.toggle('on', s.id === 'tab-' + b.dataset.tab));
  ({ queue: loadJobs, outputs: loadOutputs, tools: renderAllTools,
     keys: loadKeys, doctor: loadDoctor }[b.dataset.tab] || (() => {}))();
});

/* ---------------- catalog ---------------- */
async function loadCatalog(refresh) {
  const d = await api('/api/catalog' + (refresh ? '?refresh=true' : ''));
  S.tools = d.tools;
  S.summary = d.summary;
  $('#pillTools').textContent = `${d.summary.available}/${d.summary.total}`;
  renderPicker();
  renderAllTools();
}

function renderPicker(filter = '') {
  const q = filter.trim().toLowerCase();
  const groups = {};
  for (const t of S.tools) {
    if (q && !(`${t.name} ${t.provider} ${t.capability_label} ${t.capability}`.toLowerCase().includes(q))) continue;
    (groups[t.capability_label] ||= []).push(t);
  }
  const keys = Object.keys(groups).sort((a, b) => groups[b].length - groups[a].length);
  $('#capList').innerHTML = keys.map(cap => {
    const items = groups[cap].sort((a, b) => (b.available - a.available) || a.name.localeCompare(b.name));
    const avail = items.filter(i => i.available).length;
    return `<details class="capgroup" ${q || avail ? 'open' : ''}>
      <summary>${esc(cap)}<span class="n">${avail}/${items.length}</span></summary>
      <div class="items">${items.map(t => `
        <button class="titem ${t.available ? '' : 'blocked'} ${t.degraded ? 'degraded' : ''} ${S.current === t.name ? 'on' : ''}"
                data-tool="${esc(t.name)}" title="${esc(t.blocked_reason || t.name)}">
          <span class="s"></span><span class="nm">${esc(t.name)}</span>
        </button>`).join('')}</div></details>`;
  }).join('') || '<p style="padding:12px;color:var(--ink-3);font-size:.85rem">没有匹配的工具</p>';
}

$('#toolSearch').addEventListener('input', e => renderPicker(e.target.value));
$('#capList').addEventListener('click', e => {
  const b = e.target.closest('[data-tool]');
  if (b) selectTool(b.dataset.tool);
});

/* ---------------- tool workspace ---------------- */
function selectTool(name) {
  S.current = name;
  const t = S.tools.find(x => x.name === name);
  if (!t) return;
  $$('.titem').forEach(b => b.classList.toggle('on', b.dataset.tool === name));

  const props = (t.input_schema && t.input_schema.properties) || {};
  const required = (t.input_schema && t.input_schema.required) || [];
  const fieldNames = Object.keys(props);

  $('#workspace').innerHTML = `
    <div class="whead">
      <div>
        <h2>${esc(t.name)}</h2>
        <div class="meta">${esc(t.provider)} · ${esc(t.capability_label)} · ${esc(t.runtime)} · v${esc(t.version)}</div>
        <div class="tagline">
          ${t.degraded ? '<span class="tg warn">◐ 降级可用</span>'
            : t.available ? '<span class="tg ok">✓ 可用</span>' : '<span class="tg no">待解锁</span>'}
          ${t.best_for.slice(0, 2).map(b => `<span class="tg">${esc(b)}</span>`).join('')}
        </div>
      </div>
    </div>
    ${t.degraded ? `<div class="blockbox"><b>降级可用</b>${esc(t.blocked_reason)}
      ${t.install_instructions ? `<br><span style="font-size:.82rem;opacity:.85">补齐依赖可获得完整能力：${esc(t.install_instructions.split('\n')[0])}</span>` : ''}</div>` : ''}
    ${t.available ? '' : `<div class="blockbox"><b>此工具当前不可用</b>${esc(t.blocked_reason)}
      ${t.needs_keys.length ? `<br>到「密钥」页填入 <code>${t.needs_keys.map(esc).join('</code> 或 <code>')}</code> 即可解锁。` : ''}
      ${t.install_instructions ? `<br><span style="font-size:.82rem;opacity:.85">${esc(t.install_instructions.split('\n')[0])}</span>` : ''}</div>`}
    <div class="form" id="form">
      ${fieldNames.length ? fieldNames.map(k => field(k, props[k], required.includes(k))).join('')
        : '<p style="color:var(--ink-3)">该工具无输入参数。</p>'}
    </div>
    <div class="batchbox">
      <h4>批量生产</h4>
      <p>选一个字段作为变量，每行一个值 —— 会按行生成 N 个任务并入队，其余参数沿用上面的表单。</p>
      <div class="row2">
        <div class="fld">
          <label>变量字段</label>
          <select id="batchField">
            <option value="">（不批量，单次执行）</option>
            ${fieldNames.map(k => `<option value="${esc(k)}">${esc(k)}</option>`).join('')}
          </select>
        </div>
        <div class="fld">
          <label>任务数</label>
          <input class="inp" id="batchCount" value="0" readonly>
        </div>
      </div>
      <div class="fld" style="margin-top:11px">
        <label>变量值（每行一个）</label>
        <textarea id="batchValues" rows="5" placeholder="第一个视频的主题&#10;第二个视频的主题&#10;第三个视频的主题"></textarea>
      </div>
    </div>
    <div class="actions">
      <button class="btn" id="runBtn" ${t.available ? '' : 'disabled'}>▶ 执行</button>
      <button class="btn ghost" id="jsonBtn">查看参数 JSON</button>
      <span class="sp">留空 output_path 会自动分配到 projects/studio/</span>
    </div>`;

  $('#runBtn').onclick = run;
  $('#jsonBtn').onclick = () => {
    const v = JSON.stringify(collect(), null, 2);
    toast('参数已输出到控制台');
    console.log(v);
    alert(v);
  };
  const upd = () => {
    const lines = $('#batchValues').value.split('\n').map(s => s.trim()).filter(Boolean);
    $('#batchCount').value = $('#batchField').value ? lines.length : 0;
    $('#runBtn').textContent = ($('#batchField').value && lines.length)
      ? `▶ 批量执行 ${lines.length} 个任务` : '▶ 执行';
  };
  $('#batchValues').addEventListener('input', upd);
  $('#batchField').addEventListener('change', upd);
}

function field(key, spec, req) {
  const ty = spec.type || 'string';
  const desc = spec.description ? `<div class="hint">${esc(spec.description)}</div>` : '';
  const def = spec.default;
  const label = `<label>${esc(key)}${req ? '<span class="req">*</span>' : ''}
    <span class="ty">${esc(ty)}${spec.enum ? ' enum' : ''}</span></label>`;
  let input;

  if (spec.enum) {
    input = `<select data-k="${esc(key)}" data-t="${esc(ty)}">
      ${!req ? '<option value=""></option>' : ''}
      ${spec.enum.map(o => `<option value="${esc(o)}" ${o === def ? 'selected' : ''}>${esc(o)}</option>`).join('')}
    </select>`;
  } else if (ty === 'boolean') {
    input = `<label class="chk"><input type="checkbox" data-k="${esc(key)}" data-t="boolean"
      ${def ? 'checked' : ''}> 启用</label>`;
  } else if (ty === 'integer' || ty === 'number') {
    input = `<input class="inp" type="number" data-k="${esc(key)}" data-t="${esc(ty)}"
      ${spec.minimum !== undefined ? `min="${spec.minimum}"` : ''}
      ${spec.maximum !== undefined ? `max="${spec.maximum}"` : ''}
      ${ty === 'number' ? 'step="any"' : ''} value="${def ?? ''}">`;
  } else if (ty === 'array' || ty === 'object') {
    input = `<textarea data-k="${esc(key)}" data-t="${esc(ty)}" rows="3"
      placeholder="${ty === 'array' ? '每行一个值，或粘贴 JSON 数组' : '粘贴 JSON 对象'}"></textarea>`;
  } else if (key === 'text' || key === 'prompt' || /prompt|script|text/.test(key)) {
    input = `<textarea data-k="${esc(key)}" data-t="string" rows="4">${esc(def ?? '')}</textarea>`;
  } else {
    input = `<input class="inp" data-k="${esc(key)}" data-t="string" value="${esc(def ?? '')}"
      placeholder="${key === 'output_path' ? '留空自动分配' : ''}">`;
  }
  return `<div class="fld">${label}${input}${desc}</div>`;
}

function collect() {
  const out = {};
  $$('#form [data-k]').forEach(el => {
    const k = el.dataset.k, t = el.dataset.t;
    let v;
    if (t === 'boolean') v = el.checked;
    else if (t === 'integer') v = el.value === '' ? '' : parseInt(el.value, 10);
    else if (t === 'number') v = el.value === '' ? '' : parseFloat(el.value);
    else if (t === 'array') {
      const raw = el.value.trim();
      if (!raw) v = '';
      else if (raw.startsWith('[')) { try { v = JSON.parse(raw); } catch (e) { v = raw.split('\n'); } }
      else v = raw.split('\n').map(s => s.trim()).filter(Boolean);
    } else if (t === 'object') {
      const raw = el.value.trim();
      try { v = raw ? JSON.parse(raw) : ''; } catch (e) { v = ''; }
    } else v = el.value;
    if (v !== '' && v !== undefined && !(Array.isArray(v) && !v.length)) out[k] = v;
  });
  return out;
}

async function run() {
  const btn = $('#runBtn');
  const base = collect();
  const bf = $('#batchField').value;
  const lines = $('#batchValues').value.split('\n').map(s => s.trim()).filter(Boolean);
  btn.disabled = true;
  try {
    if (bf && lines.length) {
      const spec = (S.tools.find(t => t.name === S.current).input_schema.properties || {})[bf] || {};
      const rows = lines.map(v => ({ ...base, [bf]: cast(v, spec.type) }));
      const r = await api('/api/batch', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ tool: S.current, rows, label: S.current }),
      });
      toast(`已入队 ${r.count} 个任务`);
    } else {
      await api('/api/jobs', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ tool: S.current, inputs: base, label: S.current }),
      });
      toast('任务已入队');
    }
    $$('#tabs button').forEach(x => x.classList.toggle('on', x.dataset.tab === 'queue'));
    $$('.tab').forEach(s => s.classList.toggle('on', s.id === 'tab-queue'));
    loadJobs();
  } catch (e) {
    toast('提交失败: ' + e.message, true);
  } finally {
    btn.disabled = false;
  }
}

const cast = (v, ty) => ty === 'integer' ? parseInt(v, 10) : ty === 'number' ? parseFloat(v)
  : ty === 'boolean' ? /^(true|1|yes)$/i.test(v) : v;

/* ---------------- jobs ---------------- */
async function loadJobs() {
  const d = await api('/api/jobs');
  S.jobs = d.jobs;
  renderJobs(d.stats);
}

function renderJobs(stats) {
  if (stats) {
    $('#queueStats').innerHTML = `
      <span>共 <b>${stats.total}</b></span><span>排队 <b>${stats.queued}</b></span>
      <span>执行中 <b>${stats.running}</b></span><span>成功 <b>${stats.success}</b></span>
      <span>失败 <b>${stats.failed}</b></span>
      <span>累计成本 <b>$${(stats.cost_usd || 0).toFixed(4)}</b></span>
      <span>并发 <b>${stats.workers}</b></span>`;
    $('#pillQueue').textContent = stats.running + stats.queued;
  }
  $('#jobList').innerHTML = S.jobs.length ? S.jobs.map(j => `
    <div class="job">
      <span class="st ${j.status}">${({queued:'排队',running:'执行中',success:'成功',failed:'失败',cancelled:'已取消'})[j.status]}</span>
      <div class="main">
        <div class="nm">${esc(j.label || j.tool)}</div>
        <div class="sub">${esc(j.tool)}${j.batch_id ? ` · 批次 ${esc(j.batch_id)}` : ''} · ${esc(Object.keys(j.inputs).slice(0,4).join(', '))}</div>
        ${j.error ? `<div class="err">${esc(j.error).slice(0, 300)}</div>` : ''}
        ${j.artifacts_rel && j.artifacts_rel.length ? `<div class="arts">${
          j.artifacts_rel.map(a => `<a href="/media/${encodeURI(a)}" target="_blank">${esc(a.split('/').pop())}</a>`).join('')}</div>` : ''}
      </div>
      <div class="rt">${j.elapsed}s${j.cost_usd ? `<br>$${j.cost_usd.toFixed(4)}` : ''}</div>
    </div>`).join('')
    : '<p style="color:var(--ink-3);padding:30px;text-align:center">还没有任务。到「生产」页选一个工具执行。</p>';
}

/* ---------------- SSE ---------------- */
function connect() {
  const es = new EventSource('/api/stream');
  es.onopen = () => { $('#conn').className = 'status live'; $('#conn').innerHTML = '<span class="dot"></span>实时连接'; };
  es.onerror = () => { $('#conn').className = 'status'; $('#conn').innerHTML = '<span class="dot"></span>已断开'; };
  es.onmessage = ev => {
    let j; try { j = JSON.parse(ev.data); } catch (e) { return; }
    if (!j || !j.id) return;
    const i = S.jobs.findIndex(x => x.id === j.id);
    if (i >= 0) S.jobs[i] = j; else S.jobs.unshift(j);
    renderJobs();
    api('/api/jobs').then(d => renderJobs(d.stats)).catch(() => {});
  };
}

/* ---------------- outputs ---------------- */
async function loadOutputs() {
  const d = await api('/api/outputs');
  $('#outStats').innerHTML = `<span>共 <b>${d.total}</b> 个产出文件</span>`;
  const icon = e => ({ mp4: '🎬', mov: '🎬', webm: '🎬', mp3: '🎵', wav: '🎵',
    png: '🖼', jpg: '🖼', jpeg: '🖼', srt: '📝', vtt: '📝', json: '📄' }[e] || '📄');
  $('#outGrid').innerHTML = d.files.length ? d.files.map(f => {
    const url = '/media/' + encodeURI(f.path);
    const prev = ['mp4', 'webm', 'mov'].includes(f.ext)
      ? `<video src="${url}" muted preload="metadata"></video>`
      : ['png', 'jpg', 'jpeg', 'webp', 'gif'].includes(f.ext) ? `<img src="${url}" loading="lazy">`
      : icon(f.ext);
    return `<a class="out" href="${url}" target="_blank">
      <div class="prev">${prev}</div>
      <div class="info"><div class="n">${esc(f.name)}</div>
        <div class="m">${(f.size / 1048576).toFixed(1)} MB · ${new Date(f.mtime * 1000).toLocaleString('zh-CN')}</div>
      </div></a>`;
  }).join('') : '<p style="color:var(--ink-3);padding:30px">还没有产出文件。</p>';
}

/* ---------------- tools grid ---------------- */
function renderAllTools() {
  if (!S.summary) return;
  const s = S.summary;
  $('#toolStats').innerHTML = `<span>共 <b>${s.total}</b></span><span>可用 <b>${s.available}</b></span>`
    + (s.degraded ? `<span>其中降级 <b>${s.degraded}</b></span>` : '')
    + `<span>待解锁 <b>${s.blocked}</b></span><span>需密钥 <b>${s.needs_key ?? '–'}</b></span>`;
  const q = ($('#allToolSearch').value || '').trim().toLowerCase();
  const onlyBlocked = $('#onlyBlocked').checked;
  const list = S.tools.filter(t =>
    (!onlyBlocked || !t.available) &&
    (!q || `${t.name} ${t.provider} ${t.capability_label}`.toLowerCase().includes(q)));
  $('#allTools').innerHTML = list.map(t => `
    <div class="tcard ${t.available ? '' : 'blocked'} ${t.degraded ? 'degraded' : ''}">
      <h4><span class="s"></span>${esc(t.name)}</h4>
      <div class="p">${esc(t.provider)} · ${esc(t.capability_label)} · ${esc(t.runtime)}</div>
      ${t.best_for.length ? `<div class="bf">${esc(t.best_for[0])}</div>` : ''}
      ${t.available && !t.degraded ? '' : `<div class="why">${esc(t.blocked_reason).slice(0, 160)}</div>`}
    </div>`).join('') || '<p style="color:var(--ink-3)">无匹配</p>';
}
$('#allToolSearch').addEventListener('input', renderAllTools);
$('#onlyBlocked').addEventListener('change', renderAllTools);

/* ---------------- keys ---------------- */
async function loadKeys() {
  const d = await api('/api/keys');
  S.keys = d.keys;
  const setN = d.keys.filter(k => k.set).length;
  $('#keyStats').innerHTML = `<span>已配置 <b>${setN}</b> / ${d.keys.length}</span>`;
  $('#keyList').innerHTML = d.keys.map(k => `
    <div class="key ${k.set ? 'set' : ''}">
      <div class="kh"><span class="kn">${esc(k.key)}</span>
        ${k.tier ? `<span class="kt ${/免费|完全免费/.test(k.tier) ? 'free' : ''}">${esc(k.tier)}</span>` : ''}</div>
      ${k.label ? `<div class="kl">${esc(k.label)}${k.url ? ` · <a href="${esc(k.url)}" target="_blank" rel="noopener">申请</a>` : ''}</div>` : ''}
      <input class="inp" data-key="${esc(k.key)}" type="password" autocomplete="off"
        placeholder="${k.set ? k.masked + '（留空保持不变）' : '粘贴密钥…'}">
      ${k.unlock_count ? `<div class="ku">可解锁 ${k.unlock_count} 个工具：${esc(k.unlocks.slice(0, 4).join(', '))}${k.unlocks.length > 4 ? '…' : ''}</div>` : ''}
    </div>`).join('');
}

async function saveKeys() {
  const updates = {};
  $$('#keyList [data-key]').forEach(el => { if (el.value.trim()) updates[el.dataset.key] = el.value.trim(); });
  if (!Object.keys(updates).length) return toast('没有需要保存的修改');
  try {
    const r = await api('/api/keys', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ updates }),
    });
    toast(`已保存 ${r.written} 个密钥，可用工具 ${r.summary.available}/${r.summary.total}`);
    await loadCatalog(false);
    await loadKeys();
  } catch (e) { toast('保存失败: ' + e.message, true); }
}

/* ---------------- doctor ---------------- */
async function loadDoctor() {
  const d = await api('/api/doctor');
  const mark = ok => ok ? '<span class="ok-i">✓</span>' : '<span class="no-i">✗</span>';
  $('#doctorBody').innerHTML = `
    <h3 class="sec">命令行依赖</h3>
    <div class="dgrid">${d.commands.map(c => `
      <div class="ditem"><div class="dn">${mark(c.ok)} ${esc(c.name)}</div>
        <div class="dv">${esc(c.ok ? (c.version || c.path) : '未安装')}</div></div>`).join('')}</div>
    ${d.node_ok_for_hyperframes ? '' :
      `<div class="notice">⚠️ 当前 Node 主版本 ${d.node_major}，HyperFrames 渲染引擎要求 ≥ 22。
       在项目目录执行 <code>nvm use</code> 切换后重启工作台。</div>`}
    <h3 class="sec">Python 模块</h3>
    <div class="dgrid">${d.modules.map(m => `
      <div class="ditem"><div class="dn">${mark(m.ok)} ${esc(m.name)}</div></div>`).join('')}</div>
    <h3 class="sec">可免费安装的扩展（无需任何 API 密钥）</h3>
    <div class="dgrid">${d.free_installs.map(f => `
      <div class="ditem"><div class="dn">${esc(f.pkg)}</div>
        <div class="dv" style="white-space:normal">${esc(f.desc)}<br>解锁: ${esc(f.unlocks.join(', '))} · 体积${esc(f.size)}</div>
        <div style="margin-top:8px"><code>.venv/bin/pip install ${esc(f.pkg)}</code></div>
      </div>`).join('')}</div>`;
}

/* ---------------- boot ---------------- */
loadCatalog(false).then(connect).catch(e => toast('加载失败: ' + e.message, true));
loadJobs().catch(() => {});
