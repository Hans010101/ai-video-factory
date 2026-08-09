'use strict';

const S = { tools: [], summary: null, current: null, jobs: [], keys: [], plan: null,
            i18n: { tools: {}, fields: {}, status: {} } };
const zhField = (k) => S.i18n.fields[k] || k;
const zhStatus = (k) => S.i18n.status[k] || k;
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
function goTab(name) {
  $$('#tabs button').forEach(x => x.classList.toggle('on', x.dataset.tab === name));
  $$('.tab').forEach(s => s.classList.toggle('on', s.id === 'tab-' + name));
  ({ queue: loadJobs, outputs: loadOutputs, tools: renderAllTools,
     keys: loadKeys, doctor: loadDoctor }[name] || (() => {}))();
}
$('#tabs').addEventListener('click', e => {
  const b = e.target.closest('button[data-tab]');
  if (b) goTab(b.dataset.tab);
});

/* ---------------- 智能派单 ---------------- */
const dz = $('#dropzone'), fi = $('#fileInput');
$('#pickFile').onclick = () => fi.click();
fi.onchange = () => fi.files[0] && upload(fi.files[0]);
['dragenter', 'dragover'].forEach(ev => dz.addEventListener(ev, e => {
  e.preventDefault(); dz.classList.add('hot');
}));
['dragleave', 'drop'].forEach(ev => dz.addEventListener(ev, e => {
  e.preventDefault(); dz.classList.remove('hot');
}));
dz.addEventListener('drop', e => {
  const f = e.dataTransfer.files[0];
  if (f) upload(f);
});

async function upload(file) {
  const fd = new FormData();
  fd.append('file', file);
  try {
    toast(`正在解析 ${file.name}…`);
    const r = await api('/api/intake/upload', { method: 'POST', body: fd });
    $('#briefText').value = r.text;
    $('#briefMeta').textContent = `已载入 ${r.filename}，${r.chars} 字`;
    toast(`已载入 ${r.chars} 字，可以开始匹配`);
  } catch (e) { toast('解析失败: ' + e.message, true); }
}

// 花钱的选项必须让人看得见代价：按分镜数实时算最坏情况花费
function updateCostNote() {
  const mode = $('#aiFallback').value;
  const note = $('#costNote');
  if (mode === 'off') { note.style.display = 'none'; return; }
  const scenes = (S.plan && S.plan.brief) ? S.plan.brief.visual_count
    : ($('#briefText').value.match(/^\s*画面\s*[:：]/gm) || []).length;
  const unit = mode === 'image' ? 0.04 : 0.5;
  const cap = parseFloat($('#budget').value);
  note.style.display = 'block';
  note.innerHTML = `⚠️ 已开启 AI 生成兜底（${mode === 'image' ? '图片' : '视频'}，约 $${unit}/段）。`
    + (scenes ? `当前 ${scenes} 条画面建议，<b>最坏情况全部生成约 $${(scenes * unit).toFixed(2)}</b>。` : '')
    + (cap > 0 ? `预算上限 $${cap.toFixed(2)}，超出即停。` : '<b>未设上限</b>，建议填一个。');
}
$('#aiFallback').addEventListener('change', updateCostNote);
$('#budget').addEventListener('input', updateCostNote);
$('#briefText').addEventListener('input', updateCostNote);

const intakeOptions = () => ({
  budget_usd: parseFloat($('#budget').value) || null,
  want_subtitle: $('#wantSub').checked,
  ai_fallback: $('#aiFallback').value,
});

$('#planBtn').onclick = async () => {
  const text = $('#briefText').value.trim();
  if (!text) return toast('请先粘贴脚本或上传文档', true);
  const btn = $('#planBtn');
  btn.disabled = true;
  try {
    S.plan = await api('/api/intake/plan', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ text, ...intakeOptions() }),
    });
    updateCostNote();
    renderPlan(S.plan);
    toast(`已解析 ${S.plan.brief.scene_count} 个分镜，匹配 ${S.plan.stages.length} 道工序`);
  } catch (e) { toast('匹配失败: ' + e.message, true); }
  finally { btn.disabled = false; }
};

