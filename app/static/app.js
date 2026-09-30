/* 展签审校工作台前端（原生 JS） */
const ROLE_NAME = { librarian: '馆员', translator: '译者', reviewer: '校对' };

const state = {
  me: null,
  languages: [],
  currentLangId: null,
  board: null,
  batchPreview: null,
};

const $ = (sel) => document.querySelector(sel);

function esc(s) {
  return String(s ?? '').replace(/[&<>"']/g, (c) => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
  }[c]));
}

function toast(msg, isErr = false) {
  const el = $('#toast');
  el.textContent = msg;
  el.className = 'toast show' + (isErr ? ' err' : '');
  clearTimeout(toast._t);
  toast._t = setTimeout(() => { el.className = 'toast'; }, 3200);
}

async function api(path, opts = {}) {
  const resp = await fetch(path, {
    headers: { 'Content-Type': 'application/json' },
    ...opts,
  });
  if (resp.status === 401) {
    location.href = '/login';
    throw new Error('未登录');
  }
  const data = await resp.json().catch(() => ({}));
  if (!resp.ok) {
    // 409 发布失败时 detail 为 {message, reasons}
    const d = data.detail;
    if (d && typeof d === 'object') {
      const err = new Error(d.message + (d.reasons && d.reasons.length ? '：' + d.reasons.join('；') : ''));
      err.payload = d;
      throw err;
    }
    throw new Error(typeof d === 'string' ? d : '请求失败 (' + resp.status + ')');
  }
  return data;
}

const isWorker = () => state.me.role === 'translator' || state.me.role === 'reviewer';
const isLibrarian = () => state.me.role === 'librarian';

// ---------------------------- 初始化 ----------------------------

async function init() {
  state.me = await api('/api/me');
  $('#who').textContent = `${state.me.display_name}（${state.me.username}）`;
  const rb = $('#role-badge');
  rb.textContent = ROLE_NAME[state.me.role];
  rb.classList.add(state.me.role);

  $('#logout-btn').addEventListener('click', async () => {
    await fetch('/api/logout', { method: 'POST' });
    location.href = '/login';
  });

  if (isLibrarian()) {
    $('#librarian-panel').hidden = false;
    $('#batch-panel').hidden = false;
    bindLibrarianEvents();
    bindBatchEvents();
  }

  await loadLanguages();
  await loadSnapshots();
}

async function loadLanguages() {
  state.languages = await api('/api/languages');
  const tabs = $('#lang-tabs');
  if (!state.languages.length) {
    tabs.innerHTML = '<span class="muted">尚无目标语言，请馆员先添加。</span>';
    $('#board').innerHTML = '';
    return;
  }
  if (!state.languages.some((l) => l.id === state.currentLangId)) {
    state.currentLangId = state.languages[0].id;
  }
  tabs.innerHTML = state.languages.map((l) => `
    <button class="langtab ${l.id === state.currentLangId ? 'active' : ''}" data-id="${l.id}">
      ${esc(l.name)} <span class="muted">${esc(l.code)}</span>
    </button>`).join('');
  tabs.querySelectorAll('.langtab').forEach((b) =>
    b.addEventListener('click', () => {
      state.currentLangId = Number(b.dataset.id);
      loadLanguages().then(loadBoard);
    }));
  await loadBoard();
  if (isLibrarian()) {
    loadLabelAdmin();
    renderBatchPickers();
  }
}

// ---------------------------- 馆员：多语同批发布 ----------------------------

function renderBatchPickers() {
  const el = $('#batch-lang-pickers');
  if (!state.languages.length) {
    el.innerHTML = '<span class="muted">尚无目标语言。</span>';
    return;
  }
  const prev = new Set((state.batchPreview?.languages || []).map((l) => l.language_id));
  el.innerHTML = state.languages.map((l) => `
    <label class="batch-pick">
      <input type="checkbox" class="batch-lang" value="${l.id}" ${prev.has(l.id) ? 'checked' : ''}>
      ${esc(l.name)} <span class="muted">${esc(l.code)}</span>
    </label>`).join('');
}

