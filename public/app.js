/* =========================================================
   经意 · 前端
   ========================================================= */
(() => {
  'use strict';

  const $ = (s, r = document) => r.querySelector(s);
  const $$ = (s, r = document) => Array.from(r.querySelectorAll(s));
  const LS = {
    get(k, d) { try { const v = localStorage.getItem(k); return v ? JSON.parse(v) : d; } catch { return d; } },
    set(k, v) { try { localStorage.setItem(k, JSON.stringify(v)); } catch { } },
    del(k) { try { localStorage.removeItem(k); } catch { } },
  };

  const PRESETS = {
    deepseek: { base: 'https://api.deepseek.com/v1', model: 'deepseek-chat' },
    openai: { base: 'https://api.openai.com/v1', model: 'gpt-4o-mini' },
    silicon: { base: 'https://api.siliconflow.cn/v1', model: 'Qwen/Qwen2.5-7B-Instruct' },
    zhipu: { base: 'https://open.bigmodel.cn/api/paas/v4', model: 'glm-4-flash' },
  };

  const state = {
    mode: 'fast',
    query: '',
    lastResult: null,
    busy: false,
    cfg: LS.get('jingyi.cfg', { baseUrl: '', apiKey: '', model: '' }),
  };

  /* ---------------------------------------------------- DOM */
  const el = {
    form: $('#search-form'),
    q: $('#q'),
    btnSearch: $('#btn-search'),
    hero: $('#hero'),
    resultsWrap: $('#results-wrap'),
    results: $('#results'),
    empty: $('#empty'),
    notice: $('#notice'),
    aiPanel: $('#ai-panel'),
    aiAnalysis: $('#ai-analysis'),
    aiThemes: $('#ai-themes'),
    aiKws: $('#ai-kws'),
    resultCount: $('#result-count'),
    resultsMeta: $('#results-meta'),
    modeSwitch: $('#mode-switch'),
    daily: $('#daily'),
    dailyText: $('#daily-text'),
    dailyRef: $('#daily-ref'),
    historyWrap: $('#history-wrap'),
    historyList: $('#history-list'),
    drawer: $('#drawer'),
    drawerMask: $('#drawer-mask'),
    modal: $('#modal'),
    modalMask: $('#modal-mask'),
    modalTitle: $('#modal-title'),
    modalBody: $('#modal-body'),
    toast: $('#toast'),
    aiDot: $('#ai-dot'),
    footStats: $('#foot-stats'),
  };

  /* ---------------------------------------------------- 工具 */
  function toast(msg, ms = 1900) {
    el.toast.textContent = msg;
    el.toast.hidden = false;
    clearTimeout(toast._t);
    toast._t = setTimeout(() => { el.toast.hidden = true; }, ms);
  }

  function esc(s) {
    return String(s).replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  }

  async function api(path, body) {
    const opt = body
      ? { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) }
      : { method: 'GET' };
    const r = await fetch(path, opt);
    let data = null;
    try { data = await r.json(); } catch { }
    if (!r.ok) throw new Error((data && data.error) || `请求失败 (${r.status})`);
    return data;
  }

  function aiReady() {
    return !!(state.cfg.apiKey && state.cfg.baseUrl);
  }

  function refreshAiDot() {
    el.aiDot.classList.toggle('on', aiReady());
    el.aiDot.title = aiReady() ? `已配置：${state.cfg.model || '默认模型'}` : '未配置 AI';
  }

  /* ------------------------------------------------ 高亮 */
  function collectTerms(q, res) {
    const set = new Set();
    if (res) {
      (res.keywords || []).forEach(k => { if (k) set.add(String(k)); });
      (res.concepts || []).forEach(k => { if (k) set.add(String(k)); });
    }
    String(q).split(/[^\u4e00-\u9fffA-Za-z]+/).forEach(seg => {
      if (seg.length >= 2 && seg.length <= 8) set.add(seg);
    });
    return Array.from(set)
      .filter(t => t.length >= 2 && t.length <= 10)
      .sort((a, b) => b.length - a.length)
      .slice(0, 40);
  }

  function highlight(text, terms) {
    if (!terms || !terms.length) return esc(text);
    const ranges = [];
    for (const t of terms) {
      let from = 0;
      while (true) {
        const i = text.indexOf(t, from);
        if (i < 0) break;
        ranges.push([i, i + t.length]);
        from = i + 1;
        if (ranges.length > 60) break;
      }
    }
    if (!ranges.length) return esc(text);
    ranges.sort((a, b) => a[0] - b[0] || b[1] - a[1]);
    // 合并重叠
    const merged = [];
    for (const r of ranges) {
      const last = merged[merged.length - 1];
      if (last && r[0] <= last[1]) last[1] = Math.max(last[1], r[1]);
      else merged.push([r[0], r[1]]);
    }
    let out = '';
    let pos = 0;
    for (const [a, b] of merged) {
      if (a > pos) out += esc(text.slice(pos, a));
      out += '<mark>' + esc(text.slice(a, b)) + '</mark>';
      pos = b;
    }
    out += esc(text.slice(pos));
    return out;
  }

  /* ------------------------------------------------ 渲染 */
  function renderResult(res) {
    state.lastResult = res;
    const list = res.results || [];
    el.resultsWrap.hidden = false;
    el.hero.classList.add('compact');
    el.resultCount.textContent = list.length ? `${list.length} 节` : '';
    el.resultsMeta.textContent = [
      res.took != null ? `${res.took} ms` : '',
      res.mode === 'deep' ? 'AI 语义重排' : '本地 BM25',
    ].filter(Boolean).join(' · ');

    // AI 分析面板
    if (res.analysis || (res.themes && res.themes.length) || (res.keywords && res.keywords.length)) {
      el.aiPanel.hidden = false;
      el.aiAnalysis.textContent = res.analysis || '';
      el.aiAnalysis.hidden = !res.analysis;
      el.aiThemes.innerHTML = (res.themes || []).map(t => `<span>${esc(t)}</span>`).join('');
      el.aiThemes.hidden = !(res.themes && res.themes.length);
      el.aiKws.innerHTML = (res.keywords || []).map(k => `<span>${esc(k)}</span>`).join('');
      el.aiKws.hidden = !(res.keywords && res.keywords.length);
    } else {
      el.aiPanel.hidden = true;
    }

    // 提示
    if (res.warning) {
      el.notice.hidden = false;
      el.notice.textContent = res.warning;
    } else {
      el.notice.hidden = true;
    }

    const terms = collectTerms(res.query || state.query, res);
    el.results.innerHTML = list.map((r, i) => cardHTML(r, i, terms)).join('');
    el.empty.hidden = list.length > 0;

    // 绑定事件
    $$('.verse-card', el.results).forEach(card => {
      const idx = Number(card.dataset.i);
      const r = list[idx];
      card.querySelector('.vc-ref').addEventListener('click', () => openChapter(r));
      card.querySelector('[data-act="copy"]').addEventListener('click', () => copyVerse(r));
      card.querySelector('[data-act="chapter"]').addEventListener('click', () => openChapter(r));
      card.querySelector('[data-act="explain"]').addEventListener('click', () => explainVerse(r, card));
    });

    if (list.length) {
      const top = list[0];
      el.resultsMeta.title = `最相近：${top.ref}`;
    }
  }

  function cardHTML(r, i, terms) {
    const rel = Math.max(4, Math.min(100, r.rel || 0));
    const text = highlight(r.text || '', terms);
    const reason = r.reason
      ? `<div class="vc-reason"><b>为什么</b><span>${esc(r.reason)}</span></div>`
      : '';
    return `
      <li class="verse-card${i === 0 ? ' ai-top' : ''}" data-i="${i}" style="animation-delay:${Math.min(i * 28, 300)}ms">
        <div class="vc-head">
          <span class="vc-rank">${String(i + 1).padStart(2, '0')}</span>
          <span class="vc-ref" role="button" tabindex="0">${esc(r.ref)}</span>
          <span class="vc-rel">
            <span class="vc-bar"><i style="width:${rel}%"></i></span>
            ${Math.round(rel)}%
          </span>
        </div>
        <p class="vc-text">${text}</p>
        ${reason}
        <div class="vc-tools">
          <button class="tool-btn" data-act="copy" type="button">复制</button>
          <button class="tool-btn" data-act="chapter" type="button">读整章</button>
          <button class="tool-btn" data-act="explain" type="button">AI 解读</button>
        </div>
        <div class="vc-explain" hidden></div>
      </li>`;
  }

  /* ------------------------------------------------ 交互 */
  async function doSearch(q) {
    q = (q || '').trim();
    if (!q) { el.q.focus(); return; }
    if (state.busy) return;

    state.busy = true;
    state.query = q;
    el.btnSearch.classList.add('loading');
    el.btnSearch.disabled = true;
    el.resultsWrap.hidden = false;
    el.hero.classList.add('compact');
    el.empty.hidden = true;
    el.notice.hidden = true;
    el.aiPanel.hidden = true;
    el.results.innerHTML = skeleton();

    const mode = state.mode;
    if (mode === 'deep' && !aiReady()) {
      toast('尚未配置 AI 接口，已使用本地检索');
    }

    try {
      const res = await api('/api/search', {
        q,
        mode: mode === 'deep' && aiReady() ? 'deep' : 'fast',
        limit: 12,
        llm: state.cfg,
      });
      renderResult(res);
      pushHistory(q);
      window.scrollTo({ top: Math.max(0, el.resultsWrap.offsetTop - 90), behavior: 'smooth' });
    } catch (e) {
      el.results.innerHTML = '';
      el.empty.hidden = false;
      el.notice.hidden = false;
      el.notice.textContent = '检索失败：' + e.message;
    } finally {
      state.busy = false;
      el.btnSearch.classList.remove('loading');
      el.btnSearch.disabled = false;
    }
  }

  function skeleton() {
    return Array.from({ length: 3 }).map(() => `
      <li class="verse-card" style="opacity:.5">
        <div class="vc-head"><span class="vc-rank">··</span><span class="vc-ref">检索中…</span></div>
        <p class="vc-text" style="color:#4d515a">正在比对全本经文…</p>
      </li>`).join('');
  }

  async function copyVerse(r) {
    const text = `「${r.text}」\n—— ${r.ref}（和合本）`;
    try {
      await navigator.clipboard.writeText(text);
      toast('已复制经文');
    } catch {
      const ta = document.createElement('textarea');
      ta.value = text; document.body.appendChild(ta); ta.select();
      try { document.execCommand('copy'); toast('已复制经文'); } catch { toast('复制失败'); }
      document.body.removeChild(ta);
    }
  }

  async function explainVerse(r, card) {
    const box = card.querySelector('.vc-explain');
    if (!box.hidden) { box.hidden = true; return; }
    box.hidden = false;
    if (!aiReady()) {
      box.innerHTML = '<span class="load">请先在「AI 设置」里填写接口，才能使用 AI 解读。</span>';
      return;
    }
    box.innerHTML = '<span class="load">正在解读…</span>';
    try {
      const res = await api('/api/explain', {
        ref: r.ref, text: r.text, q: state.query, llm: state.cfg,
      });
      box.textContent = res.content || '';
    } catch (e) {
      box.innerHTML = '<span class="load">解读失败：' + esc(e.message) + '</span>';
    }
  }

  async function openChapter(r) {
    el.modal.hidden = false;
    el.modalMask.hidden = false;
    el.modalTitle.textContent = `${r.book} 第 ${r.chapter} 章`;
    el.modalBody.innerHTML = '<p style="color:var(--muted);font-size:13px">加载中…</p>';
    try {
      const data = await api(`/api/chapter?b=${r.bookId}&c=${r.chapter}`);
      el.modalBody.innerHTML = data.verses.map(v => `
        <div class="chapter-line" ${v.verse === r.verse ? 'style="background:rgba(217,179,116,.07);border-radius:8px;padding-left:8px"' : ''}>
          <span class="no">${v.verse}</span>
          <span>${esc(v.text)}</span>
        </div>`).join('');
    } catch (e) {
      el.modalBody.innerHTML = `<p style="color:var(--danger)">${esc(e.message)}</p>`;
    }
  }

  function closeModal() {
    el.modal.hidden = true;
    el.modalMask.hidden = true;
  }

  /* ------------------------------------------------ 历史 */
  function pushHistory(q) {
    let list = LS.get('jingyi.history', []);
    list = list.filter(x => x !== q);
    list.unshift(q);
    list = list.slice(0, 8);
    LS.set('jingyi.history', list);
    renderHistory();
  }

  function renderHistory() {
    const list = LS.get('jingyi.history', []);
    if (!list.length) { el.historyWrap.hidden = true; return; }
    el.historyWrap.hidden = false;
    el.historyList.innerHTML = list
      .map(q => `<button type="button" data-q="${esc(q)}">${esc(q)}</button>`)
      .join('');
    $$('button', el.historyList).forEach(b => {
      b.addEventListener('click', () => {
        el.q.value = b.dataset.q;
        autoGrow();
        doSearch(b.dataset.q);
      });
    });
  }

  /* ------------------------------------------------ 设置 */
  function openDrawer() {
    el.drawer.hidden = false;
    el.drawerMask.hidden = false;
    el.drawer.style.animation = 'none';
    void el.drawer.offsetWidth;
    el.drawer.style.animation = '';
    $('#cfg-base').value = state.cfg.baseUrl || '';
    $('#cfg-key').value = state.cfg.apiKey || '';
    $('#cfg-model').value = state.cfg.model || '';
    $('#test-result').hidden = true;
  }
  function closeDrawer() {
    el.drawer.hidden = true;
    el.drawerMask.hidden = true;
  }

  function saveCfg() {
    state.cfg = {
      baseUrl: $('#cfg-base').value.trim() || PRESETS.deepseek.base,
      apiKey: $('#cfg-key').value.trim(),
      model: $('#cfg-model').value.trim() || PRESETS.deepseek.model,
    };
    LS.set('jingyi.cfg', state.cfg);
    refreshAiDot();
    toast(state.cfg.apiKey ? '已保存，可切换到「AI 深度理解」' : '已保存（未填 Key，仅本地检索）');
  }

  async function testCfg() {
    const box = $('#test-result');
    box.hidden = false;
    box.className = 'test-result';
    box.textContent = '正在连接…';
    const cfg = {
      baseUrl: $('#cfg-base').value.trim() || PRESETS.deepseek.base,
      apiKey: $('#cfg-key').value.trim(),
      model: $('#cfg-model').value.trim() || PRESETS.deepseek.model,
    };
    if (!cfg.apiKey) {
      box.className = 'test-result bad';
      box.textContent = '请先填写 API Key。';
      return;
    }
    try {
      const r = await api('/api/test', { llm: cfg });
      box.className = 'test-result ok';
      box.textContent = `连接成功（${r.model || cfg.model}，${r.ms} ms）。返回：${r.reply || ''}`;
    } catch (e) {
      box.className = 'test-result bad';
      box.textContent = '连接失败：' + e.message;
    }
  }

  /* ------------------------------------------------ 启动 */
  function autoGrow() {
    const t = el.q;
    t.style.height = 'auto';
    t.style.height = Math.min(t.scrollHeight, 190) + 'px';
  }

  async function loadShell() {
    try {
      const d = await api('/api/daily');
      if (d && d.text) {
        el.daily.hidden = false;
        el.dailyText.textContent = '「' + d.text + '」';
        el.dailyRef.textContent = d.ref + ' · 和合本';
      }
    } catch { }

    try {
      const st = await api('/api/stats');
      el.footStats.textContent = `${st.verses.toLocaleString()} 节 · 索引 ${st.sentences.toLocaleString()} 句`;
    } catch { }
  }

  function bind() {
    el.form.addEventListener('submit', e => {
      e.preventDefault();
      doSearch(el.q.value);
    });
    el.q.addEventListener('input', autoGrow);
    el.q.addEventListener('keydown', e => {
      if (e.key === 'Enter' && !e.shiftKey && !e.isComposing) {
        e.preventDefault();
        doSearch(el.q.value);
      }
    });

    el.modeSwitch.addEventListener('click', e => {
      const b = e.target.closest('button[data-mode]');
      if (!b) return;
      state.mode = b.dataset.mode;
      $$('button', el.modeSwitch).forEach(x => x.classList.toggle('active', x === b));
      if (state.mode === 'deep' && !aiReady()) {
        toast('AI 模式下需先在右上角「AI 设置」填写接口');
      }
    });

    $('#chips').addEventListener('click', e => {
      const b = e.target.closest('.chip[data-q]');
      if (!b) return;
      el.q.value = b.dataset.q;
      autoGrow();
      doSearch(b.dataset.q);
    });

    $('#btn-daily').addEventListener('click', async () => {
      try {
        const v = await api('/api/random');
        if (v && v.text) {
          el.daily.hidden = false;
          el.dailyText.textContent = '「' + v.text + '」';
          el.dailyRef.textContent = v.ref + ' · 和合本';
          el.daily.scrollIntoView({ block: 'center', behavior: 'smooth' });
        }
      } catch { toast('获取失败'); }
    });

    $('#btn-settings').addEventListener('click', openDrawer);
    $('#btn-close-drawer').addEventListener('click', closeDrawer);
    el.drawerMask.addEventListener('click', closeDrawer);
    el.modalMask.addEventListener('click', closeModal);
    $('#btn-close-modal').addEventListener('click', closeModal);

    $('#btn-save-cfg').addEventListener('click', saveCfg);
    $('#btn-test-cfg').addEventListener('click', testCfg);
    $('#btn-clear-cfg').addEventListener('click', () => {
      state.cfg = { baseUrl: '', apiKey: '', model: '' };
      LS.del('jingyi.cfg');
      $('#cfg-base').value = '';
      $('#cfg-key').value = '';
      $('#cfg-model').value = '';
      $('#test-result').hidden = true;
      refreshAiDot();
      toast('已清除设置');
    });

    $$('.preset-row .chip').forEach(b => {
      b.addEventListener('click', () => {
        const p = PRESETS[b.dataset.preset];
        if (!p) return;
        $('#cfg-base').value = p.base;
        $('#cfg-model').value = p.model;
        toast(`已填入 ${b.textContent} 默认地址，请补充 Key`);
      });
    });

    document.addEventListener('keydown', e => {
      if (e.key === 'Escape') { closeDrawer(); closeModal(); }
      if (e.key === '/' && document.activeElement !== el.q && !/input|textarea/i.test(document.activeElement.tagName)) {
        e.preventDefault();
        el.q.focus();
      }
    });

    // 支持 URL 直接带查询：?q=...
    const uq = new URLSearchParams(location.search).get('q');
    if (uq) {
      el.q.value = uq;
      autoGrow();
      doSearch(uq);
    }
  }

  function init() {
    refreshAiDot();
    renderHistory();
    bind();
    loadShell();
    autoGrow();
  }

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init);
  else init();
})();