function renderPlan(p) {
  const b = p.brief;
  const scenes = b.scenes.map(s => `<tr>
    <td>第${s.index}镜</td>
    <td>${esc(s.narration) || '<span style="color:var(--ink-3)">—</span>'}</td>
    <td>${esc(s.visual) || '<span style="color:var(--ink-3)">—</span>'}</td></tr>`).join('');

  const chain = p.stages.map((s, i) => {
    const alts = (s.candidates || []).filter(c => c.tool !== s.tool);
    return `<div class="stage ${s.available ? '' : 'blocked'}">
      <div class="sn">${i + 1}</div>
      <div>
        <div class="sname">${esc(s.stage)}</div>
        <div class="stool">${s.available ? esc(s.tool_label || s.tool) : '暂无可用工具'}</div>
        <div class="swhy">${esc(s.reason)}</div>
        <div class="swhy">${esc(s.detail)}</div>
        ${alts.length ? `<select data-stage="${esc(s.stage)}">
            <option value="">备选工具（默认用推荐）</option>
            ${alts.map(c => `<option value="${esc(c.tool)}">${esc(c.label)}${c.score != null ? ` · ${c.score}` : ''}</option>`).join('')}
          </select>` : ''}
      </div>
      <div class="sright">${s.available ? (s.dispatchable ? `${s.job_count} 个任务` : '待前序产物') : '受阻'}
        ${s.score != null ? `<br>评分 ${s.score}` : ''}</div>
    </div>`;
  }).join('');

  $('#planBody').innerHTML = `
    <table class="scenetable">
      <thead><tr><th>分镜</th><th>旁白</th><th>画面建议</th></tr></thead>
      <tbody>${scenes}</tbody>
    </table>
    <div class="chain">${chain}</div>
    ${p.deferred_stages && p.deferred_stages.length ? `<div class="deferred">
      <b>${esc(p.deferred_stages.join('、'))}</b> 需要前序工序的真实产物路径才能执行，
      本次不会下发。等配音/画面完成后，到「成片库」拿到文件，再去「单工具」页触发即可。</div>` : ''}
    ${p.blocked_stages.length ? `<div class="deferred" style="background:var(--warn-bg);color:var(--warn)">
      <b>${esc(p.blocked_stages.join('、'))}</b> 没有可用工具，需要先到「密钥配置」补齐相应密钥。</div>` : ''}
    <div class="actions">
      <button class="btn" id="runPlanBtn" ${p.runnable ? '' : 'disabled'}>
        ④ 下发 ${p.total_jobs} 个任务</button>
      <span class="sp">共 ${b.scene_count} 分镜 · ${b.narration_chars} 字旁白 · ${b.visual_count} 条画面建议</span>
    </div>`;

  const rb = $('#runPlanBtn');
  if (rb) rb.onclick = runPlan;
}