function renderBatchPreview(p) {
  state.batchPreview = p;
  const el = $('#batch-preview');
  const rows = p.languages.map((l) => {
    const c = l.counts || {};
    const badge = l.ready
      ? '<span class="tag ready">就绪</span>'
      : '<span class="tag ' + (c.missing ? 'missing' : 'stale') + '">未就绪</span>';
    const detail = l.ready
      ? `<span class="muted">覆盖 ${l.total} 条在展展签 · 下一版本 v${l.next_version}（当前 v${l.current_version || '—'}）</span>`
      : `<ul>${l.blocking_reasons.map((r) => `<li>${esc(r)}</li>`).join('')}</ul>`;
    return `
      <div class="batch-lang-row ${l.ready ? 'ok' : 'blocked'}">
        <div><b>${esc(l.name)}</b> <span class="muted">${esc(l.code)}</span> ${badge}</div>
        <div class="batch-lang-detail">${detail}</div>
      </div>`;
  }).join('');
  el.innerHTML = `
    <div class="publish-box ${p.ready ? 'ok' : 'blocked'}">
      ${p.ready
        ? `✅ 所选 ${p.languages.length} 种语言均已覆盖当前全部 ${p.on_display_total} 条在展展签，可以整批提交。`
        : '🚫 所选语言未全部就绪，整批提交会被拒绝，原因如下：'}
    </div>
    ${rows}
    <div class="muted" style="margin:8px 0">
      校验凭证（提交时原样回传，无需手填）：
      <code class="batch-cred">${esc(p.credential.slice(0, 48))}…</code>
    </div>
    <div class="row-actions">
      <button id="batch-commit-btn" ${p.ready ? '' : 'disabled'}>
        ${p.ready ? '凭凭证整批提交（原子生成各语言快照）' : '全部语言就绪后方可整批提交'}
      </button>
      <button class="ghost small" id="batch-repreview-btn">放弃凭证并重新预览</button>
    </div>`;
  $('#batch-commit-btn')?.addEventListener('click', commitBatch);
  $('#batch-repreview-btn')?.addEventListener('click', () => previewBatch());
}

function renderBatchError(err) {
  const el = $('#batch-preview');
  const reasons = err.payload?.reasons || [];
  el.innerHTML = `
    <div class="publish-box blocked">
      🚫 ${esc(err.payload?.message || err.message)}
      ${reasons.length ? '<ul>' + reasons.map((r) => `<li>${esc(r)}</li>`).join('') + '</ul>' : ''}
      <div class="row-actions"><button class="small" id="batch-repreview-btn">重新预览</button></div>
    </div>`;
  state.batchPreview = null;
  $('#batch-repreview-btn')?.addEventListener('click', () => previewBatch());
}

async function previewBatch() {
  const ids = [...document.querySelectorAll('.batch-lang:checked')].map((c) => Number(c.value));
  if (ids.length < 2) {
    return toast('请至少勾选两种不同的目标语言', true);
  }
  const btn = $('#batch-preview-btn');
  btn.disabled = true;
  try {
    const p = await api('/api/publish-batches/preview', {
      method: 'POST',
      body: JSON.stringify({ language_ids: ids }),
    });
    renderBatchPreview(p);
  } catch (e) {
    if (e.payload) renderBatchError(e);
    toast(e.message, true);
  } finally {
    btn.disabled = false;
  }
}

async function commitBatch() {
  const p = state.batchPreview;
  if (!p?.credential) return toast('请先预览', true);
  const btn = $('#batch-commit-btn');
  btn.disabled = true;
  try {
    const r = await api('/api/publish-batches', {
      method: 'POST',
      body: JSON.stringify({ credential: p.credential }),
    });
    const summary = r.languages.map((l) => `${l.name} v${l.version}`).join('、');
    toast(`批次 #${r.batch_no} 已发布：${summary}；各语言快照均不可变`);
    state.batchPreview = null;
    $('#batch-preview').innerHTML = '';
    renderBatchPickers();
    await Promise.all([loadBoard(), loadSnapshots()]);
  } catch (e) {
    if (e.payload) renderBatchError(e);
    toast(e.message, true);
  } finally {
    btn.disabled = false;
  }
}

function bindBatchEvents() {
  $('#batch-preview-btn').addEventListener('click', () => previewBatch());
}

// ---------------------------- 馆员：原文与语言 ----------------------------

async function loadLabelAdmin() {
  const labels = await api('/api/labels');
  $('#label-admin').innerHTML = labels.map((l) => {
    const withdrawn = l.status === 'withdrawn';
    const statusBadge = withdrawn
      ? '<span class="tag withdrawn">已撤展</span>'
      : '<span class="tag ready">在展</span>';
    let statusHtml = '';
    if (withdrawn) {
      statusHtml = `
        <div class="withdraw-box">
          撤展原因：<b>${esc(l.withdrawn_reason || '（未填写）')}</b>
          <span class="muted">· ${esc(l.withdrawn_at)} · 操作人 ${esc(l.withdrawn_by_name || ('#' + l.withdrawn_by))}</span>
        </div>`;
    } else if (l.restored_at) {
      statusHtml = `
        <div class="muted">
          曾撤展，已于 ${esc(l.restored_at)} 由 ${esc(l.restored_by_name || ('#' + l.restored_by))} 复展；
          各语言译文确认已清除，须重新经他人确认并发布新快照后游客才可见。
        </div>`;
    }
    const statusActions = withdrawn
      ? `<button class="small restore-label" data-id="${l.id}" data-rev="${l.status_rev}">复展（状态版本升至 ${l.status_rev + 1}，清除各语言确认、保留译文）</button>`
      : `
        <input type="text" class="withdraw-reason" placeholder="撤展原因（必填）">
        <button class="small danger withdraw-label" data-id="${l.id}" data-rev="${l.status_rev}">撤展（立即从所有语言游客最新内容隐藏）</button>`;
    return `
    <div class="item" data-label="${l.id}">
      <div class="meta">
        #${l.id} · 原文修订号 ${l.source_rev} · 状态版本 ${l.status_rev} · 更新于 ${esc(l.updated_at)}
        ${statusBadge}
      </div>
      <textarea class="src-edit" rows="2">${esc(l.source_text)}</textarea>
      ${statusHtml}
      <div class="row-actions">
        <button class="small ghost save-src" data-id="${l.id}" data-rev="${l.source_rev}">
          保存原文（修订号升至 ${l.source_rev + 1}，并使该条所有译文确认失效）
        </button>
        ${statusActions}
      </div>
    </div>`;
  }).join('') || '<div class="muted">暂无展签。</div>';

  document.querySelectorAll('.save-src').forEach((btn) =>
    btn.addEventListener('click', async () => {
      const id = btn.dataset.id;
      const text = document.querySelector(`.item[data-label="${id}"] .src-edit`).value.trim();
      if (!text) return toast('原文不能为空', true);
      btn.disabled = true;
      try {
        await api('/api/labels/' + id, {
          method: 'PUT',
          body: JSON.stringify({ source_text: text, source_rev: Number(btn.dataset.rev) }),
        });
        toast('原文已更新：修订号 +1，相关译文确认已失效');
        await Promise.all([loadLabelAdmin(), loadBoard()]);
      } catch (e) {
        toast(e.message, true);
        btn.disabled = false;
      }
    }));

  document.querySelectorAll('.withdraw-label').forEach((btn) =>
    btn.addEventListener('click', async () => {
      const id = btn.dataset.id;
      const reason = document.querySelector(`.item[data-label="${id}"] .withdraw-reason`).value.trim();
      if (!reason) return toast('撤展原因必填', true);
      btn.disabled = true;
      try {
        await api('/api/labels/' + id + '/withdraw', {
          method: 'POST',
          body: JSON.stringify({ status_rev: Number(btn.dataset.rev), reason }),
        });
        toast('已撤展：游客最新内容立即对所有语言隐藏，历史快照不变');
        await Promise.all([loadLabelAdmin(), loadBoard()]);
      } catch (e) {
        toast(e.message, true);
        btn.disabled = false;
      }
    }));

  document.querySelectorAll('.restore-label').forEach((btn) =>
    btn.addEventListener('click', async () => {
      btn.disabled = true;
      try {
        await api('/api/labels/' + btn.dataset.id + '/restore', {
          method: 'POST',
          body: JSON.stringify({ status_rev: Number(btn.dataset.rev) }),
        });
        toast('已复展：译文保留、确认已清除；重新确认并发布新快照后游客才可见');
        await Promise.all([loadLabelAdmin(), loadBoard()]);
      } catch (e) {
        toast(e.message, true);
        btn.disabled = false;
      }
    }));
}