async function runPlan() {
  const overrides = {};
  $$('#planBody select[data-stage]').forEach(s => { if (s.value) overrides[s.dataset.stage] = s.value; });
  const btn = $('#runPlanBtn');
  btn.disabled = true;
  try {
    const r = await api('/api/intake/run', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ text: $('#briefText').value, ...intakeOptions(), overrides }),
    });
    toast(`已下发 ${r.submitted} 个任务`);
    goTab('queue');
  } catch (e) { toast('下发失败: ' + e.message, true); btn.disabled = false; }
}

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
    if (q && !(`${t.name} ${t.label} ${t.provider} ${t.capability_label} ${t.capability}`.toLowerCase().includes(q))) continue;
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
                data-tool="${esc(t.name)}" title="${esc(t.name)} — ${esc(t.blocked_reason || '可用')}">
          <span class="s"></span><span class="nm">${esc(t.label || t.name)}</span>
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
        <h2>${esc(t.label || t.name)}</h2>
        <div class="meta">${esc(t.name)} · ${esc(t.provider)} · ${esc(t.capability_label)}
          · ${t.runtime === 'local' ? '本地运行' : '云端调用'} · v${esc(t.version)}</div>
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
  const zh = zhField(key);
  const label = `<label>${esc(zh)}${req ? '<span class="req">*</span>' : ''}
    <span class="ty">${zh !== key ? esc(key) + ' · ' : ''}${esc(ty)}${spec.enum ? ' 枚举' : ''}</span></label>`;
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
      <span class="st ${j.status}">${zhStatus(j.status)}</span>
      <div class="main">
        <div class="nm">${esc(j.label || S.i18n.tools[j.tool] || j.tool)}</div>
        <div class="sub">${esc(S.i18n.tools[j.tool] || j.tool)}${j.batch_id ? ` · 批次 ${esc(j.batch_id)}` : ''}
          · ${esc(Object.keys(j.inputs).slice(0, 4).map(zhField).join('、'))}</div>
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
      <h4><span class="s"></span>${esc(t.label || t.name)}</h4>
      <div class="p">${esc(t.name)} · ${esc(t.provider)} · ${esc(t.capability_label)}
        · ${t.runtime === 'local' ? '本地' : '云端'}</div>
      ${t.best_for.length ? `<div class="bf">${esc(t.best_for[0])}</div>` : ''}
      ${t.available && !t.degraded ? '' : `<div class="why">${esc(t.blocked_reason).slice(0, 160)}</div>`}
    </div>`).join('') || '<p style="color:var(--ink-3)">无匹配</p>';
}
$('#allToolSearch').addEventListener('input', renderAllTools);
$('#onlyBlocked').addEventListener('change', renderAllTools);

/* ---------------- keys ---------------- */
// 不用 emoji：部分系统字体渲染成豆腐块，反而看不出状态。用 CSS 圆点 + 纯文字。
// 文字统一以「已配置」开头，配置与否一眼可辨，后半段才是验证结论。
const VSTATE = {
  ok:      { cls: 'vok',   text: '已配置 · 可用' },
  warn:    { cls: 'vwarn', text: '已配置 · 有问题' },
  bad:     { cls: 'vbad',  text: '已配置 · 不可用' },
  unknown: { cls: 'vunk',  text: '已配置 · 未验证' },
};

async function loadKeys() {
  const d = await api('/api/keys');
  S.keys = d.keys;
  renderKeys();
  // 进页面就自动验证已填的密钥 —— 光标绿色不代表能用，
  // 用户踩过的坑全是「填了但不能用」。
  verifyKeys(true);
}

function renderKeys() {
  const keys = S.keys;
  const setN = keys.filter(k => k.set).length;
  const v = S.verify || {};
  const okN = keys.filter(k => v[k.key] && v[k.key].state === 'ok').length;
  const badN = keys.filter(k => v[k.key] && ['bad', 'warn'].includes(v[k.key].state)).length;
  $('#keyStats').innerHTML =
    `<span>已配置 <b>${setN}</b> / ${keys.length}</span>`
    + (okN ? `<span style="color:var(--ok)">已验证可用 <b>${okN}</b></span>` : '')
    + (badN ? `<span style="color:var(--err)">需处理 <b>${badN}</b></span>` : '');

  const card = (k) => {
    const r = v[k.key];
    const st = k.set ? VSTATE[(r && r.state) || 'unknown'] : null;
    return `
    <div class="key ${k.set ? 'set' : ''} ${st ? st.cls : ''}">
      <div class="kh">
        ${k.set
          ? `<span class="setflag">已配置</span>
             <span class="vbadge ${st.cls}"><i class="vdot"></i>${st.text.replace('已配置 · ', '')}</span>`
          : '<span class="vbadge vnone"><i class="vdot"></i>未配置</span>'}
        ${k.tier ? `<span class="kt ${/免费|完全免费/.test(k.tier) ? 'free' : ''}">${esc(k.tier)}</span>` : ''}
      </div>
      <div class="kn">${esc(k.key)}</div>
      ${k.label ? `<div class="kl">${esc(k.label)}${k.url ? ` · <a href="${esc(k.url)}" target="_blank" rel="noopener">申请</a>` : ''}</div>` : ''}
      <input class="inp" data-key="${esc(k.key)}" type="password" autocomplete="off"
        placeholder="${k.set ? k.masked + '（留空保持不变）' : '粘贴密钥…'}">
      ${r && r.detail && k.set ? `<div class="vdetail ${st.cls}">${esc(r.detail)}</div>` : ''}
      ${k.unlock_count ? `<div class="ku">可解锁 ${k.unlock_count} 个工具：${esc(k.unlocks.slice(0, 4).join(', '))}${k.unlocks.length > 4 ? '…' : ''}</div>` : ''}
    </div>`;
  };

  // 分三组：已配置 → 待配置 → 配套参数。三十多张卡片平铺时很难扫读，
  // 分组后「哪些已经好了、下一个该配哪个」一眼就能看出来。
  const groups = [
    { key: 'done', title: '已配置',
      hint: '状态由真实 API 探测得出，不是只看有没有填',
      items: keys.filter(k => k.set && k.group !== 'setting') },
    { key: 'todo', title: '待配置',
      hint: '按重要性排序 —— 越靠前对成片质量影响越大',
      items: keys.filter(k => !k.set && k.group !== 'setting') },
    { key: 'setting', title: '配套参数',
      hint: '这些不是密钥，是区域、项目名、本地模型等配置项',
      items: keys.filter(k => k.group === 'setting') },
  ];

  $('#keyList').innerHTML = groups.filter(g => g.items.length).map(g => `
    <div class="kgroup">
      <div class="kgh"><h3>${g.title}<span class="n">${g.items.length}</span></h3>
        <p>${g.hint}</p></div>
      <div class="kgrid">${g.items.map(card).join('')}</div>
    </div>`).join('');
}

async function verifyKeys(silent) {
  const btn = $('#verifyBtn');
  if (btn) { btn.disabled = true; btn.textContent = '验证中…'; }
  try {
    const d = await api('/api/keys/verify', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ updates: {} }),
    });
    S.verify = {};
    d.results.forEach(r => { S.verify[r.key] = r; });
    renderKeys();
    if (!silent) {
      const bad = d.results.filter(r => ['bad', 'warn'].includes(r.state));
      toast(bad.length ? `${bad.length} 个密钥有问题，见卡片说明` : '全部密钥验证通过', bad.length > 0);
    }
  } catch (e) {
    if (!silent) toast('验证失败: ' + e.message, true);
  } finally {
    if (btn) { btn.disabled = false; btn.textContent = '重新验证'; }
  }
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
api('/api/i18n')
  .then(d => { S.i18n = d; })
  .catch(() => {})
  .then(() => loadCatalog(false))
  .then(connect)
  .catch(e => toast('加载失败: ' + e.message, true));
loadJobs().catch(() => {});