function bindLibrarianEvents() {
  $('#add-label-btn').addEventListener('click', async () => {
    const text = $('#new-label-text').value.trim();
    if (!text) return toast('原文不能为空', true);
    try {
      await api('/api/labels', { method: 'POST', body: JSON.stringify({ source_text: text, source_rev: 1 }) });
      $('#new-label-text').value = '';
      toast('展签已新增（原文修订号 1）');
      await Promise.all([loadLabelAdmin(), loadBoard()]);
    } catch (e) { toast(e.message, true); }
  });

  $('#add-lang-btn').addEventListener('click', async () => {
    const code = $('#new-lang-code').value.trim();
    const name = $('#new-lang-name').value.trim();
    if (!code || !name) return toast('代码与名称均必填', true);
    try {
      await api('/api/languages', { method: 'POST', body: JSON.stringify({ code, name }) });
      $('#new-lang-code').value = '';
      $('#new-lang-name').value = '';
      toast('语言已新增');
      await loadLanguages();
    } catch (e) { toast(e.message, true); }
  });
}

// ---------------------------- 语言看板 ----------------------------

async function loadBoard() {
  if (!state.currentLangId) return;
  state.board = await api('/api/board/' + state.currentLangId);
  renderBoard();
}

function renderBoard() {
  const b = state.board;
  const c = b.counts;
  const lang = b.language;

  const statusLine = b.latest_snapshot
    ? `最新已发布快照 v${b.latest_snapshot.version}（${esc(b.latest_snapshot.published_at)} 由 ${esc(b.latest_snapshot.published_by_name || '?')} 发布）`
    : '该语言尚未发布过快照';

  let publishHtml;
  if (isLibrarian()) {
    if (b.publishable) {
      publishHtml = `
        <div class="publish-box ok">
          ✅ 全部 ${b.total} 条译文均对应当前原文修订且已确认，可以发布。
          <div class="row-actions">
            <button id="publish-btn">发布新的不可变语言快照</button>
          </div>
        </div>`;
    } else {
      publishHtml = `
        <div class="publish-box blocked">
          🚫 暂不可发布，原因：
          <ul>${b.blocking_reasons.map((r) => `<li>${esc(r)}</li>`).join('')}</ul>
        </div>`;
    }
  } else {
    publishHtml = `<div class="muted">${b.publishable ? '全部译文已就绪，等待馆员发布。' : '尚有译文未就绪。'}</div>`;
  }

  let html = `
  <div class="card">
    <h2>${esc(lang.name)} <span class="muted">${esc(lang.code)}</span> 审校看板</h2>
    <div class="stats">
      <div class="stat s-missing"><b>${c.missing}</b>待译</div>
      <div class="stat s-pending"><b>${c.pending}</b>待审</div>
      <div class="stat s-stale"><b>${c.stale}</b>过期</div>
      <div class="stat s-ready"><b>${c.ready}</b>已确认</div>
      <div class="stat"><b>${b.total}</b>在展总计</div>
      <div class="stat s-withdrawn"><b>${b.withdrawn_total || 0}</b>已撤展</div>
    </div>
    <div class="snap-meta" style="margin-top:8px">${statusLine}</div>
    ${publishHtml}
  </div>`;

  html += '<div class="card">' + b.items.map(renderItem).join('') + '</div>';
  if (b.withdrawn_items && b.withdrawn_items.length) {
    html += `
    <div class="card withdrawn-card">
      <h2>已撤展展签（不计入待办与发布，游客最新内容不可见）</h2>
      ${b.withdrawn_items.map(renderWithdrawnItem).join('')}
    </div>`;
  }
  $('#board').innerHTML = html;

  const pb = $('#publish-btn');
  if (pb) pb.addEventListener('click', publishSnapshot);
  bindItemEvents();
}

function renderClaim(item) {
  // 仅待审译文可能带有效认领；认领人、到期时间由看板接口给出
  if (item.status !== 'pending' || !item.claim) return '';
  const c = item.claim;
  const exp = esc(c.expires_at);
  if (isWorker() && c.mine) {
    return `
      <div class="claim-box mine">
        🔒 你已认领该译文，剩余 <b class="claim-countdown" data-expires="${exp}">--:--</b>，
        请在此期间完成校对确认；其他账号此时不能确认。
        <div class="row-actions">
          <button class="small ghost release-claim">提前释放认领</button>
        </div>
      </div>`;
  }
  const who = esc(c.claimer_name || ('#' + c.claimer_id));
  return `
  <div class="claim-box">
    🔒 已由 <b>${who}</b> 认领，到期 ${exp}（UTC），剩余
    <b class="claim-countdown" data-expires="${exp}">--:--</b>；到期后可接手。
  </div>`;
}

function renderItem(item) {
  const l = item.label;
  const t = item.translation;
  const tagMap = {
    missing: ['待译', 'missing'],
    pending: ['待审', 'pending'],
    stale: ['过期', 'stale'],
    ready: ['已确认', 'ready'],
  };
  const [tagText, tagCls] = tagMap[item.status];

  let transHtml = '';
  if (t) {
    const conf = t.confirmed
      ? `✅ 已由 <b>${esc(t.confirmer_name || ('#' + t.confirmer_id))}</b> 确认`
      : '⏳ 未确认';
    transHtml = `
      <div class="trans">
        <div class="meta">
          译文修订号 ${t.trans_rev} · 对应原文修订号 ${t.source_rev_at_edit} ·
          译者 ${esc(t.translator_name || ('#' + t.translator_id))} · ${conf}
        </div>
        <div class="text">${esc(t.text)}</div>
      </div>`;
  }

  // 操作区
  let actions = '';
  if (isWorker()) {
    const canEdit = true; // 译者/校对均可提交译文
    const own = t && t.translator_id === state.me.id;
    actions += `
      <label>${t ? '修订译文（提交后译文修订号 +1、清除确认）' : '提交译文'}</label>
      <textarea class="t-edit" rows="2">${t ? esc(t.text) : ''}</textarea>
      <div class="row-actions">
        <button class="small submit-trans">${t ? '提交修订' : '提交译文'}</button>`;

    if (t) {
      if (item.status === 'ready') {
        actions += `<span class="muted">已确认，无需操作。</span>`;
      } else if (item.status === 'stale') {
        actions += `<span class="reason">译文基于旧版原文，须先提交修订后再确认。</span>`;
      } else if (own) {
        actions += `<span class="muted">此译文由你提交，依规则须由另一名账号校对确认，且你不能认领自己的译文。</span>`;
      } else if (item.claim && !item.claim.mine) {
        actions += `<span class="muted">认领有效期内仅认领人可确认；可等待到期接手，或请认领人释放。</span>`;
      } else {
        // 待审且非本人提交：无人认领可直接确认（沿用原流程）或先认领；
        // 本人持有认领时确认按钮照常可用
        actions += `<button class="small ghost confirm-trans">确认译文</button>`;
        if (!item.claim) {
          actions += `<button class="small claim-btn">认领 30 分钟</button>`;
        }
      }
    }
    actions += '</div>';
  } else if (t && t.confirmed) {
    actions += `<div class="reason ok">✅ 已确认（读者视角为可发布内容）</div>`;
  }

  return `
  <div class="item" data-label="${l.id}"
       data-source-rev="${l.source_rev}"
       data-trans-rev="${t ? t.trans_rev : ''}">
    <div class="meta">
      展签 #${l.id} · 原文修订号 ${l.source_rev}
      <span class="tag ${tagCls}">${tagText}</span>
    </div>
    <div class="src">${esc(l.source_text)}</div>
    ${transHtml}
    <div class="reason ${item.status === 'ready' ? 'ok' : ''}">${esc(item.reason)}</div>
    ${renderClaim(item)}
    ${actions}
  </div>`;
}

function renderWithdrawnItem(item) {
  const l = item.label;
  const t = item.translation;
  const transHtml = t ? `
    <div class="trans">
      <div class="meta">
        译文修订号 ${t.trans_rev} · 对应原文修订号 ${t.source_rev_at_edit} ·
        译者 ${esc(t.translator_name || ('#' + t.translator_id))}
        ${t.confirmed ? ` · 原确认人 ${esc(t.confirmer_name || ('#' + t.confirmer_id))}（复展后确认已失效）` : ''}
      </div>
      <div class="text muted">${esc(t.text)}</div>
    </div>` : '<div class="muted">尚无译文。</div>';
  return `
  <div class="item withdrawn-item" data-label="${l.id}">
    <div class="meta">
      展签 #${l.id} · 原文修订号 ${l.source_rev} · 状态版本 ${l.status_rev}
      <span class="tag withdrawn">已撤展</span>
    </div>
    <div class="src">${esc(l.source_text)}</div>
    ${transHtml}
    <div class="withdraw-box">
      撤展原因：<b>${esc(l.withdrawn_reason || '（未填写）')}</b>
      <span class="muted">· ${esc(l.withdrawn_at)} · 操作人 ${esc(l.withdrawn_by_name || ('#' + l.withdrawn_by))}</span>
    </div>
    <div class="muted">已从该语言游客最新发布内容中隐藏；历史快照内容保持不变。</div>
  </div>`;
}

function bindItemEvents() {
  document.querySelectorAll('.submit-trans').forEach((btn) =>
    btn.addEventListener('click', async () => {
      const box = btn.closest('.item');
      const text = box.querySelector('.t-edit').value.trim();
      if (!text) return toast('译文不能为空', true);
      const payload = {
        text,
        source_rev: Number(box.dataset.sourceRev),
      };
      if (box.dataset.transRev !== '') payload.trans_rev = Number(box.dataset.transRev);
      btn.disabled = true;
      try {
        await api(`/api/board/${state.currentLangId}/labels/${box.dataset.label}/translation`, {
          method: 'POST',
          body: JSON.stringify(payload),
        });
        toast('译文已保存：' + (payload.trans_rev ? '译文修订号 +1，确认已清除' : '已提交，等待校对'));
        await loadBoard();
      } catch (e) {
        toast(e.message, true);
        btn.disabled = false;
      }
    }));

  document.querySelectorAll('.confirm-trans').forEach((btn) =>
    btn.addEventListener('click', async () => {
      const box = btn.closest('.item');
      btn.disabled = true;
      try {
        await api(`/api/board/${state.currentLangId}/labels/${box.dataset.label}/confirm`, {
          method: 'POST',
          body: JSON.stringify({
            source_rev: Number(box.dataset.sourceRev),
            trans_rev: Number(box.dataset.transRev),
          }),
        });
        toast('已确认');
        await loadBoard();
      } catch (e) {
        toast(e.message, true);
        btn.disabled = false;
      }
    }));

  document.querySelectorAll('.claim-btn').forEach((btn) =>
    btn.addEventListener('click', async () => {
      const box = btn.closest('.item');
      btn.disabled = true;
      try {
        await api(`/api/board/${state.currentLangId}/labels/${box.dataset.label}/claim`, {
          method: 'POST',
          body: JSON.stringify({
            source_rev: Number(box.dataset.sourceRev),
            trans_rev: Number(box.dataset.transRev),
          }),
        });
        toast('已认领 30 分钟：期间仅你可以确认，可提前释放');
        await loadBoard();
      } catch (e) {
        toast(e.message, true);
        btn.disabled = false;
      }
    }));

  document.querySelectorAll('.release-claim').forEach((btn) =>
    btn.addEventListener('click', async () => {
      const box = btn.closest('.item');
      btn.disabled = true;
      try {
        await api(`/api/board/${state.currentLangId}/labels/${box.dataset.label}/claim/release`, {
          method: 'POST',
        });
        toast('已释放认领，其他账号现在可以认领或确认');
        await loadBoard();
      } catch (e) {
        toast(e.message, true);
        btn.disabled = false;
      }
    }));
}

// ---------------------------- 认领倒计时 ----------------------------

// 服务器时间为 UTC（格式 YYYY-MM-DD HH:MM:SS）；expires 字符串补 Z 后按 UTC 解析
function claimRemaining(expires) {
  const ms = Date.parse(expires.replace(' ', 'T') + 'Z') - Date.now();
  return Math.max(0, Math.floor(ms / 1000));
}

function fmtCountdown(sec) {
  const m = Math.floor(sec / 60);
  const s = sec % 60;
  return `${m}:${String(s).padStart(2, '0')}`;
}

function tickClaimCountdowns() {
  let expired = false;
  document.querySelectorAll('.claim-countdown').forEach((el) => {
    const sec = claimRemaining(el.dataset.expires);
    el.textContent = fmtCountdown(sec);
    if (sec <= 0) expired = true;
  });
  // 有认领刚到期：刷新一次看板，使“接手/确认”按钮立即可用（仅安排一次）
  if (expired && tickClaimCountdowns._board) {
    tickClaimCountdowns._board = false;
    loadBoard().finally(() => { tickClaimCountdowns._board = true; });
  }
}

tickClaimCountdowns._board = true;
setInterval(tickClaimCountdowns, 1000);

// ---------------------------- 发布快照 ----------------------------

async function publishSnapshot() {
  try {
    const r = await api(`/api/board/${state.currentLangId}/publish`, { method: 'POST' });
    toast(`快照 v${r.version} 已发布，内容不可变`);
    await Promise.all([loadBoard(), loadSnapshots()]);
  } catch (e) { toast(e.message, true); }
}

async function loadSnapshots() {
  const snaps = await api('/api/snapshots');
  const el = $('#snapshot-list');
  if (!snaps.length) {
    el.innerHTML = '<span class="muted">尚无已发布快照。</span>';
    return;
  }
  el.classList.remove('muted');
  el.innerHTML = snaps.map((s) => `
    <div class="item" style="display:flex;align-items:center;gap:10px;flex-wrap:wrap">
      <span class="tag ready">v${s.version}</span>
      <b>${esc(s.name)}</b> <span class="muted">${esc(s.code)}</span>
      ${s.batch_id ? `<span class="tag ready">同批 #${s.batch_id}</span>` : ''}
      <span class="muted">${esc(s.published_at)} · ${esc(s.published_by_name || '?')}</span>
      <div class="spacer" style="flex:1"></div>
      <button class="small ghost view-snap" data-id="${s.id}">查看内容</button>
    </div>`).join('');
  el.querySelectorAll('.view-snap').forEach((btn) =>
    btn.addEventListener('click', () => viewSnapshot(btn.dataset.id)));
}

async function viewSnapshot(id) {
  const s = await api('/api/snapshots/' + id);
  $('#snap-title').textContent = `${s.payload.language_name} 快照 v${s.version}`;
  $('#snap-body').innerHTML =
    `<div class="snap-meta">发布于 ${esc(s.published_at)} · 快照 ID ${s.id}（内容不可变）</div>` +
    s.payload.entries.map((e) => `
      <div class="visitor-entry">
        <div class="meta">展签 #${e.label_id} · 原文 rev ${e.source_rev} · 译文 rev ${e.trans_rev}</div>
        <div class="zh">${esc(e.source_text)}</div>
        <div class="t">${esc(e.text)}</div>
      </div>`).join('');
  $('#snapshot-modal').hidden = false;
}

init().catch((e) => toast(e.message, true));
