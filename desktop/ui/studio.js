/* ---------------------------------------------------------------------------
   AhoosAI Studio 3.0 -- the page.

   One file, no framework, no build step: the studio runs offline and is
   debugged in place. Four views -- chat, battle, models, settings -- over one
   status object polled from the local server, and two streams: a chat turn and
   a battle, both read as server-sent events from a POST.

   Everything the model writes goes through MD.render, which escapes first.
   --------------------------------------------------------------------------- */
(() => {
  'use strict';

  const $ = (s, r = document) => r.querySelector(s);
  const $$ = (s, r = document) => [...r.querySelectorAll(s)];
  const esc = window.MD.esc;
  const ic = (name, cls = '') => `<svg class="i ${cls}" aria-hidden="true"><use href="#i-${name}"/></svg>`;
  const h = html => { const t = document.createElement('template'); t.innerHTML = html.trim(); return t.content.firstElementChild; };
  const sleep = ms => new Promise(r => setTimeout(r, ms));
  const store = {
    get: k => { try { return localStorage.getItem('ahoos.' + k); } catch (e) { return null; } },
    set: (k, v) => { try { localStorage.setItem('ahoos.' + k, v); } catch (e) { /* private mode */ } },
  };

  const S = {
    status: null, lang: 'fa', view: 'chat',
    chats: [], chatId: null, chat: null, search: '',
    run: null, attachments: [], plan: false,
    chips: [],
    battles: [], score: null, battle: null, battleRun: null,
    hub: { model: null, adapter: null, sel: { model: '', adapter: '' }, busy: {} },
    settingsTab: 'internet',
  };

  /* ---- words and numbers --------------------------------------------------- */

  function t(key, vars) {
    const entry = window.I18N[key];
    let text = entry ? (entry[S.lang] ?? entry.en) : key;
    if (vars) for (const k of Object.keys(vars)) text = text.split('{' + k + '}').join(vars[k]);
    return text;
  }
  const locale = () => (S.lang === 'fa' ? 'fa-IR' : 'en-US');
  const num = (n, d = 0) => new Intl.NumberFormat(locale(), { maximumFractionDigits: d }).format(n || 0);
  function bytes(b) {
    if (!b) return num(0) + ' B';
    const units = ['B', 'KB', 'MB', 'GB', 'TB'];
    let i = 0;
    while (b >= 1000 && i < units.length - 1) { b /= 1000; i++; }
    return num(b, b < 10 && i > 1 ? 2 : 1) + ' ' + units[i];
  }
  function dur(s) {
    if (s == null || !isFinite(s)) return '';
    s = Math.round(s);
    if (s < 60) return t('sec', { n: num(s) });
    const m = Math.floor(s / 60);
    if (m < 60) return t('min_sec', { m: num(m), s: num(s % 60) });
    return t('hour_min', { h: num(Math.floor(m / 60)), m: num(m % 60) });
  }
  function dayGroup(ts) {
    const d = new Date(ts * 1000), now = new Date();
    const days = Math.floor((new Date(now.toDateString()) - new Date(d.toDateString())) / 864e5);
    if (days <= 0) return t('today');
    if (days === 1) return t('yesterday');
    if (days < 7) return t('this_week');
    if (days < 31) return t('this_month');
    return t('older');
  }

  /* ---- the server ---------------------------------------------------------- */

  async function api(path, body, method) {
    const options = { method: method || (body === undefined ? 'GET' : 'POST') };
    if (body !== undefined) {
      options.headers = { 'Content-Type': 'application/json' };
      options.body = JSON.stringify(body);
    }
    const response = await fetch(path, options);
    if (!response.ok) {
      let detail = await response.text();
      try { detail = JSON.parse(detail).detail || detail; } catch (e) { /* plain text */ }
      throw new Error(String(detail).slice(0, 400));
    }
    return response.json();
  }

  async function sse(path, body, on, signal) {
    const response = await fetch(path, {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body), signal,
    });
    if (!response.ok) {
      let detail = await response.text();
      try { detail = JSON.parse(detail).detail || detail; } catch (e) { /* plain */ }
      throw new Error(String(detail).slice(0, 400));
    }
    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let buffer = '';
    for (;;) {
      const { value, done } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      let cut;
      while ((cut = buffer.indexOf('\n\n')) >= 0) {
        const frame = buffer.slice(0, cut);
        buffer = buffer.slice(cut + 2);
        let event = 'message', data = '';
        for (const line of frame.split('\n')) {
          if (line.startsWith('event:')) event = line.slice(6).trim();
          else if (line.startsWith('data:')) data += line.slice(5).trim();
        }
        if (!data) continue;
        try { on(event, JSON.parse(data)); } catch (e) { console.error(e); }
      }
    }
  }

  /* ---- toasts, modals, pop-overs ---------------------------------------------- */

  function toast(text, kind = 'info', ms = 4200) {
    const icon = { ok: 'check', bad: 'alert', info: 'info' }[kind] || 'info';
    const node = h(`<div class="toast ${kind}">${ic(icon)}<div>${esc(text)}</div></div>`);
    $('#toasts').appendChild(node);
    setTimeout(() => { node.classList.add('out'); setTimeout(() => node.remove(), 300); }, ms);
  }
  const fail = error => toast(String(error && error.message ? error.message : error), 'bad', 6500);

  function modal({ title, text = '', body = null, ok = t('ok'), cancel = t('cancel'), danger = false, wide = false }) {
    return new Promise(resolve => {
      const scrim = h(`<div class="scrim"><div class="modal"${wide ? ' style="width:min(720px,100%)"' : ''}>
        <h3>${esc(title)}</h3>${text ? `<p>${esc(text)}</p>` : ''}<div class="mbody"></div>
        <div class="acts"><button class="btn ghost" data-a="no">${esc(cancel)}</button>
        <button class="btn ${danger ? 'danger' : 'primary'}" data-a="yes">${esc(ok)}</button></div></div></div>`);
      if (body) $('.mbody', scrim).appendChild(body);
      const close = value => {
        scrim.classList.add('closing');
        setTimeout(() => scrim.remove(), 150);
        document.removeEventListener('keydown', keys, true);
        resolve(value);
      };
      const keys = e => {
        if (e.key === 'Escape') { e.stopPropagation(); close(null); }
        if (e.key === 'Enter' && !(e.target instanceof HTMLTextAreaElement)) { e.preventDefault(); close(collect()); }
      };
      const collect = () => {
        const field = $('input,textarea', scrim);
        return field ? field.value : true;
      };
      scrim.addEventListener('click', e => { if (e.target === scrim) close(null); });
      $('[data-a="no"]', scrim).onclick = () => close(null);
      $('[data-a="yes"]', scrim).onclick = () => close(collect());
      document.addEventListener('keydown', keys, true);
      document.body.appendChild(scrim);
      setTimeout(() => { const f = $('input,textarea', scrim); if (f) { f.focus(); f.select(); } }, 60);
    });
  }
  const ask = (title, value = '', placeholder = '') =>
    modal({ title, body: h(`<input class="input" value="${esc(value)}" placeholder="${esc(placeholder)}" dir="auto">`) });

  let openPop = null;
  function closePop() {
    if (!openPop) return;
    const { node, anchor } = openPop;
    openPop = null;
    if (anchor) anchor.setAttribute('aria-expanded', 'false');
    node.classList.add('closing');
    setTimeout(() => node.remove(), 130);
  }
  function popover(anchor, content, { up = false, align = 'start', width } = {}) {
    if (openPop && openPop.anchor === anchor) { closePop(); return null; }
    closePop();
    const node = h('<div class="pop scroll" role="menu"></div>');
    if (typeof content === 'string') node.innerHTML = content; else node.appendChild(content);
    if (width) node.style.width = width + 'px';
    document.body.appendChild(node);
    const a = anchor.getBoundingClientRect();
    const box = node.getBoundingClientRect();
    const rtl = document.documentElement.dir === 'rtl';
    let x = (align === 'end') !== rtl ? a.right - box.width : a.left;
    x = Math.max(8, Math.min(x, innerWidth - box.width - 8));
    let y = up ? a.top - box.height - 8 : a.bottom + 8;
    if (!up && y + box.height > innerHeight - 8) y = Math.max(8, a.top - box.height - 8);
    node.style.left = x + 'px';
    node.style.top = Math.max(8, y) + 'px';
    node.style.setProperty('--origin', (up ? 'bottom ' : 'top ') + ((align === 'end') !== rtl ? 'right' : 'left'));
    anchor.setAttribute('aria-expanded', 'true');
    openPop = { node, anchor };
    return node;
  }
  document.addEventListener('pointerdown', e => {
    if (openPop && !openPop.node.contains(e.target) && !openPop.anchor.contains(e.target)) closePop();
  });

  function seg(options, value, onPick) {
    const node = h('<div class="seg" role="radiogroup"><span class="thumb"></span></div>');
    for (const [v, label] of options) {
      const b = h(`<button type="button" role="radio" data-v="${esc(v)}">${label}</button>`);
      b.onclick = () => { set(v); onPick(v); };
      node.appendChild(b);
    }
    const set = v => {
      $$('button', node).forEach(b => { const on = b.dataset.v === String(v); b.classList.toggle('on', on); b.setAttribute('aria-checked', on); });
      requestAnimationFrame(() => {
        const on = $('button.on', node);
        const thumb = $('.thumb', node);
        if (on) { thumb.style.left = on.offsetLeft + 'px'; thumb.style.width = on.offsetWidth + 'px'; }
      });
    };
    set(value);
    node.set = set;
    return node;
  }
  function switcher(on, onFlip) {
    const node = h(`<button type="button" class="switch${on ? ' on' : ''}" role="switch" aria-checked="${!!on}"></button>`);
    node.onclick = () => { const v = !node.classList.contains('on'); node.classList.toggle('on', v); node.setAttribute('aria-checked', v); onFlip(v); };
    return node;
  }
  function rangeFill(input) {
    const set = () => input.style.setProperty('--fill', ((input.value - input.min) / (input.max - input.min) * 100) + '%');
    input.addEventListener('input', set);
    set();
    return input;
  }

  async function copy(text, button) {
    try { await navigator.clipboard.writeText(text); } catch (e) {
      const area = h('<textarea style="position:fixed;opacity:0"></textarea>');
      area.value = text; document.body.appendChild(area); area.select(); document.execCommand('copy'); area.remove();
    }
    if (button) {
      const was = button.innerHTML;
      button.classList.add('done');
      button.innerHTML = ic('check', 'sm') + t('copied');
      setTimeout(() => { button.classList.remove('done'); button.innerHTML = was; }, 1400);
    }
  }

  /* ---- look: language, theme, motion ------------------------------------------ */

  function applyLook() {
    const settings = (S.status && S.status.settings) || {};
    let lang = settings.language || store.get('lang');
    if (!lang) {
      const tz = (Intl.DateTimeFormat().resolvedOptions().timeZone || '');
      lang = tz === 'Asia/Tehran' || (navigator.languages || []).some(l => l.startsWith('fa')) ? 'fa' : 'en';
    }
    const changed = lang !== S.lang;
    S.lang = lang;
    store.set('lang', lang);
    document.documentElement.lang = lang;
    document.documentElement.dir = lang === 'fa' ? 'rtl' : 'ltr';
    const theme = settings.theme || store.get('theme') || 'system';
    store.set('theme', theme);
    const dark = theme === 'dark' || (theme === 'system' && matchMedia('(prefers-color-scheme: dark)').matches);
    document.documentElement.dataset.theme = dark ? 'dark' : 'light';
    document.documentElement.dataset.motion = settings.motion === 'reduced' ? 'reduced' : 'full';
    return changed;
  }
  matchMedia('(prefers-color-scheme: dark)').addEventListener('change', applyLook);

  async function setting(patch, quiet) {
    try {
      S.status = await api('/local/settings', patch);
      if (applyLook()) renderAll();
      if (!quiet) toast(t('saved'), 'ok', 1800);
      return S.status;
    } catch (e) { fail(e); return null; }
  }

  /* ---- status ------------------------------------------------------------------ */

  let pollTimer = null;
  async function refresh() {
    clearTimeout(pollTimer);
    try {
      const was = S.status;
      S.status = await api('/local/status');
      if (!was && applyLook()) { /* first load decides the language */ }
      paintStatus(was);
    } catch (e) { /* the server is starting or stopping */ }
    const busy = S.status && (S.status.state === 'loading' || (S.status.jobs || []).some(j => ['queued', 'running', 'converting'].includes(j.state)));
    pollTimer = setTimeout(refresh, busy ? 1200 : 5000);
  }

  function paintStatus(was) {
    renderTop();
    renderFoot();
    if (S.view === 'models') updateModels();
    if (S.view === 'battle' && was && (was.profile || {}).name !== S.status.profile.name) renderBattle();
    const running = (S.status.jobs || []).filter(j => ['queued', 'running', 'converting'].includes(j.state)).length;
    const badge = $('#nav [data-view="models"] .count');
    if (badge) { badge.textContent = num(running); badge.style.display = running ? '' : 'none'; }
    if (was && was.state !== 'ready' && S.status.state === 'ready') toast(t('model_ready', { name: S.status.profile.name }), 'ok');
    if (was && was.state !== 'failed' && S.status.state === 'failed') toast(t('model_failed'), 'bad', 8000);
    if (was) {
      const before = Object.fromEntries((was.jobs || []).map(j => [j.id, j.state]));
      for (const job of S.status.jobs || []) {
        if (before[job.id] && before[job.id] !== 'done' && job.state === 'done') toast(t('download_done', { name: job.title }), 'ok');
        if (before[job.id] && before[job.id] !== 'failed' && job.state === 'failed') toast(t('download_failed', { name: job.title }), 'bad');
      }
    }
  }

  const ready = () => S.status && S.status.state === 'ready';
  const apex = () => S.status && S.status.profile.engine === 'apex';
  const settingsOf = () => (S.status && S.status.settings) || {};

  /* ---- the frame: sidebar and top bar ------------------------------------------ */

  function renderSide() {
    const side = $('#side');
    side.innerHTML = `
      <div class="side-head">
        <div class="brand"><svg class="mark" aria-hidden="true"><use href="#i-mark"/></svg>
          <div class="name">AhoosAI Studio<small>${t('offline_tag')}</small></div>
          <span class="ver ltr">${esc((S.status && S.status.version) || '3.0.0')}</span></div>
        <button class="icon-btn" id="hideSide" title="${t('hide_sidebar')} (Ctrl+B)">${ic('sidebar')}</button>
      </div>
      <button class="new-chat" id="newChat"><span class="plus">${ic('plus', 'sm')}</span>${t('new_chat')}<kbd class="ltr">Ctrl N</kbd></button>
      <nav class="nav" id="nav">
        <span class="nav-pill"></span>
        <button data-view="chat">${ic('chat')}${t('nav_chat')}</button>
        <button data-view="battle">${ic('swords')}${t('nav_battle')}</button>
        <button data-view="models">${ic('cube')}${t('nav_models')}<span class="count" style="display:none">0</span></button>
        <button data-view="settings">${ic('sliders')}${t('nav_settings')}</button>
      </nav>
      <div class="side-body" id="sideBody"></div>
      <div class="side-foot" id="sideFoot"></div>`;
    $('#hideSide').onclick = toggleSide;
    $('#newChat').onclick = () => newChat();
    $$('#nav button').forEach(b => { b.onclick = () => go(b.dataset.view); });
    renderSideBody();
    renderFoot(true);
    movePill();
  }

  function movePill() {
    const on = $(`#nav button[data-view="${S.view}"]`);
    $$('#nav button').forEach(b => b.classList.toggle('on', b === on));
    const pill = $('#nav .nav-pill');
    if (on && pill) pill.style.transform = `translateY(${on.offsetTop - 4}px)`;
  }

  function toggleSide() {
    const app = $('#app');
    if (innerWidth <= 980) { app.classList.toggle('side-open'); return; }
    app.classList.toggle('side-hidden');
    store.set('side', app.classList.contains('side-hidden') ? 'hidden' : 'shown');
  }

  function renderSideBody() {
    const body = $('#sideBody');
    if (!body) return;
    if (S.view === 'battle') { renderBattleSide(body); return; }
    body.innerHTML = `
      <label class="side-search">${ic('search', 'sm')}<input id="chatSearch" placeholder="${t('search_chats')}" value="${esc(S.search)}"></label>
      <div class="side-list scroll" id="chatList"></div>`;
    $('#chatSearch').oninput = e => { S.search = e.target.value; paintChats(); };
    paintChats();
  }

  function paintChats() {
    const list = $('#chatList');
    if (!list) return;
    const q = S.search.trim().toLowerCase();
    const rows = S.chats.filter(c => !q || (c.title || '').toLowerCase().includes(q));
    if (!rows.length) {
      list.innerHTML = `<div class="side-empty">${q ? t('no_matches') : t('no_chats')}</div>`;
      return;
    }
    list.innerHTML = '';
    let group = null;
    rows.forEach((chat, index) => {
      const g = chat.pinned ? t('pinned') : dayGroup(chat.updated);
      if (g !== group) { group = g; list.appendChild(h(`<div class="group-h">${esc(g)}</div>`)); }
      const live = S.run && S.run.chatId === chat.id;
      const row = h(`<div class="row${chat.id === S.chatId ? ' on' : ''}" role="button" tabindex="0" style="animation-delay:${Math.min(index, 12) * 18}ms">
        ${chat.pinned ? ic('pin', 'sm pin') : ''}
        <span class="ttl" dir="auto">${esc(chat.title || t('untitled'))}</span>
        ${live ? '<span class="spinner spin"></span>' : ''}
        <button class="icon-btn more" style="width:26px;height:26px" title="${t('more')}">${ic('dots', 'sm')}</button></div>`);
      row.onclick = e => { if (!e.target.closest('.more')) openChat(chat.id); };
      row.onkeydown = e => { if (e.key === 'Enter') openChat(chat.id); };
      $('.more', row).onclick = e => { e.stopPropagation(); chatMenu(e.currentTarget, chat); };
      list.appendChild(row);
    });
  }

  function chatMenu(anchor, chat) {
    const menu = h(`<div>
      <button class="item" data-a="rename">${ic('pencil', 'sm')}<span class="grow">${t('rename')}</span></button>
      <button class="item" data-a="pin">${ic('pin', 'sm')}<span class="grow">${chat.pinned ? t('unpin') : t('pin')}</span></button>
      <hr><button class="item danger" data-a="delete">${ic('trash', 'sm')}<span class="grow">${t('delete')}</span></button></div>`);
    popover(anchor, menu, { align: 'end' });
    $('[data-a="rename"]', menu).onclick = async () => {
      closePop();
      const title = await ask(t('rename_chat'), chat.title);
      if (title == null || !title.trim()) return;
      await api(`/local/chats/${chat.id}`, { title }).catch(fail);
      loadChats();
    };
    $('[data-a="pin"]', menu).onclick = async () => {
      closePop();
      await api(`/local/chats/${chat.id}`, { pinned: !chat.pinned }).catch(fail);
      loadChats();
    };
    $('[data-a="delete"]', menu).onclick = async () => {
      closePop();
      const sure = await modal({ title: t('delete_chat_q'), text: chat.title, ok: t('delete'), danger: true });
      if (!sure) return;
      await api(`/local/chats/${chat.id}`, undefined, 'DELETE').catch(fail);
      if (S.chatId === chat.id) newChat();
      loadChats();
    };
  }

  async function loadChats() {
    try { S.chats = await api('/local/chats'); } catch (e) { S.chats = []; }
    paintChats();
  }

  const sigs = {};
  const changed = (name, value) => {
    const sig = JSON.stringify(value) + S.lang;
    if (sigs[name] === sig) return false;
    sigs[name] = sig;
    return true;
  };

  function renderFoot(force) {
    const foot = $('#sideFoot');
    if (!foot || !S.status) return;
    const st = S.status;
    if (!force && foot.childElementCount && !changed('foot', [st.state, st.profile])) return;
    if (force) changed('foot', [st.state, st.profile]);
    const said = { ready: t('state_ready'), loading: t('state_loading'), failed: t('state_failed'), missing: t('state_missing') }[st.state];
    foot.innerHTML = `<button class="engine" id="engineBtn"><svg class="i" aria-hidden="true"><use href="#i-cube"/></svg>
      <span class="txt"><b dir="auto">${esc(st.profile.name)}</b><small><span class="state ${st.state}">${esc(said)}</span> · ${esc(st.profile.base || '—')}</small></span></button>`;
    $('#engineBtn').onclick = () => go('models');
  }

  function renderTop(force) {
    const top = $('#top');
    if (!S.status) return;
    const st = S.status;
    const sig = [st.state, st.profile, st.active, currentFolder(), (st.internet || {}).mode, settingsOf().web_on, settingsOf().theme];
    if (!changed('top', sig) && !force && top.childElementCount) return;
    if (openPop && top.contains(openPop.anchor)) closePop();
    const base = st.bases.find(b => b.key === st.active.base);
    const folderName = currentFolder() ? currentFolder().split(/[\\/]/).filter(Boolean).pop() : '';
    const internet = (st.internet || {}).mode || 'off';
    const webOn = internet !== 'off' && settingsOf().web_on;
    const theme = settingsOf().theme || 'system';
    top.innerHTML = `
      <button class="icon-btn" id="showSide" title="${t('toggle_sidebar')} (Ctrl+B)">${ic('sidebar')}</button>
      <button class="picker" id="picker" aria-haspopup="menu" aria-expanded="false">
        <span class="nm" dir="auto">${esc(st.profile.name)}</span>
        ${base && base.quant ? `<span class="q ltr">${esc(base.quant)}</span>` : ''}
        ${st.state !== 'ready' ? `<span class="state ${st.state}">${esc({ loading: t('state_loading'), failed: t('state_failed'), missing: t('state_missing') }[st.state] || '')}</span>` : ''}${ic('chev', 'sm chev')}</button>
      <span class="spacer"></span>
      <button class="chip${folderName ? ' on' : ' warn'}" id="folderChip" title="${esc(currentFolder() || t('no_folder'))}">${ic('folder', 'sm')}<span class="lbl" dir="auto">${esc(folderName || t('connect_folder'))}</span></button>
      <button class="chip${webOn ? ' on' : internet === 'off' ? ' warn' : ''}" id="webChip" title="${t('web_chip_title')}">${ic(internet === 'off' ? 'off' : 'globe', 'sm')}<span class="lbl">${webOn ? t('web_on') : internet === 'off' ? t('web_off') : t('web_ready')}</span></button>
      <button class="icon-btn" id="langBtn" title="${t('language')}"><b style="font-size:12px">${S.lang === 'fa' ? 'EN' : 'فا'}</b></button>
      <button class="icon-btn" id="themeBtn" title="${t('theme')}">${ic({ system: 'monitor', light: 'sun', dark: 'moon' }[theme])}</button>`;
    $('#showSide').onclick = toggleSide;
    $('#picker').onclick = e => modelMenu(e.currentTarget);
    $('#folderChip').onclick = e => folderMenu(e.currentTarget);
    $('#webChip').onclick = () => {
      if ((S.status.internet || {}).mode === 'off') {
        toast(t('web_setup_first'));
        S.settingsTab = 'internet';
        go('settings');
        return;
      }
      setting({ web_on: !settingsOf().web_on }, true);
    };
    $('#langBtn').onclick = () => setting({ language: S.lang === 'fa' ? 'en' : 'fa' }, true);
    $('#themeBtn').onclick = () => {
      const next = { system: 'light', light: 'dark', dark: 'system' }[theme];
      setting({ theme: next }, true).then(() => toast(t('theme_' + next), 'info', 1400));
    };
  }

  function combos() {
    const st = S.status;
    const out = [];
    for (const base of st.bases) {
      for (const adapter of st.adapters) {
        if (base.arch && adapter.arch && base.arch !== adapter.arch) continue;
        if (adapter.builtin && base.family && ('builtin:' + base.family) !== adapter.key) continue;
        if (adapter.builtin && !base.family && !base.arch) continue;
        out.push({ base, adapter, name: adapter.name, sub: `${base.name} · ${base.quant}` });
      }
      out.push({ base, adapter: null, name: base.name, sub: t('no_adapter_base', { q: base.quant || '' }) });
    }
    return out;
  }

  function modelMenu(anchor) {
    const st = S.status;
    const menu = h('<div></div>');
    menu.appendChild(h(`<div class="pop-h">${t('on_this_computer')}</div>`));
    const list = combos();
    if (!list.length) menu.appendChild(h(`<div class="side-empty">${t('no_models_yet')}</div>`));
    for (const c of list) {
      const on = st.active.base === c.base.key && st.active.adapter === (c.adapter ? c.adapter.key : '');
      const row = h(`<button class="item${on ? ' sel' : ''}">
        <span class="step-ico">${ic(c.adapter ? 'layers' : 'cube', 'sm')}</span>
        <span class="grow"><b dir="auto">${esc(c.name)}</b><small dir="auto">${esc(c.sub)}</small></span>
        ${on ? ic('check', 'tick') : ''}</button>`);
      row.onclick = async () => {
        closePop();
        if (on) return;
        try {
          S.status = await api('/local/select', { base: c.base.key, adapter: c.adapter ? c.adapter.key : '' });
          paintStatus(null);
          paintComposer();
          toast(t('switching_to', { name: c.name }));
          refresh();
        } catch (e) { fail(e); }
      };
      menu.appendChild(row);
    }
    menu.appendChild(h('<hr>'));
    const manage = h(`<button class="item">${ic('cube', 'sm')}<span class="grow"><b>${t('manage_models')}</b><small>${t('manage_models_sub')}</small></span></button>`);
    manage.onclick = () => { closePop(); go('models'); };
    menu.appendChild(manage);
    popover(anchor, menu, { width: 340 });
  }

  function folderMenu(anchor) {
    const folder = currentFolder();
    const menu = h('<div></div>');
    if (folder) {
      menu.appendChild(h(`<div class="pop-h">${t('working_folder')}</div>`));
      menu.appendChild(h(`<div style="padding:2px 10px 8px;font-family:var(--mono);font-size:12px;color:var(--muted);direction:ltr;text-align:start;word-break:break-all">${esc(folder)}</div>`));
    } else {
      menu.appendChild(h(`<div style="padding:10px 10px 6px;max-width:300px"><b>${t('no_folder_title')}</b><div class="muted" style="font-size:12.5px;margin-top:4px">${t('no_folder_text')}</div></div>`));
    }
    const add = (icon, label, fn, cls = '') => {
      const b = h(`<button class="item ${cls}">${ic(icon, 'sm')}<span class="grow">${label}</span></button>`);
      b.onclick = () => { closePop(); fn(); };
      menu.appendChild(b);
    };
    add('folder', folder ? t('change_folder') : t('choose_folder'), pickFolder);
    if (folder) {
      add('external', t('open_in_explorer'), () => api('/local/open-project', { folder }).catch(fail));
      add('terminal', t('terminal'), toggleTerminal);
      menu.appendChild(h('<hr>'));
      add('x', t('disconnect_folder'), async () => {
        await api('/local/set-folder', { folder: '', chat_id: S.chatId || '' }).catch(fail);
        if (S.chat) S.chat.folder = '';
        await refresh();
        renderTop(true);
        renderEmpty();
      }, 'danger');
    }
    popover(anchor, menu, { align: 'end', width: 320 });
  }

  async function pickFolder() {
    // The folder chosen in a conversation becomes that conversation's project.
    const keep = async folder => {
      if (S.chatId) {
        await api('/local/set-folder', { folder, chat_id: S.chatId }).catch(() => {});
        if (S.chat) S.chat.folder = folder;
      }
      toast(t('folder_connected', { name: folder }), 'ok');
      await refresh();
      renderTop(true);
      renderEmpty();
    };
    try {
      const answer = await api('/local/pick-folder', {});
      if (answer.folder) await keep(answer.folder);
    } catch (e) {
      const typed = await ask(t('folder_path_q'), currentFolder() || '', 'C:\\Projects\\my-app');
      if (!typed) return;
      try {
        await api('/local/set-folder', { folder: typed });
        await keep(typed);
      } catch (inner) { fail(inner); }
    }
  }

  /* ---- views --------------------------------------------------------------------- */

  function go(view) {
    if (view === S.view && $('#view-' + view).classList.contains('on')) return;
    const swap = () => {
      S.view = view;
      $$('.view').forEach(v => v.classList.toggle('on', v.id === 'view-' + view));
      movePill();
      renderSideBody();
      if (view === 'models') renderModels();
      if (view === 'settings') renderSettings();
      if (view === 'battle') renderBattle();
      if (view === 'chat') setTimeout(() => $('#input') && $('#input').focus(), 50);
      $('#app').classList.remove('side-open');
    };
    if (document.startViewTransition && !document.hidden && document.documentElement.dataset.motion !== 'reduced') {
      // A transition that cannot run (the window is minimised, another one is
      // under way) rejects; the swap has still happened, so that is not an error.
      const transition = document.startViewTransition(swap);
      transition.ready.catch(() => {});
      transition.finished.catch(() => {});
    } else swap();
  }

  /* ---- chat --------------------------------------------------------------------- */

  function renderChatView() {
    const view = $('#view-chat');
    view.innerHTML = `
      <div class="scroller scroll" id="scroller"><div class="empty" id="empty"></div><div class="thread" id="thread"></div></div>
      <div class="composer-wrap">
        <button class="jump" id="jump" title="${t('to_bottom')}">${ic('down', 'sm')}</button>
        <form class="composer" id="composer" autocomplete="off">
          <div class="attach-list" id="attachList"></div>
          <textarea id="input" rows="1" dir="auto" placeholder="${t('placeholder')}"></textarea>
          <div class="bar">
            <button type="button" class="tool" id="attachBtn" title="${t('attach')}">${ic('clip', 'sm')}</button>
            <button type="button" class="tool" id="skillsBtn" title="${t('skills')}">${ic('sparkle', 'sm')}<span id="skillsCount"></span></button>
            <button type="button" class="tool" id="webBtn" title="${t('web_chip_title')}">${ic('globe', 'sm')}<span>${t('search_web_short')}</span></button>
            <button type="button" class="tool" id="planBtn" title="${t('plan_title')}">${ic('plan', 'sm')}<span>${t('plan')}</span></button>
            <button type="button" class="tool" id="levelBtn" title="${t('level_title')}">${ic('bulb', 'sm')}<span id="levelLbl"></span></button>
            <button type="button" class="tool" id="tempBtn" title="${t('temp_title')}">${ic('thermo', 'sm')}<span id="tempLbl"></span></button>
            <span class="grow"></span>
            <button type="submit" class="send" id="send" title="${t('send')}">${ic('send')}</button>
          </div>
          <input type="file" id="fileInput" multiple hidden>
        </form>
        <div class="foot" id="foot"></div>
      </div>
      <div class="term" id="term">
        <div class="term-bar"><b>${t('terminal')}</b><span class="where" id="termWhere"></span>
          <button id="termSystem">${t('system_terminal')}</button><button id="termClose">${ic('x', 'sm')}</button></div>
        <div class="term-out" id="termOut"></div>
        <div class="term-in"><span>&gt;</span><input id="termIn" spellcheck="false" placeholder="${t('term_placeholder')}"></div>
      </div>`;
    wireComposer();
    wireTerminal();
    const scroller = $('#scroller');
    scroller.addEventListener('scroll', () => {
      const away = scroller.scrollHeight - scroller.scrollTop - scroller.clientHeight > 160;
      $('#jump').classList.toggle('on', away);
      $('#top').classList.toggle('scrolled', scroller.scrollTop > 4);
    });
    $('#jump').onclick = () => scrollDown(true);
    $('#thread').addEventListener('click', threadClick);
    paintThread();
  }

  function paintComposer() {
    if (!S.status || !$('#composer')) return;
    const st = settingsOf();
    const internet = (S.status.internet || {}).mode || 'off';
    $('#webBtn').classList.toggle('on', internet !== 'off' && !!st.web_on);
    $('#planBtn').classList.toggle('on', S.plan);
    $('#levelBtn').style.display = apex() ? '' : 'none';
    $('#levelLbl').textContent = t('level_n', { n: num(st.level || 5) });
    $('#tempLbl').textContent = num(st.temperature || 5) + '/' + num(10);
    const on = S.chips.filter(c => c.on).length;
    $('#skillsCount').textContent = on ? num(on) : '';
    $('#skillsBtn').classList.toggle('on', on > 0);
    const foot = $('#foot');
    const memory = st.memory || 0;
    foot.innerHTML = `<span>${ic('brain', 'sm')}${memory ? t('memory_n', { n: num(memory) }) : t('memory_off')}</span>
      <span>${ic('shield', 'sm')}${st.permission === 'auto' ? t('perm_auto_short') : st.permission === 'session' ? t('perm_session_short') : t('perm_ask_short')}</span>
      <span class="ltr" style="direction:inherit">${t('enter_hint')}</span>`;
    setSending(!!S.run);
  }

  function setSending(on) {
    const send = $('#send');
    if (!send) return;
    send.classList.toggle('stop', on);
    send.innerHTML = on ? ic('stop') : ic('send');
    send.title = on ? t('stop') + ' (Esc)' : t('send');
    send.disabled = false;
  }

  function wireComposer() {
    const input = $('#input');
    const form = $('#composer');
    const grow = () => { input.style.height = 'auto'; input.style.height = Math.min(input.scrollHeight, 240) + 'px'; };
    input.addEventListener('input', grow);
    input.addEventListener('focus', () => form.classList.add('focus'));
    input.addEventListener('blur', () => form.classList.remove('focus'));
    input.addEventListener('keydown', e => {
      if (e.key === 'Enter' && !e.shiftKey && !e.isComposing) { e.preventDefault(); form.requestSubmit(); }
    });
    form.addEventListener('submit', e => {
      e.preventDefault();
      if (S.run) { stopRun(); return; }
      const text = input.value.trim();
      if (!text) { input.focus(); return; }
      input.value = '';
      grow();
      send(text);
    });
    $('#attachBtn').onclick = () => $('#fileInput').click();
    $('#fileInput').onchange = e => { addFiles([...e.target.files]); e.target.value = ''; };
    form.addEventListener('dragover', e => { e.preventDefault(); form.classList.add('drag'); });
    form.addEventListener('dragleave', () => form.classList.remove('drag'));
    form.addEventListener('drop', e => { e.preventDefault(); form.classList.remove('drag'); addFiles([...e.dataTransfer.files]); });
    input.addEventListener('paste', e => {
      const files = [...(e.clipboardData || {}).files || []];
      if (files.length) { e.preventDefault(); addFiles(files); }
    });
    $('#webBtn').onclick = () => $('#webChip').click();
    $('#planBtn').onclick = () => { S.plan = !S.plan; paintComposer(); toast(S.plan ? t('plan_on') : t('plan_off'), 'info', 1600); };
    $('#skillsBtn').onclick = e => skillsMenu(e.currentTarget);
    $('#levelBtn').onclick = e => levelDial(e.currentTarget);
    $('#tempBtn').onclick = e => tempDial(e.currentTarget);
    paintComposer();
  }

  function addFiles(files) {
    for (const file of files.slice(0, 6)) {
      if (file.size > 400000) { toast(t('file_too_big', { name: file.name }), 'bad'); continue; }
      if (/^(image|video|audio)\//.test(file.type)) { toast(t('file_not_text', { name: file.name }), 'bad'); continue; }
      const reader = new FileReader();
      reader.onload = () => {
        const text = String(reader.result || '');
        if (text.includes('\u0000')) { toast(t('file_not_text', { name: file.name }), 'bad'); return; }
        S.attachments.push({ name: file.name, text: text.slice(0, 60000) });
        paintAttachments();
      };
      reader.readAsText(file);
    }
  }
  function paintAttachments() {
    const list = $('#attachList');
    list.innerHTML = '';
    S.attachments.forEach((a, i) => {
      const chip = h(`<span class="file-chip">${ic('file', 'sm')}<span dir="auto">${esc(a.name)}</span><button type="button" title="${t('remove')}">${ic('x', 'sm')}</button></span>`);
      $('button', chip).onclick = () => { S.attachments.splice(i, 1); paintAttachments(); };
      list.appendChild(chip);
    });
  }

  async function loadChips() {
    try { S.chips = await api('/local/chips'); } catch (e) { S.chips = []; }
    paintComposer();
  }
  function skillsMenu(anchor) {
    const menu = h(`<div><div class="pop-h">${t('skills')}</div></div>`);
    if (!S.chips.length) menu.appendChild(h(`<div class="side-empty">${t('no_skills')}</div>`));
    for (const chip of S.chips) {
      const row = h(`<div class="item"><span class="grow"><b dir="auto">${esc(chip.name)}</b><small dir="auto">${esc(chip.summary || '')}</small></span></div>`);
      row.appendChild(switcher(chip.on, async on => {
        chip.on = on;
        await setting({ chips_on: S.chips.filter(c => c.on).map(c => c.id) }, true);
        paintComposer();
      }));
      menu.appendChild(row);
    }
    menu.appendChild(h('<hr>'));
    const manage = h(`<button class="item">${ic('sliders', 'sm')}<span class="grow">${t('manage_skills')}</span></button>`);
    manage.onclick = () => { closePop(); S.settingsTab = 'skills'; go('settings'); };
    menu.appendChild(manage);
    popover(anchor, menu, { up: true, width: 320 });
  }

  const words = level => Math.max(12, Math.round(15 * Math.pow(level, 1.5)));
  function levelDial(anchor) {
    const level = settingsOf().level || 5;
    const node = h(`<div class="dial"><div class="row2"><b>${t('level_title')}</b><span class="big" id="lv">${num(level)}</span></div>
      <input type="range" min="1" max="20" step="1" value="${level}" aria-label="${t('level_title')}">
      <div class="ticks ltr"><span>1</span><span>5</span><span>10</span><span>15</span><span>20</span></div>
      <p class="say" id="lvSay"></p></div>`);
    const input = rangeFill($('input', node));
    const say = v => { $('#lv', node).textContent = num(v); $('#lvSay', node).textContent = t('level_say', { w: num(words(v)) }); };
    say(level);
    input.oninput = () => say(Number(input.value));
    input.onchange = () => setting({ level: Number(input.value) }, true).then(paintComposer);
    popover(anchor, node, { up: true });
  }
  const TEMP_SAY = ['t1', 't1', 't3', 't3', 't5', 't5', 't7', 't7', 't9', 't9'];
  function tempDial(anchor) {
    const temp = settingsOf().temperature || 5;
    const node = h(`<div class="dial"><div class="row2"><b>${t('temp_title')}</b><span class="big" id="tv">${num(temp)}</span></div>
      <input type="range" min="1" max="10" step="1" value="${temp}" aria-label="${t('temp_title')}">
      <div class="ticks ltr"><span>1</span><span>5</span><span>10</span></div><p class="say" id="tvSay"></p></div>`);
    const input = rangeFill($('input', node));
    const say = v => { $('#tv', node).textContent = num(v); $('#tvSay', node).textContent = t(TEMP_SAY[v - 1]) + (v === 5 ? ' · ' + t('t_default') : ''); };
    say(temp);
    input.oninput = () => say(Number(input.value));
    input.onchange = () => setting({ temperature: Number(input.value) }, true).then(paintComposer);
    popover(anchor, node, { up: true });
  }

  /* ---- the empty state ------------------------------------------------------------ */

  function renderEmpty() {
    const empty = $('#empty');
    if (!empty || !S.status) return;
    const st = S.status;
    const hasChat = S.chat && S.chat.messages.length;
    empty.style.display = hasChat || S.run ? 'none' : '';
    if (hasChat || S.run) return;
    const folderName = currentFolder() ? currentFolder().split(/[\\/]/).filter(Boolean).pop() : '';
    const internet = (st.internet || {}).mode || 'off';
    const ideas = st.folder
      ? [['book', 'idea_explain', 'idea_explain_p'], ['bug', 'idea_bugs', 'idea_bugs_p'],
         ['file-plus', 'idea_readme', 'idea_readme_p'], ['play', 'idea_run', 'idea_run_p']]
      : [['code', 'idea_script', 'idea_script_p'], ['book', 'idea_teach', 'idea_teach_p'],
         ['layers', 'idea_page', 'idea_page_p'], ['globe', 'idea_news', 'idea_news_p']];
    const hour = new Date().getHours();
    const greet = hour < 5 ? t('greet_night') : hour < 12 ? t('greet_morning') : hour < 18 ? t('greet_day') : t('greet_evening');
    empty.innerHTML = `
      <svg class="hero-mark mark" aria-hidden="true"><use href="#i-mark"/></svg>
      <h2>${esc(greet)} <span class="g">${t('hero_q')}</span></h2>
      <p class="sub" dir="auto">${t('hero_sub', { name: esc(st.profile.name) })}</p>
      <div class="caps">
        <span class="cap${folderName ? '' : ' off'}">${ic('folder')}${folderName ? t('cap_folder', { name: esc(folderName) }) : `${t('cap_no_folder')} <button id="capFolder">${t('connect')}</button>`}</span>
        <span class="cap${internet !== 'off' && settingsOf().web_on ? '' : ' off'}">${ic('globe')}${internet === 'off' ? `${t('cap_no_web')} <button id="capWeb">${t('turn_on')}</button>` : settingsOf().web_on ? t('cap_web') : t('cap_web_ready')}</span>
        <span class="cap">${ic('shield')}${t('cap_gate')}</span>
      </div>
      <div class="ideas stagger">${ideas.map(([icon, title, prompt]) => `
        <button class="idea" data-prompt="${esc(t(prompt))}"><span class="ico">${ic(icon)}</span>
        <span><b>${t(title)}</b><small dir="auto">${esc(t(prompt))}</small></span></button>`).join('')}</div>`;
    const capFolder = $('#capFolder');
    if (capFolder) capFolder.onclick = pickFolder;
    const capWeb = $('#capWeb');
    if (capWeb) capWeb.onclick = () => { S.settingsTab = 'internet'; go('settings'); };
    $$('.idea', empty).forEach(b => {
      b.onclick = () => {
        const input = $('#input');
        input.value = b.dataset.prompt;
        input.dispatchEvent(new Event('input'));
        input.focus();
      };
    });
  }

  /* ---- messages ------------------------------------------------------------------- */

  function paintThread() {
    const thread = $('#thread');
    if (!thread) return;
    thread.innerHTML = '';
    const messages = (S.chat && S.chat.messages) || [];
    messages.forEach((m, i) => thread.appendChild(m.role === 'user' ? userEl(m) : botEl(m, i === messages.length - 1)));
    if (S.run && S.run.chatId === S.chatId && S.chatId) {
      if (S.run.userEl && !S.run.userEl.isConnected && !messages.some(m => m.id === (S.run.user || {}).id)) thread.appendChild(S.run.userEl);
      thread.appendChild(S.run.live.el);
    }
    renderEmpty();
    requestAnimationFrame(() => scrollDown(false));
  }

  function userEl(m) {
    const files = (m.attachments || []).map(a => `<span class="file-chip">${ic('file', 'sm')}<span dir="auto">${esc(a.name)}</span></span>`).join('');
    return h(`<article class="msg user"><div class="col"><div class="bubble" dir="auto">${esc(m.content)}</div>
      ${files ? `<div class="files">${files}</div>` : ''}${m.plan ? `<span class="plan-tag">${ic('plan', 'sm')} ${t('plan_mode_tag')}</span>` : ''}</div></article>`);
  }

  function botShell(name) {
    return h(`<article class="msg bot"><div class="bubble">
      <div class="slot-think"></div><div class="slot-plan"></div><div class="steps"></div><div class="slot-check"></div><div class="content" dir="auto"></div><div class="slot-foot"></div>
      <div class="meta"><span class="tag" dir="auto">${esc(name)}</span><span class="stats"></span><span class="sp"></span><span class="acts"></span></div></div></article>`);
  }

  function botEl(m, last) {
    const el = botShell(m.model || '');
    if (m.think) $('.slot-think', el).appendChild(thinkEl(m.think, false, m));
    if (m.plan) $('.slot-plan', el).appendChild(planEl(m.plan, m.folder));
    for (const step of m.steps || []) $('.steps', el).appendChild(stepFromRecord(step));
    const content = $('.content', el);
    content.innerHTML = window.MD.render(m.content || '');
    decorateCode(content);
    finishFoot(el, m, last);
    return el;
  }

  function thinkEl(text, open, meta) {
    const words = text.split(/\s+/).filter(Boolean).length;
    const node = h(`<details class="think"${open ? ' open' : ''}><summary>${ic('brain', 'sm')}<span class="lbl">${t('reasoning_words', { n: num(words) })}</span>${ic('chev', 'sm chev')}</summary><div class="think-text" dir="auto"></div></details>`);
    $('.think-text', node).textContent = text;
    if (meta && meta.forced) $('.lbl', node).innerHTML += ` · <span class="over">${t('closed_at_ceiling')}</span>`;
    return node;
  }

  function finishFoot(el, m, last) {
    const foot = $('.slot-foot', el);
    foot.innerHTML = '';
    if (m.error) foot.appendChild(h(`<div class="note bad">${ic('alert', 'sm')} ${esc(m.error)}</div>`));
    if (m.stopped) foot.appendChild(h(`<div class="note warn">${t('stopped_note')}</div>`));
    if (m.ran_out && !m.undone) foot.appendChild(h(`<div class="note warn">${ic('info', 'sm')} ${t('ran_out_note')}</div>`));
    if (m.problems && m.problems.length && !m.undone) {
      foot.appendChild(h(`<div class="note warn problems"><b>${t('check_left')}</b><ul dir="ltr">${m.problems.map(p => `<li>${esc(p)}</li>`).join('')}</ul></div>`));
    }
    if (m.undone) foot.appendChild(h(`<div class="note">${ic('undo', 'sm')} ${t('undone_note')}</div>`));
    const acts = h('<div class="turn-acts"></div>');
    const steps = (m.plan && m.plan.steps) || [];
    const unfinished = steps.some(s => s.status === 'pending' || s.status === 'active');
    if (last && m.plan && !m.undone && (unfinished || m.plan_only)) {
      const go = h(`<button class="btn primary sm">${ic('play', 'sm')}${m.plan_only ? t('carry_out_plan') : t('continue_btn')}</button>`);
      go.onclick = () => send(t('continue_msg'), { resume: true, plan: false });
      acts.appendChild(go);
    }
    if (m.try && m.try.page && !m.undone) {
      const open = h(`<button class="btn sm">${ic('external', 'sm')}${t('open_page')}</button>`);
      open.onclick = () => api('/local/open-file', { folder: m.folder, path: m.try.page }).catch(fail);
      acts.appendChild(open);
    }
    if (m.folder) {
      const where = h(`<button class="btn sm ghost">${ic('folder', 'sm')}${t('open_project')}</button>`);
      where.title = m.folder;
      where.onclick = () => api('/local/open-project', { folder: m.folder }).catch(fail);
      acts.appendChild(where);
    }
    if (m.checkpoint && !m.undone && m.id) {
      const undo = h(`<button class="btn sm ghost danger">${ic('undo', 'sm')}${t('undo_btn')}</button>`);
      undo.onclick = async () => {
        const yes = await modal({ title: t('undo_btn'), text: t('undo_q', { n: num((m.changes || []).length) }), ok: t('undo_ok'), danger: true });
        if (!yes) return;
        try {
          const done = await api('/local/undo', { chat_id: S.chatId, message_id: m.id });
          m.undone = true;
          toast(t('undo_done', { n: num(done.restored.length) }), 'ok');
          finishFoot(el, m, last);
        } catch (e) { fail(e); }
      };
      acts.appendChild(undo);
    }
    if (acts.childElementCount) foot.appendChild(acts);
    const stats = m.stats || {};
    $('.meta .stats', el).textContent = stats.seconds ? t('stats', { s: num(stats.seconds, 1), n: num(stats.tokens), tps: num(stats.tps, 1) }) : '';
    const metaActs = $('.meta .acts', el);
    metaActs.innerHTML = `<button data-a="copy">${ic('copy', 'sm')}${t('copy')}</button>`
      + (last ? `<button data-a="retry">${ic('refresh', 'sm')}${t('answer_again')}</button>` : '');
    $('[data-a="copy"]', metaActs).onclick = e => copy(m.content || '', e.currentTarget);
    const retry = $('[data-a="retry"]', metaActs);
    if (retry) retry.onclick = () => answerAgain(el);
  }

  function decorateCode(root) {
    $$('.code', root).forEach(block => {
      const [copyBtn, saveBtn] = $$('.code-acts button', block);
      if (copyBtn && !copyBtn.innerHTML) copyBtn.innerHTML = ic('copy', 'sm') + t('copy');
      if (saveBtn && !saveBtn.innerHTML) saveBtn.innerHTML = ic('save', 'sm') + (S.status && S.status.folder ? t('save_to_folder') : t('save_as'));
    });
  }

  async function threadClick(e) {
    const button = e.target.closest('.code-acts button');
    if (!button) return;
    const block = button.closest('.code');
    const code = $('code', block).innerText;
    if (button.dataset.act === 'copy') { copy(code, button); return; }
    const guess = block.dataset.file || ('snippet.' + ({ python: 'py', javascript: 'js', typescript: 'ts', bash: 'sh', html: 'html', css: 'css', json: 'json' }[block.dataset.lang] || 'txt'));
    if (S.status.folder) {
      const path = await ask(t('save_in_folder_q'), guess);
      if (!path) return;
      try {
        const saved = await api('/local/folder/save', { path, content: code });
        toast(t('saved_to', { path: saved.path }), 'ok');
      } catch (err) { fail(err); }
      return;
    }
    try {
      const saved = await api('/local/save-as', { name: guess, content: code });
      if (saved.path) toast(t('saved_to', { path: saved.path }), 'ok');
    } catch (err) {
      const link = h(`<a download="${esc(guess)}"></a>`);
      link.href = URL.createObjectURL(new Blob([code], { type: 'text/plain' }));
      link.click();
    }
  }

  function scrollDown(force) {
    const scroller = $('#scroller');
    if (!scroller) return;
    const near = scroller.scrollHeight - scroller.scrollTop - scroller.clientHeight < 220;
    if (force || near) scroller.scrollTop = scroller.scrollHeight;
  }

  /* ---- plans (3.0) ------------------------------------------------------------------- */

  function currentFolder() { return (S.chat && S.chat.folder) || (S.status && S.status.folder) || ''; }

  function planEl(plan, folder) {
    const node = h(`<div class="plan-card"><div class="plan-head">${ic('plan', 'sm')}<b class="goal" dir="auto"></b>
      <span class="count"></span></div><ol class="plan-steps"></ol><div class="plan-where ltr"></div><div class="plan-acts"></div></div>`);
    paintPlan(node, plan, folder);
    return node;
  }
  function paintPlan(node, plan, folder) {
    if (!plan) return;
    $('.goal', node).textContent = plan.request || plan.goal || t('plan_title');
    $('.goal', node).title = plan.goal || '';
    const list = $('.plan-steps', node);
    list.innerHTML = '';
    (plan.steps || []).forEach((step, i) => list.appendChild(planStepEl(step, i)));
    $('.plan-where', node).textContent = folder || '';
    paintPlanCount(node, plan);
  }
  function planStepEl(step, i) {
    // A file step reads as a verb and the file, in the page's language; the
    // model's sentence about it is the tooltip. Other steps read as that sentence.
    const verb = { create: t('do_create'), edit: t('do_edit'), run: t('do_run'), fix: '' }[step.do] || '';
    const label = step.do === 'fix' || !step.path ? (step.do === 'fix' ? step.title : step.detail || step.title) : verb;
    const li = h(`<li data-status="${esc(step.status || 'pending')}" data-i="${i}"><span class="mark"></span>
      <span class="ttl" dir="auto"></span>${step.path ? `<code class="ltr">${esc(step.path)}</code>` : ''}</li>`);
    $('.ttl', li).textContent = label || '';
    li.dataset.detail = step.detail || '';
    paintMark(li);
    if (step.detail) li.title = step.detail;
    return li;
  }
  function paintMark(li) {
    const status = li.dataset.status || 'pending';
    $('.mark', li).innerHTML = status === 'active' ? '<span class="spinner"></span>'
      : status === 'pending' ? '' : ic({ done: 'check', failed: 'alert', skipped: 'minus' }[status] || 'check', 'sm');
    if (!li.dataset.detail) li.title = t('st_' + status);
  }
  function paintPlanCount(node, plan) {
    const steps = plan.steps || [];
    const done = steps.filter(s => s.status === 'done').length;
    $('.count', node).textContent = steps.length ? t('plan_count', { done: num(done), total: num(steps.length) }) : '';
  }

  /* ---- tool cards ------------------------------------------------------------------- */

  const TOOL_ICON = { list_files: 'list', read_file: 'file', search_files: 'search', write_file: 'file-plus',
                      edit_file: 'file-edit', delete_file: 'trash', run_command: 'terminal', web_search: 'globe', fetch_url: 'link',
                      open_file: 'external', stop_command: 'stop' };
  function host(url) { try { return new URL(url).hostname; } catch (e) { return url || ''; } }
  function toolTitle(name, args, state, meta) {
    args = args || {};
    meta = meta || {};
    const path = `<code>${esc(args.path || '.')}</code>`;
    const map = {
      list_files: ['t_list_run', 't_list_done'], read_file: ['t_read_run', 't_read_done'],
      search_files: ['t_search_run', 't_search_done'], write_file: ['t_write_run', meta.created === false ? 't_update_done' : 't_write_done'],
      edit_file: ['t_edit_run', 't_edit_done'], delete_file: ['t_delete_run', 't_delete_done'],
      run_command: ['t_cmd_run', 't_cmd_done'], web_search: ['t_web_run', 't_web_done'], fetch_url: ['t_fetch_run', 't_fetch_done'],
      open_file: ['t_open_run', 't_open_done'], stop_command: ['t_stop_run', 't_stop_done'],
    }[name] || ['t_tool', 't_tool'];
    const key = state === 'done' ? map[1] : map[0];
    return t(key, {
      path, text: esc(args.text || ''), command: `<code>${esc((args.command || '').slice(0, 80))}</code>`,
      query: esc(args.query || ''), host: esc(host(args.url || meta.url || '')), name: esc(name),
    });
  }
  function toolSub(step) {
    const meta = step.meta || {};
    if (step.status === 'refused') return t('refused');
    if (step.name === 'run_command' && meta.background) return t('bg_running');
    if (step.name === 'run_command' && meta.code != null) return (meta.code === 0 ? t('exit_ok') : t('exit_code', { n: meta.code })) + ' · ' + num(meta.seconds, 1) + 's';
    if (step.name === 'write_file' && meta.lines) return t('n_lines', { n: num(meta.lines) });
    if (step.name === 'web_search' && meta.results) return t('n_results', { n: num(meta.results.length) });
    if (step.status === 'error') return t('failed');
    return '';
  }

  function stepEl(id, name) {
    const node = h(`<div class="step" data-id="${esc(id)}" data-status="running">
      <button class="step-head" type="button"><span class="step-ico">${ic(TOOL_ICON[name] || 'bolt', 'sm')}</span>
        <span class="step-title">${toolTitle(name, {}, 'running')}</span><span class="step-sub"></span>${ic('chev', 'sm chev')}</button>
      <div class="step-body"><div class="step-inner"></div></div></div>`);
    $('.step-head', node).onclick = () => node.classList.toggle('open');
    node.dataset.name = name;
    return node;
  }

  function stepBody(step) {
    const inner = h('<div></div>');
    const meta = step.meta || {};
    if (step.name === 'web_search' && meta.results) {
      const list = h('<div class="results"></div>');
      for (const r of meta.results) {
        const row = h(`<button class="result" type="button"><span class="fav">${esc(host(r.url).replace(/^www\./, '')[0] || '?')}</span>
          <span><b dir="auto">${esc(r.title)}</b><small dir="auto">${esc(host(r.url))} — ${esc(r.snippet)}</small></span></button>`);
        row.onclick = () => api('/local/open-url', { url: r.url }).catch(fail);
        list.appendChild(row);
      }
      inner.appendChild(list);
      return inner;
    }
    if (step.name === 'run_command' && meta.background) {
      const bar = h(`<div class="bg-bar"><span class="spinner"></span><span>${t('bg_running')}</span>
        ${meta.url ? `<a href="${esc(meta.url)}" class="ltr">${esc(meta.url)}</a>` : ''}<span class="grow"></span>
        <button class="btn sm">${ic('stop', 'sm')}${t('bg_stop')}</button></div>`);
      $('button', bar).onclick = async () => {
        try { await api('/local/procs/' + encodeURIComponent(meta.background) + '/stop', {}); } catch (e) { fail(e); return; }
        bar.innerHTML = `${ic('check', 'sm')}<span>${t('bg_stopped')}</span>`;
      };
      inner.appendChild(bar);
    }
    const output = step.output || '';
    if (!output) return inner;
    const pre = h('<pre dir="ltr"></pre>');
    const isDiff = /^--- a\//m.test(output) && /^\+\+\+ b\//m.test(output);
    if (isDiff) pre.innerHTML = window.MD.diffLines(output);
    else if (step.name === 'write_file' || step.name === 'read_file') {
      const ext = String((step.args || {}).path || '').split('.').pop();
      pre.innerHTML = window.MD.highlight(output, ext);
    } else pre.textContent = output;
    inner.appendChild(pre);
    return inner;
  }

  function stepFromRecord(step) {
    const node = stepEl(step.id, step.name);
    node.dataset.status = step.status === 'done' ? 'done' : step.status;
    $('.step-title', node).innerHTML = toolTitle(step.name, step.args, step.status === 'done' ? 'done' : 'run', step.meta);
    $('.step-sub', node).textContent = toolSub(step);
    $('.step-inner', node).appendChild(stepBody(step));
    return node;
  }

  /* ---- a live turn --------------------------------------------------------------- */

  function liveBot(name) {
    const el = botShell(name);
    const content = $('.content', el);
    content.classList.add('live');
    const reading = h(`<div class="reading"><span class="dots">${t('reading_prompt').replace(/…$/, '')}</span><span class="bar"><i></i></span><span class="pct"></span></div>`);
    $('.slot-think', el).before(reading);
    let text = '', think = '', thinkNode = null, frame = 0, planNode = null, planData = null;
    const steps = {};
    const paint = () => {
      frame = 0;
      content.innerHTML = window.MD.render(text);
      decorateCode(content);
      scrollDown(false);
    };
    const schedule = () => { if (!frame) frame = requestAnimationFrame(paint); };
    const hideReading = () => { if (reading.isConnected) reading.remove(); };
    const step = id => steps[id];
    return {
      el,
      name(n) { $('.meta .tag', el).textContent = n; },
      progress(done, total) {
        if (!reading.isConnected) return;
        const pct = total ? Math.round(done / total * 100) : 0;
        $('i', reading).style.width = pct + '%';
        $('.pct', reading).textContent = total ? `${num(done)} / ${num(total)}` : '';
        if (done >= total && total) $('.dots', reading).textContent = t('thinking').replace(/…$/, '');
      },
      think(piece) {
        hideReading();
        think += piece;
        if (!thinkNode) { thinkNode = thinkEl('', true); $('.slot-think', el).appendChild(thinkNode); }
        const box = $('.think-text', thinkNode);
        box.textContent = think;
        box.scrollTop = box.scrollHeight;
        $('.lbl', thinkNode).textContent = t('reasoning_live', { n: num(think.split(/\s+/).filter(Boolean).length) });
        scrollDown(false);
      },
      thinkEnd(d) {
        if (!thinkNode) return;
        thinkNode.open = false;
        $('.lbl', thinkNode).innerHTML = esc(t('reasoning_words', { n: num(d.words || 0) }))
          + (d.target ? ' · ' + esc(t('target_words', { n: num(d.target) })) : '')
          + (d.forced ? ` · <span class="over">${t('closed_at_ceiling')}</span>` : '');
      },
      planning(d) {
        hideReading();
        if (planData) return;
        if (!planNode) {
          planNode = planEl({ goal: '', steps: [] });
          planNode.classList.add('drafting');
          $('.slot-plan', el).appendChild(planNode);
        }
        $('.goal', planNode).textContent = t('planning');
        $('.plan-steps', planNode).innerHTML = (d.paths || []).filter(Boolean).map(x =>
          `<li data-status="pending"><span class="mark"></span><span class="ttl"></span><code class="ltr">${esc(x)}</code></li>`).join('');
        scrollDown(false);
      },
      plan(d) {
        hideReading();
        planData = d.plan;
        if (!planNode) { planNode = planEl(planData, d.folder); $('.slot-plan', el).appendChild(planNode); }
        else { planNode.classList.remove('drafting'); paintPlan(planNode, planData, d.folder || $('.plan-where', planNode).textContent); }
        scrollDown(false);
      },
      planStep(d) {
        if (!planNode || !planData) return;
        const step = planData.steps[d.index];
        if (step) { step.status = d.status; step.note = d.note; }
        const li = $(`li[data-i="${d.index}"]`, planNode);
        if (li) { li.dataset.status = d.status; paintMark(li); }
        paintPlanCount(planNode, planData);
      },
      check(d) {
        const slot = $('.slot-check', el);
        const problems = d.problems || [];
        slot.innerHTML = problems.length
          ? `<div class="note warn">${ic('alert', 'sm')} ${t('check_found', { n: num(problems.length) })}<ul dir="ltr">${problems.slice(0, 8).map(p => `<li>${esc(p)}</li>`).join('')}</ul></div>`
          : `<div class="note ok">${ic('check', 'sm')} ${t('check_ok')}</div>`;
        scrollDown(false);
      },
      folder(d) {
        if (S.chat && (!S.run || S.run.chatId === S.chat.id)) S.chat.folder = d.path;
        if (planNode) $('.plan-where', planNode).textContent = d.path;
        toast(t('project_folder', { path: d.path }), 'info', 4000);
        renderTop(true);
      },
      toolStart(d) {
        hideReading();
        if (steps[d.id]) return;
        const node = stepEl(d.id, d.name);
        steps[d.id] = { node, name: d.name, args: {}, out: [] };
        $('.steps', el).appendChild(node);
        scrollDown(false);
      },
      toolArgs(d) {
        const s = step(d.id);
        if (!s) return;
        Object.assign(s.args, d);
        $('.step-title', s.node).innerHTML = toolTitle(s.name, s.args, 'running');
        if (d.tail != null) {
          $('.step-sub', s.node).textContent = t('n_chars', { n: num(d.chars || 0) });
          let pre = $('pre.live', s.node);
          if (!pre) {
            $('.step-inner', s.node).innerHTML = '';
            pre = h('<pre class="live" dir="ltr"></pre>');
            $('.step-inner', s.node).appendChild(pre);
            s.node.classList.add('open');
          }
          const ext = String(s.args.path || '').split('.').pop();
          pre.innerHTML = window.MD.highlight(d.tail, ext);
          pre.scrollTop = pre.scrollHeight;
        }
      },
      toolCall(d) {
        if (!steps[d.id]) this.toolStart(d);
        const s = step(d.id);
        Object.assign(s.args, d.args || {});
        $('.step-title', s.node).innerHTML = toolTitle(s.name, s.args, 'running');
        const pre = $('pre.live', s.node);
        if (pre) pre.classList.remove('live');
      },
      toolOutput(d) {
        const s = step(d.id);
        if (!s) return;
        let pre = $('pre.out', s.node);
        if (!pre) {
          $('.step-inner', s.node).innerHTML = '';
          pre = h('<pre class="out live" dir="ltr"></pre>');
          $('.step-inner', s.node).appendChild(pre);
          s.node.classList.add('open');
        }
        pre.textContent += (pre.textContent ? '\n' : '') + d.line;
        pre.scrollTop = pre.scrollHeight;
      },
      ask(d) {
        if (d.name === 'plan') {
          if (!planNode || !planData) this.plan({ plan: d.plan, folder: d.folder });
          const box = h(`<div class="ask plan-ask" data-ask="${esc(d.ask_id)}"><div class="q">${ic('shield', 'sm')}${t('plan_ask')}</div>
            <div class="acts"><button class="btn primary sm" data-a="allow">${ic('play', 'sm')}${t('plan_run')}</button>
            ${d.session ? `<button class="btn sm" data-a="session">${t('allow_session')}</button>` : ''}
            <button class="btn ghost danger sm" data-a="deny">${t('plan_no')}</button></div><div class="said"></div></div>`);
          $$('.acts button', box).forEach(b => {
            b.onclick = async () => {
              $$('.acts button', box).forEach(x => { x.disabled = true; });
              try { await api('/local/answer', { ask_id: d.ask_id, answer: b.dataset.a }); } catch (e) { fail(e); }
            };
          });
          $('.plan-acts', planNode).appendChild(box);
          scrollDown(true);
          if (!document.hasFocus()) {
            const title = document.title;
            document.title = '● ' + t('needs_you');
            window.addEventListener('focus', () => { document.title = title; }, { once: true });
          }
          return;
        }
        if (!steps[d.id]) this.toolStart({ id: d.id, name: d.name });
        const s = step(d.id);
        s.node.dataset.status = 'asking';
        s.node.classList.add('open');
        const title = d.name === 'run_command' ? t('ask_run') : d.name === 'delete_file' ? t('ask_delete', { path: esc(d.path || s.args.path || '') })
          : d.name === 'edit_file' ? t('ask_edit', { path: esc(d.path || '') }) : d.diff != null ? t('ask_replace', { path: esc(d.path || '') }) : t('ask_create', { path: esc(d.path || '') });
        const shown = d.command != null ? `<pre dir="ltr">${esc(d.command)}</pre>`
          : d.diff != null ? `<pre dir="ltr">${d.diff ? window.MD.diffLines(d.diff) : esc(t('no_changes'))}</pre>`
          : d.content != null ? `<pre dir="ltr">${window.MD.highlight(d.content, String(d.path || '').split('.').pop())}</pre>` : '';
        const box = h(`<div class="ask" data-ask="${esc(d.ask_id)}"><div class="q">${ic('shield', 'sm')}${title}</div>${shown}
          <div class="acts"><button class="btn primary sm" data-a="allow">${ic('check', 'sm')}${t('allow')}</button>
          ${d.session ? `<button class="btn sm" data-a="session">${t('allow_session')}</button>` : ''}
          <button class="btn ghost danger sm" data-a="deny">${t('refuse')}</button></div><div class="said"></div></div>`);
        $$('.acts button', box).forEach(b => {
          b.onclick = async () => {
            $$('.acts button', box).forEach(x => { x.disabled = true; });
            try { await api('/local/answer', { ask_id: d.ask_id, answer: b.dataset.a }); } catch (e) { fail(e); }
          };
        });
        $('.step-inner', s.node).innerHTML = '';
        s.node.querySelector('.step-body').before(box);
        scrollDown(true);
        if (!document.hasFocus()) {
          // The window's title is the one signal that reaches a person who
          // has switched to another program while a change waits for them.
          const title = document.title;
          document.title = '● ' + t('needs_you');
          window.addEventListener('focus', () => { document.title = title; }, { once: true });
        }
      },
      asked(d) {
        const box = $(`[data-ask="${d.ask_id}"]`, el);
        if (!box) return;
        box.classList.add('settled');
        $('.said', box).textContent = { allow: t('said_allow'), session: t('said_session'), deny: t('said_deny') }[d.answer] || '';
        setTimeout(() => { box.style.transition = 'opacity .3s'; box.style.opacity = '.0'; setTimeout(() => box.remove(), 320); }, 1300);
        const node = box.closest('.step');
        if (node) node.dataset.status = d.answer === 'deny' ? 'refused' : 'running';
      },
      toolResult(d) {
        if (!steps[d.id]) this.toolStart({ id: d.id, name: d.name });
        const s = step(d.id);
        s.node.dataset.status = d.status === 'done' ? 'done' : d.status;
        $('.step-title', s.node).innerHTML = toolTitle(d.name, d.args, d.status === 'done' ? 'done' : 'run', d.meta);
        $('.step-sub', s.node).textContent = toolSub(d);
        const inner = $('.step-inner', s.node);
        inner.innerHTML = '';
        inner.appendChild(stepBody(d));
        if (d.name !== 'run_command' && d.name !== 'web_search') s.node.classList.remove('open');
        if (d.status === 'done' && ['write_file', 'edit_file', 'delete_file'].includes(d.name)) refreshFiles();
      },
      delta(piece) { hideReading(); text += piece; schedule(); },
      reset(value) { text = value || ''; schedule(); },
      error(detail) {
        hideReading();
        $('.slot-foot', el).appendChild(h(`<div class="note bad">${ic('alert', 'sm')} ${esc(detail)}</div>`));
      },
      done(message) {
        hideReading();
        if (frame) { cancelAnimationFrame(frame); frame = 0; }
        content.classList.remove('live');
        text = message.content || '';
        content.innerHTML = window.MD.render(text);
        decorateCode(content);
        if (thinkNode) { thinkNode.open = false; }
        finishFoot(el, message, true);
      },
      finish() {
        hideReading();
        content.classList.remove('live');
        $$('.step[data-status="running"],.step[data-status="asking"]', el).forEach(n => { n.dataset.status = 'error'; });
      },
    };
  }

  function refreshFiles() { /* a hook for a future file tree; the folder chip already shows the folder */ }

  async function send(text, options = {}) {
    if (S.run) return;
    if (!ready()) {
      toast(S.status && S.status.state === 'loading' ? t('model_loading_wait') : t('no_model_toast'), 'bad');
      if (S.status && S.status.state !== 'loading') go('models');
      return;
    }
    if (S.view !== 'chat') go('chat');
    const plan = options.plan !== undefined ? options.plan : S.plan;
    const attachments = S.attachments.splice(0);
    paintAttachments();
    const user = { role: 'user', content: text, attachments: attachments.map(a => ({ name: a.name })), plan };
    const userNode = userEl(user);
    const live = liveBot(S.status.profile.name);
    $('#thread').appendChild(userNode);
    $('#thread').appendChild(live.el);
    if (plan && options.plan === undefined) { S.plan = false; }
    S.run = { ctrl: new AbortController(), live, runId: null, chatId: S.chatId, userEl: userNode, user: null };
    renderEmpty();
    scrollDown(true);
    paintComposer();
    await stream({ chat_id: S.chatId, prompt: text, attachments, plan, resume: !!options.resume, web: !!settingsOf().web_on }, live);
  }

  async function answerAgain(node) {
    if (S.run || !S.chatId) return;
    if (!ready()) { toast(t('model_loading_wait'), 'bad'); return; }
    const live = liveBot(S.status.profile.name);
    node.replaceWith(live.el);
    if (S.chat && S.chat.messages.length && S.chat.messages[S.chat.messages.length - 1].role === 'assistant') S.chat.messages.pop();
    S.run = { ctrl: new AbortController(), live, runId: null, chatId: S.chatId, userEl: null, user: null };
    paintComposer();
    await stream({ chat_id: S.chatId, retry: true, web: !!settingsOf().web_on }, live);
  }

  async function stream(body, live) {
    const run = S.run;
    let finished = false;
    try {
      await sse('/local/chat', body, (event, d) => {
        switch (event) {
          case 'run':
            run.runId = d.run_id;
            run.user = d.user;
            live.name(d.profile);
            if (!run.chatId) {
              run.chatId = d.chat_id;
              if (!S.chatId) { S.chatId = d.chat_id; S.chat = { id: d.chat_id, title: d.title, messages: [] }; }
            }
            if (S.chat && S.chat.id === run.chatId && d.user && !S.chat.messages.some(m => m.id === d.user.id)) S.chat.messages.push(d.user);
            loadChats();
            break;
          case 'progress': live.progress(d.done, d.total); break;
          case 'think': live.think(d.text); break;
          case 'think_end': live.thinkEnd(d); break;
          case 'planning': live.planning(d); break;
          case 'plan': live.plan(d); break;
          case 'plan_step': live.planStep(d); break;
          case 'check': live.check(d); break;
          case 'folder': live.folder(d); break;
          case 'tool_start': live.toolStart(d); break;
          case 'tool_args': live.toolArgs(d); break;
          case 'tool_call': live.toolCall(d); break;
          case 'tool_output': live.toolOutput(d); break;
          case 'ask': live.ask(d); break;
          case 'asked': live.asked(d); break;
          case 'tool_result': live.toolResult(d); break;
          case 'delta': live.delta(d.text); break;
          case 'reply_reset': live.reset(d.text); break;
          case 'error': live.error(d.detail); break;
          case 'done':
            finished = true;
            live.done(d.message);
            if (S.chat && S.chat.id === run.chatId) S.chat.messages.push(d.message);
            break;
          default: break;
        }
      }, run.ctrl.signal);
    } catch (e) {
      if (e.name !== 'AbortError') live.error(String(e.message || e));
    } finally {
      if (!finished) live.finish();
      S.run = null;
      paintComposer();
      loadChats();
      $$('#thread .meta [data-a="retry"]').forEach(b => { if (!b.closest('.msg').isSameNode(live.el)) b.remove(); });
    }
  }

  async function stopRun() {
    if (!S.run) return;
    if (S.run.runId) api('/local/stop', { run_id: S.run.runId }).catch(() => {});
    else S.run.ctrl.abort();
    toast(t('stopping'), 'info', 1500);
  }

  async function openChat(id) {
    if (S.view !== 'chat') go('chat');
    try {
      S.chat = await api('/local/chats/' + id);
      S.chatId = id;
    } catch (e) { fail(e); return; }
    paintChats();
    paintThread();
    $('#app').classList.remove('side-open');
  }

  function newChat() {
    if (S.view !== 'chat') go('chat');
    S.chatId = null;
    S.chat = null;
    S.plan = false;
    paintChats();
    paintThread();
    paintComposer();
    setTimeout(() => $('#input') && $('#input').focus(), 30);
  }

  /* ---- the terminal ---------------------------------------------------------------- */

  let socket = null;
  function wireTerminal() {
    $('#termClose').onclick = () => $('#term').classList.remove('on');
    $('#termSystem').onclick = () => api('/local/open-terminal', {}).catch(fail);
    $('#termIn').addEventListener('keydown', e => {
      if (e.key !== 'Enter') return;
      const command = e.target.value.trim();
      if (!command) return;
      e.target.value = '';
      const live = connectShell();
      const go = () => live.send(JSON.stringify({ command }));
      if (live.readyState === 1) go(); else live.addEventListener('open', go, { once: true });
    });
  }
  const termSay = (text, cls) => {
    const out = $('#termOut');
    if (!out) return;
    out.appendChild(h(`<div class="${cls || ''}">${esc(text)}</div>`));
    out.scrollTop = out.scrollHeight;
  };
  function connectShell() {
    if (socket && socket.readyState <= 1) return socket;
    socket = new WebSocket((location.protocol === 'https:' ? 'wss://' : 'ws://') + location.host + '/local/shell');
    socket.onmessage = async event => {
      const m = JSON.parse(event.data || '{}');
      if (m.folder) { $('#termWhere').textContent = m.folder; return; }
      if (m.ask) {
        const yes = await modal({ title: t('ask_run'), body: h(`<pre class="log" style="margin:0">${esc(m.ask)}</pre>`), ok: t('allow') });
        if (!yes) { termSay(t('refused'), 'bad'); return; }
        socket.send(JSON.stringify({ command: m.ask, allowed: true }));
        return;
      }
      if (m.started) { termSay('> ' + m.started, 'cmd'); return; }
      if (m.line !== undefined) { termSay(m.line); return; }
      if (m.cut) { termSay(t('too_much_output'), 'bad'); return; }
      if (m.error) { termSay(m.error, 'bad'); return; }
      if (m.code !== undefined) termSay(m.code === 0 ? t('exit_ok') : t('exit_code', { n: m.code }), m.code === 0 ? 'code' : 'bad');
    };
    socket.onclose = () => { socket = null; };
    return socket;
  }
  function toggleTerminal() {
    if (S.view !== 'chat') go('chat');
    const term = $('#term');
    const opening = !term.classList.contains('on');
    term.classList.toggle('on', opening);
    if (!opening) return;
    if (!S.status.folder) { termSay(t('no_folder_title'), 'bad'); return; }
    connectShell();
    setTimeout(() => $('#termIn').focus(), 200);
  }

  /* ---- battle ------------------------------------------------------------------------ */

  async function loadBattles() {
    try {
      const answer = await api('/local/battles');
      S.battles = answer.battles;
      S.score = answer.score;
    } catch (e) { S.battles = []; }
    if (S.view === 'battle') renderSideBody();
  }

  function renderBattleSide(body) {
    const score = S.score || { base: 0, adapter: 0, tie: 0, both_bad: 0, votes: 0, battles: 0 };
    const total = Math.max(1, score.base + score.adapter + score.tie);
    body.innerHTML = `
      <div class="score"><h4>${ic('trophy', 'sm')}${t('scoreboard')} · ${t('n_votes', { n: num(score.votes) })}</h4>
        <div class="vs-bar"><i class="a" style="flex-grow:${score.base / total}"></i><i class="t" style="flex-grow:${score.tie / total}"></i><i class="b" style="flex-grow:${score.adapter / total}"></i></div>
        <div class="legend"><div><b>${num(score.base)}</b>${t('base_model')}</div><div class="mid"><b>${num(score.tie)}</b>${t('tie')}</div><div class="end"><b>${num(score.adapter)}</b>${t('with_adapter')}</div></div></div>
      <div class="side-list scroll" id="battleList"></div>`;
    const list = $('#battleList');
    if (!S.battles.length) { list.innerHTML = `<div class="side-empty">${t('no_battles')}</div>`; return; }
    list.appendChild(h(`<div class="group-h" style="display:flex;justify-content:space-between;align-items:center">${t('past_battles')}<button class="btn ghost sm" id="clearBattles" style="height:24px">${t('clear')}</button></div>`));
    $('#clearBattles', list).onclick = async () => {
      if (!await modal({ title: t('clear_battles_q'), ok: t('clear'), danger: true })) return;
      const answer = await api('/local/battles', undefined, 'DELETE').catch(fail);
      if (answer) { S.score = answer.score; S.battles = []; S.battle = null; renderBattle(); renderSideBody(); }
    };
    S.battles.forEach((b, index) => {
      const mark = !b.vote ? ic('scale', 'sm') : b.vote === 'tie' || b.vote === 'both_bad' ? ic('scale', 'sm') : ic('trophy', 'sm');
      const winner = b.vote && b.sides[b.vote] ? (b.sides[b.vote] === 'base' ? t('base_short') : t('adapter_short')) : '';
      const row = h(`<div class="row${S.battle && S.battle.id === b.id ? ' on' : ''}" role="button" tabindex="0" style="animation-delay:${Math.min(index, 12) * 18}ms">
        ${mark}<span class="ttl" dir="auto">${esc(b.prompt)}</span>${winner ? `<span class="badge ${b.sides[b.vote] === 'base' ? '' : 'acc'}">${esc(winner)}</span>` : ''}</div>`);
      row.onclick = () => { S.battle = b; renderBattle(); renderSideBody(); };
      list.appendChild(row);
    });
  }

  function renderBattle() {
    const view = $('#view-battle');
    const st = S.status;
    if (!st) return;
    const prefs = settingsOf().battle || { mode: 'parallel', blind: false, strength: 1 };
    const profile = st.profile;
    if (!profile.has_adapter) {
      view.innerHTML = `<div class="empty" style="padding-top:12vh"><svg class="hero-mark mark" aria-hidden="true"><use href="#i-mark"/></svg>
        <h2>${t('battle_needs_adapter')}</h2><p class="sub">${t('battle_needs_adapter_p')}</p>
        <button class="btn primary" id="toModels">${ic('cube', 'sm')}${t('manage_models')}</button></div>`;
      $('#toModels').onclick = () => go('models');
      return;
    }
    const b = S.battle;
    const blind = b ? b.blind && !b.vote : prefs.blind;
    const sideName = side => {
      if (!b) return side === 'a' ? t('base_model') : t('with_adapter');
      if (blind) return t('contender', { x: side.toUpperCase() });
      return b.sides[side] === 'base' ? t('base_model') : t('with_adapter');
    };
    view.innerHTML = `
      <div class="arena-top">
        <div class="versus">
          <div class="fighter a"><span class="ava">A</span><span class="who"><b dir="auto">${esc(sideName('a'))}</b><small dir="auto">${blind ? t('hidden_until_vote') : esc((!b || b.sides.a === 'base') ? profile.base : profile.adapter)}</small></span></div>
          <div class="vs">VS</div>
          <div class="fighter b"><span class="ava">B</span><span class="who"><b dir="auto">${esc(sideName('b'))}</b><small dir="auto">${blind ? t('hidden_until_vote') : esc((!b || b.sides.b === 'adapter') ? profile.adapter : profile.base)}</small></span></div>
        </div>
        <div class="arena-controls" id="arenaCtl"></div>
      </div>
      <div class="arena" id="arena">
        ${['a', 'b'].map(side => `<div class="lane" data-side="${side}"><div class="lane-head"><span class="tag">${side.toUpperCase()}</span>
          <span class="name${blind ? ' hidden' : ''}" dir="auto">${esc(sideName(side))}</span></div>
          <div class="lane-body scroll content" dir="auto"><div class="lane-empty">${ic('swords', 'lg')}<span>${t('battle_empty')}</span></div></div>
          <div class="lane-foot"></div></div>`).join('')}
      </div>
      <div id="voteSlot"></div>
      <div class="composer-wrap"><form class="composer" id="battleForm" autocomplete="off">
        <textarea id="battleInput" rows="1" dir="auto" placeholder="${t('battle_placeholder')}"></textarea>
        <div class="bar"><span class="muted" style="font-size:12px;padding-inline-start:4px">${ic('info', 'sm')} ${t('battle_hint')}</span><span class="grow"></span>
        <button type="submit" class="send" id="battleSend">${ic('send')}</button></div></form></div>`;
    const ctl = $('#arenaCtl');
    const modeSeg = seg([['parallel', ic('layers', 'sm') + ' ' + t('together')], ['sequential', ic('list', 'sm') + ' ' + t('one_by_one')]],
      prefs.mode, v => setting({ battle: { mode: v } }, true));
    const blindLbl = h(`<label class="lbl">${ic('eye-off', 'sm')}${t('blind')}</label>`);
    blindLbl.appendChild(switcher(prefs.blind, v => setting({ battle: { blind: v } }, true)));
    const strength = h(`<span class="strength">${ic('layers', 'sm')}${t('strength')}<input type="range" min="0.1" max="2" step="0.1" value="${prefs.strength}"><b class="ltr">${num(prefs.strength * 100)}%</b></span>`);
    const range = rangeFill($('input', strength));
    range.oninput = () => { $('b', strength).textContent = num(range.value * 100) + '%'; };
    range.onchange = () => setting({ battle: { strength: Number(range.value) } }, true);
    ctl.append(modeSeg, blindLbl, strength);
    const input = $('#battleInput');
    const grow = () => { input.style.height = 'auto'; input.style.height = Math.min(input.scrollHeight, 180) + 'px'; };
    input.addEventListener('input', grow);
    input.addEventListener('keydown', e => { if (e.key === 'Enter' && !e.shiftKey && !e.isComposing) { e.preventDefault(); $('#battleForm').requestSubmit(); } });
    input.addEventListener('focus', () => $('#battleForm').classList.add('focus'));
    input.addEventListener('blur', () => $('#battleForm').classList.remove('focus'));
    $('#battleForm').addEventListener('submit', e => {
      e.preventDefault();
      if (S.battleRun) { if (S.battleRun.runId) api('/local/stop', { run_id: S.battleRun.runId }).catch(() => {}); return; }
      const text = input.value.trim();
      if (!text) return;
      input.value = ''; grow();
      fight(text);
    });
    if (b) showBattle(b);
    paintBattleSend();
  }

  function paintBattleSend() {
    const send = $('#battleSend');
    if (!send) return;
    send.classList.toggle('stop', !!S.battleRun);
    send.innerHTML = S.battleRun ? ic('stop') : ic('send');
  }

  function laneParts(side) {
    const lane = $(`.lane[data-side="${side}"]`);
    return { lane, body: $('.lane-body', lane), foot: $('.lane-foot', lane) };
  }

  function laneStats(stats) {
    if (!stats || !stats.seconds) return '';
    return `<span>${ic('bolt', 'sm')} ${num(stats.tps, 1)} ${t('tok_s')}</span><span>${num(stats.tokens)} ${t('tokens')}</span><span>${dur(stats.seconds)}</span>`;
  }

  function showBattle(b) {
    for (const side of ['a', 'b']) {
      const { body, foot } = laneParts(side);
      const r = b[side] || {};
      body.innerHTML = (r.think ? thinkEl(r.think, false).outerHTML : '') + window.MD.render(r.text || '') + (r.error ? `<div class="note bad">${esc(r.error)}</div>` : '');
      decorateCode(body);
      foot.innerHTML = laneStats(r.stats);
    }
    paintVote(b);
  }

  function paintVote(b) {
    const slot = $('#voteSlot');
    if (!slot) return;
    slot.innerHTML = '';
    if (!b || !b.a || b.a.text === undefined) return;
    const bar = h(`<div class="vote${b.vote ? ' done' : ''}">
      <button class="btn" data-v="a">${ic('trophy', 'sm')}${t('a_better')}</button>
      <button class="btn" data-v="tie">${ic('scale', 'sm')}${t('tie')}</button>
      <button class="btn" data-v="both_bad">${ic('x', 'sm')}${t('both_bad')}</button>
      <button class="btn" data-v="b">${ic('trophy', 'sm')}${t('b_better')}</button></div>`);
    $$('button', bar).forEach(button => {
      if (b.vote === button.dataset.v) button.classList.add('picked');
      button.onclick = async () => {
        try {
          const answer = await api(`/local/battles/${b.id}/vote`, { vote: button.dataset.v });
          S.score = answer.score;
          const wasBlind = b.blind && !b.vote;
          Object.assign(b, answer.battle);
          const index = S.battles.findIndex(x => x.id === b.id);
          if (index >= 0) S.battles[index] = b;
          if (wasBlind) {
            renderBattle();
            $$('.lane-head .name').forEach(n => n.classList.add('reveal'));
          } else paintVote(b);
          for (const side of ['a', 'b']) {
            const { lane } = laneParts(side);
            lane.classList.toggle('won', b.vote === side);
            lane.classList.toggle('lost', (b.vote === 'a' || b.vote === 'b') && b.vote !== side);
          }
          if (b.vote === 'a' || b.vote === 'b') {
            const who = b.sides[b.vote] === 'base' ? t('base_model') : t('with_adapter');
            toast(t('winner_is', { who }), 'ok');
          }
          renderSideBody();
        } catch (e) { fail(e); }
      };
    });
    slot.appendChild(bar);
  }

  async function fight(prompt) {
    if (!ready()) { toast(t('model_loading_wait'), 'bad'); return; }
    const prefs = settingsOf().battle || {};
    S.battle = { id: null, prompt, blind: prefs.blind, sides: { a: 'base', b: 'adapter' }, vote: null, a: {}, b: {} };
    renderBattle();
    const lanes = {};
    for (const side of ['a', 'b']) {
      const { lane, body, foot } = laneParts(side);
      body.innerHTML = '';
      body.classList.add('live');
      foot.innerHTML = `<span class="wait"><span class="spinner"></span>${prefs.mode === 'sequential' && side === 'b' ? t('waiting_turn') : t('reading_prompt')}</span>`;
      lanes[side] = { lane, body, foot, text: '', think: '', thinkNode: null, frame: 0 };
    }
    const paint = side => {
      const l = lanes[side];
      l.frame = 0;
      const thinkHtml = l.thinkNode ? '' : '';
      l.body.innerHTML = thinkHtml + window.MD.render(l.text);
      if (l.thinkNode) l.body.prepend(l.thinkNode);
      decorateCode(l.body);
      l.body.scrollTop = l.body.scrollHeight;
    };
    S.battleRun = { ctrl: new AbortController(), runId: null };
    paintBattleSend();
    try {
      await sse('/local/battle', { prompt }, (event, d) => {
        const l = d.side ? lanes[d.side] : null;
        switch (event) {
          case 'run': S.battleRun.runId = d.run_id; break;
          case 'battle': S.battle.id = d.id; S.battle.blind = d.blind; if (d.sides) S.battle.sides = d.sides; break;
          case 'side_start': l.lane.classList.add('running'); l.foot.innerHTML = `<span class="wait"><span class="spinner"></span>${t('reading_prompt')}</span>`; break;
          case 'progress':
            if (l && d.total) l.foot.innerHTML = `<span class="wait"><span class="spinner"></span>${t('reading_prompt')} ${num(Math.round(d.done / d.total * 100))}%</span>`;
            break;
          case 'think':
            l.think += d.text;
            if (!l.thinkNode) l.thinkNode = thinkEl('', true);
            $('.think-text', l.thinkNode).textContent = l.think;
            $('.lbl', l.thinkNode).textContent = t('reasoning_live', { n: num(l.think.split(/\s+/).filter(Boolean).length) });
            if (!l.frame) l.frame = requestAnimationFrame(() => paint(d.side));
            l.foot.innerHTML = `<span class="wait"><span class="spinner"></span>${t('thinking')}</span>`;
            break;
          case 'think_end': if (l.thinkNode) l.thinkNode.open = false; break;
          case 'delta':
            l.text += d.text;
            if (!l.frame) l.frame = requestAnimationFrame(() => paint(d.side));
            l.foot.innerHTML = `<span class="wait"><span class="spinner"></span>${t('writing')}</span>`;
            break;
          case 'side_done':
            l.lane.classList.remove('running');
            l.body.classList.remove('live');
            if (l.frame) cancelAnimationFrame(l.frame);
            l.text = d.text || '';
            paint(d.side);
            l.foot.innerHTML = laneStats(d.stats) + (d.error ? `<span style="color:var(--bad)">${esc(d.error)}</span>` : '');
            S.battle[d.side] = d;
            break;
          case 'error': if (l) l.foot.innerHTML = `<span style="color:var(--bad)">${esc(d.detail)}</span>`; else toast(d.detail, 'bad'); break;
          case 'done':
            S.battle = d.battle;
            S.score = d.score;
            S.battles = [d.battle, ...S.battles.filter(x => x.id !== d.battle.id)];
            paintVote(S.battle);
            renderSideBody();
            break;
          default: break;
        }
      }, S.battleRun.ctrl.signal);
    } catch (e) { if (e.name !== 'AbortError') fail(e); }
    finally {
      for (const side of ['a', 'b']) { lanes[side].body.classList.remove('live'); lanes[side].lane.classList.remove('running'); }
      S.battleRun = null;
      paintBattleSend();
    }
  }

  /* ---- models ---------------------------------------------------------------------- */

  function renderModels() {
    const view = $('#view-models');
    view.innerHTML = `<div class="page scroll"><div class="page-in">
      <h1>${t('models_title')}</h1><p class="lead">${t('models_lead')}</p>
      <div id="mRunning"></div>
      <div class="section"><h2>${ic('cube')}${t('on_this_computer')}</h2><div id="mInstalled"></div></div>
      <div class="section" id="mHubWrap"><h2>${ic('hf')}${t('hub_title')}<span class="badge acc">${t('new')}</span></h2><div id="mHub"></div></div>
      <div class="section"><h2>${ic('download')}${t('downloads')}</h2><div id="mJobs"></div></div>
      <div class="section"><h2>${ic('layers')}${t('nimbus_models')}</h2><div id="mNimbus"></div></div>
    </div></div>`;
    renderHub();
    updateModels(true);
  }

  function updateModels(force) {
    if (!$('#mRunning')) return;
    const st = S.status;
    const sig = [st.state, st.error, st.error_kind, st.state === 'loading' ? st.log : '', st.profile, st.active, st.bases, st.adapters,
                 st.builds.map(b => [b.installed, b.partial_bytes > 0]), (st.jobs || []).map(j => [j.id, j.state])];
    if (!changed('models', sig) && !force) { paintJobs(); return; }
    const said = { ready: t('state_ready'), loading: t('state_loading'), failed: t('state_failed'), missing: t('state_missing') }[st.state];
    $('#mRunning').innerHTML = `<div class="card"><div class="running">
      <span class="big-ico"><svg class="mark" aria-hidden="true"><use href="#i-mark"/></svg></span>
      <span class="what"><b dir="auto">${esc(st.profile.name)}</b><small dir="auto">${esc(st.profile.base || t('state_missing'))}${st.profile.adapter ? ' + ' + esc(st.profile.adapter) : ''}</small></span>
      <span class="badge ${st.state === 'ready' ? 'ok' : st.state === 'failed' ? 'bad' : 'warn'}">${esc(said)}</span>
      <button class="btn sm" id="restartBtn">${ic('refresh', 'sm')}${t('restart')}</button>
      <button class="btn sm ghost" id="openModels">${ic('folder', 'sm')}${t('open_folder')}</button></div>
      ${st.state === 'failed' && st.error_kind === 'blocked'
        ? `<div class="note bad blocked"><b>${t('blocked_title')}</b><p>${t('blocked_text')}</p><p>${t('blocked_how')}</p></div>`
        : st.state === 'failed' || st.state === 'loading' ? `<div class="log">${esc(st.state === 'failed' ? st.error : st.log || t('state_loading'))}</div>` : ''}</div>`;
    $('#restartBtn').onclick = () => api('/local/restart', {}).then(s => { S.status = s; paintStatus(null); }).catch(fail);
    $('#openModels').onclick = () => api('/local/open-folder?which=models', {}).catch(fail);

    const installed = $('#mInstalled');
    if (!st.bases.length) {
      installed.innerHTML = `<div class="card"><div class="side-empty">${t('no_models_yet_long')}</div></div>`;
    } else {
      const grid = h('<div class="grid stagger"></div>');
      for (const base of st.bases) {
        const on = st.active.base === base.key;
        const compatible = st.adapters.filter(a => !(base.arch && a.arch && base.arch !== a.arch)
          && !(a.builtin && base.family && ('builtin:' + base.family) !== a.key) && !(a.builtin && !base.family && !base.arch));
        const card = h(`<div class="mcard${on ? ' on' : ''}"><div class="head"><span class="ico">${ic('cube')}</span>
          <span class="t"><b dir="auto">${esc(base.name)}</b><small class="ltr" style="direction:inherit">${esc(base.source || '')}</small></span>
          ${base.key.startsWith('lib:') || !on ? `<button class="icon-btn" data-a="del" title="${t('delete')}">${ic('trash', 'sm')}</button>` : ''}</div>
          <div class="tags"><span class="badge acc ltr">${esc(base.quant || '?')}</span><span class="badge">${bytes(base.bytes)}</span>${base.arch ? `<span class="badge ltr">${esc(base.arch)}</span>` : ''}${on ? `<span class="badge ok">${t('in_use')}</span>` : ''}</div>
          <div class="acts"><select class="input" aria-label="${t('adapter')}"><option value="">${t('no_adapter')}</option>
          ${compatible.map(a => `<option value="${esc(a.key)}"${on && st.active.adapter === a.key ? ' selected' : ''}>${esc(a.name)}</option>`).join('')}</select>
          <button class="btn primary sm" data-a="use">${on ? t('apply') : t('use')}</button></div></div>`);
        if (!on && compatible.some(a => a.builtin)) $('select', card).value = compatible.find(a => a.builtin).key;
        $('[data-a="use"]', card).onclick = async () => {
          try {
            S.status = await api('/local/select', { base: base.key, adapter: $('select', card).value });
            paintStatus(null);
            toast(t('switching_to', { name: S.status.profile.name }));
          } catch (e) { fail(e); }
        };
        const del = $('[data-a="del"]', card);
        if (del) del.onclick = async () => {
          if (!await modal({ title: t('delete_model_q'), text: `${base.name} · ${bytes(base.bytes)}`, ok: t('delete'), danger: true })) return;
          try { S.status = await api('/local/models/delete', { key: base.key }); updateModels(); } catch (e) { fail(e); }
        };
        grid.appendChild(card);
      }
      installed.innerHTML = '';
      installed.appendChild(grid);
      const adapters = st.adapters.filter(a => !a.builtin);
      if (adapters.length) {
        installed.appendChild(h(`<h3 style="font-size:13px;color:var(--muted);margin:18px 0 8px">${t('your_adapters')}</h3>`));
        const agrid = h('<div class="grid stagger"></div>');
        for (const a of adapters) {
          const card = h(`<div class="mcard"><div class="head"><span class="ico">${ic('layers')}</span><span class="t"><b dir="auto">${esc(a.name)}</b><small class="ltr" style="direction:inherit">${esc(a.source || '')}</small></span>
            <button class="icon-btn" data-a="del" title="${t('delete')}">${ic('trash', 'sm')}</button></div>
            <div class="tags"><span class="badge">${bytes(a.bytes)}</span>${a.arch ? `<span class="badge ltr">${esc(a.arch)}</span>` : ''}</div></div>`);
          $('[data-a="del"]', card).onclick = async () => {
            if (!await modal({ title: t('delete_adapter_q'), text: a.name, ok: t('delete'), danger: true })) return;
            try { S.status = await api('/local/models/delete', { key: a.key }); updateModels(); } catch (e) { fail(e); }
          };
          agrid.appendChild(card);
        }
        installed.appendChild(agrid);
      }
    }

    const nimbus = $('#mNimbus');
    nimbus.innerHTML = '';
    const ngrid = h('<div class="nimbus stagger"></div>');
    for (const model of st.models) {
      const builds = st.builds.filter(b => b.model === model.id);
      const card = h(`<div class="mcard"><div class="head"><span class="ico"><svg class="mark" aria-hidden="true"><use href="#i-mark"/></svg></span>
        <span class="t"><b>${esc(model.name)}</b><small class="ltr" style="direction:inherit">${esc(model.base_repo)}</small></span></div>
        <p dir="auto">${esc(S.lang === 'fa' ? model.summary_fa : model.summary_en)}</p><div class="builds"></div></div>`);
      for (const b of builds) {
        const job = (st.jobs || []).find(j => j.title.endsWith('· ' + b.key) && ['queued', 'running'].includes(j.state));
        const row = h(`<div class="build"><span class="t"><b>${esc(b.key)}</b><small dir="auto">${esc(S.lang === 'fa' ? b.label_fa : b.label_en)} · ${bytes(b.bytes)} · ${t('ram_hint', { n: num(b.ram_hint_gb) })}</small></span></div>`);
        if (b.installed) row.appendChild(h(`<span class="badge ok">${ic('check', 'sm')}${t('installed')}</span>`));
        else if (job) row.appendChild(h(`<span class="badge warn"><span class="spinner" style="width:11px;height:11px"></span>${t('in_queue')}</span>`));
        else {
          const btn = h(`<button class="btn sm">${ic('download', 'sm')}${b.partial_bytes ? t('resume') : t('download')}</button>`);
          btn.onclick = async () => {
            try { S.status = await api('/local/download?build=' + encodeURIComponent(b.key), {}); toast(t('added_to_downloads'), 'ok'); updateModels(); refresh(); } catch (e) { fail(e); }
          };
          row.appendChild(btn);
        }
        $('.builds', card).appendChild(row);
      }
      ngrid.appendChild(card);
    }
    nimbus.appendChild(ngrid);
    paintJobs();
  }

  function paintJobs() {
    const box = $('#mJobs');
    if (!box) return;
    const jobs = S.status.jobs || [];
    if (!jobs.length) { box.innerHTML = `<div class="card"><div class="side-empty">${t('no_downloads')}</div></div>`; return; }
    let list = $('.jobs', box);
    if (!list) { box.innerHTML = ''; list = h('<div class="jobs"></div>'); box.appendChild(list); }
    const seen = new Set();
    for (const job of [...jobs].reverse()) {
      seen.add(job.id);
      let node = $(`.job[data-id="${job.id}"]`, list);
      if (!node) {
        node = h(`<div class="job" data-id="${esc(job.id)}"><div class="top-line"><span class="step-ico">${ic(job.kind === 'build' || job.kind === 'model' ? 'cube' : 'layers', 'sm')}</span><b dir="auto"></b><span class="badge st"></span><span class="acts" style="display:flex;gap:4px"></span></div>
          <div class="prog"><i></i></div><div class="line2"></div><div class="err"></div></div>`);
        list.appendChild(node);
      }
      node.dataset.state = job.state;
      $('b', node).textContent = job.title;
      const pct = job.total ? job.done / job.total * 100 : (job.state === 'done' ? 100 : 0);
      $('.prog i', node).style.width = Math.min(100, pct) + '%';
      const stateBadge = { queued: ['', t('j_queued')], running: ['acc', t('j_running')], converting: ['warn', t('j_converting')], done: ['ok', t('j_done')],
                           failed: ['bad', t('j_failed')], cancelled: ['', t('j_cancelled')] }[job.state] || ['', job.state];
      const badge = $('.badge.st', node);
      badge.className = 'badge st ' + stateBadge[0];
      badge.textContent = stateBadge[1];
      $('.line2', node).innerHTML = `<span>${bytes(job.done)} / ${bytes(job.total)}</span>`
        + (job.state === 'running' && job.speed ? `<span>${bytes(job.speed)}/s</span>` : '')
        + (job.state === 'running' && job.seconds_left ? `<span>${t('left', { t: dur(job.seconds_left) })}</span>` : '')
        + (job.current ? `<span class="ltr">${esc(job.current)}</span>` : '')
        + (job.count > 1 ? `<span>${t('n_files', { n: num(job.count) })}</span>` : '');
      $('.err', node).textContent = job.error || '';
      const acts = $('.acts', node);
      const want = ['queued', 'running'].includes(job.state) ? 'cancel' : ['failed', 'cancelled'].includes(job.state) ? 'retry' : 'remove';
      if (acts.dataset.want !== want + job.state) {
        acts.dataset.want = want + job.state;
        acts.innerHTML = '';
        const mk = (action, icon, label) => {
          const b = h(`<button class="btn sm ghost" title="${label}">${ic(icon, 'sm')}${label}</button>`);
          b.onclick = () => api(`/local/jobs/${job.id}/${action}`, {}).then(s => { S.status = s; paintJobs(); }).catch(fail);
          acts.appendChild(b);
        };
        if (want === 'cancel') mk('cancel', 'x', t('cancel'));
        if (want === 'retry') { mk('retry', 'refresh', t('resume')); mk('remove', 'trash', t('remove')); }
        if (want === 'remove') mk('remove', 'x', t('dismiss'));
      }
    }
    $$('.job', list).forEach(n => { if (!seen.has(n.dataset.id)) n.remove(); });
  }

  function renderHub() {
    const box = $('#mHub');
    box.innerHTML = `<div class="card"><p class="lead" style="margin:0 0 14px">${t('hub_lead')}</p>
      <div class="hub-cols">
        ${['model', 'adapter'].map(kind => `<div class="hub-col" data-kind="${kind}">
          <h3>${ic(kind === 'model' ? 'cube' : 'layers', 'sm')}${t(kind === 'model' ? 'hub_model' : 'hub_adapter')}</h3>
          <p>${t(kind === 'model' ? 'hub_model_p' : 'hub_adapter_p')}</p>
          <div class="with-btn"><input class="input mono" dir="ltr" placeholder="${kind === 'model' ? 'https://huggingface.co/Qwen/Qwen3-8B-GGUF' : 'https://huggingface.co/owner/my-lora'}" value="${esc((S.hub[kind] && S.hub[kind].link) || '')}">
          <button class="btn" data-a="detect">${ic('search', 'sm')}${t('detect')}</button></div>
          <div class="found-slot"></div></div>`).join('')}
      </div>
      <div class="hub-foot">
        <span class="muted" style="font-size:12.5px" id="hubSummary"></span>
        <button class="btn primary" id="hubAdd" disabled>${ic('download', 'sm')}${t('add_to_downloads')}</button></div></div>`;
    for (const col of $$('.hub-col', box)) {
      const kind = col.dataset.kind;
      const input = $('input', col);
      const detect = async () => {
        const link = input.value.trim();
        if (!link) { input.focus(); return; }
        const button = $('[data-a="detect"]', col);
        button.disabled = true;
        button.innerHTML = `<span class="spinner"></span>${t('detecting')}`;
        try {
          const found = await api('/local/hub/inspect', { link, kind });
          found.link = link;
          S.hub[kind] = found;
          const recommended = found.choices.find(c => c.recommended && !c.installed);
          S.hub.sel[kind] = recommended ? recommended.id : '';
        } catch (e) {
          S.hub[kind] = { link, error: String(e.message || e) };
          S.hub.sel[kind] = '';
        }
        button.disabled = false;
        button.innerHTML = ic('search', 'sm') + t('detect');
        paintFound(kind);
      };
      $('[data-a="detect"]', col).onclick = detect;
      input.addEventListener('keydown', e => { if (e.key === 'Enter') { e.preventDefault(); detect(); } });
      input.addEventListener('paste', () => setTimeout(detect, 30));
      paintFound(kind);
    }
    $('#hubAdd').onclick = async () => {
      const button = $('#hubAdd');
      button.disabled = true;
      let added = 0;
      for (const kind of ['model', 'adapter']) {
        const found = S.hub[kind];
        const choice = S.hub.sel[kind];
        if (!found || !choice || found.error) continue;
        try {
          S.status = await api('/local/hub/add', { kind, repo: found.repo, choice });
          added++;
          S.hub[kind] = null;
          S.hub.sel[kind] = '';
        } catch (e) { fail(e); }
      }
      if (added) { toast(t('added_n', { n: num(added) }), 'ok'); renderHub(); paintJobs(); refresh(); }
      button.disabled = false;
    };
    paintHubSummary();
  }

  function paintFound(kind) {
    const col = $(`.hub-col[data-kind="${kind}"]`);
    if (!col) return;
    const slot = $('.found-slot', col);
    const found = S.hub[kind];
    slot.innerHTML = '';
    if (!found) { paintHubSummary(); return; }
    if (found.error) { slot.innerHTML = `<div class="note bad" style="margin-top:10px">${ic('alert', 'sm')} ${esc(found.error)}</div>`; paintHubSummary(); return; }
    const box = h(`<div class="found"><div class="found-h">${ic('hf', 'sm')}<b dir="ltr">${esc(found.repo)}</b>
      ${found.architecture ? `<span class="badge ltr">${esc(found.architecture)}</span>` : ''}
      ${found.gated ? `<span class="badge warn">${ic('lock', 'sm')}${t('gated')}</span>` : ''}
      ${found.base_model ? `<span class="badge acc ltr" title="${t('base_model')}">⟵ ${esc(found.base_model)}</span>` : ''}</div></div>`);
    if (found.note) box.appendChild(h(`<div class="note warn">${esc(found.note)}</div>`));
    if (found.suggestions && found.suggestions.length) {
      const s = h(`<div class="suggest"></div>`);
      for (const repo of found.suggestions) {
        const b = h(`<button type="button">${esc(repo)}</button>`);
        b.onclick = () => { $('input', col).value = 'https://huggingface.co/' + repo; $('[data-a="detect"]', col).click(); };
        s.appendChild(b);
      }
      box.appendChild(s);
    }
    found.choices.forEach((c, index) => {
      const row = h(`<button type="button" class="pick${S.hub.sel[kind] === c.id ? ' sel' : ''}" style="animation-delay:${index * 25}ms">
        <span class="radio"></span><span class="nm">${esc(c.name)}</span>
        ${c.quant ? `<span class="badge acc ltr">${esc(c.quant)}</span>` : ''}
        ${c.format === 'peft' ? `<span class="badge warn">${t('will_convert')}</span>` : ''}
        ${c.files && c.files.length > 1 && c.format !== 'peft' ? `<span class="badge">${t('n_parts', { n: num(c.files.length) })}</span>` : ''}
        ${c.recommended ? `<span class="badge ok">${t('recommended')}</span>` : ''}
        ${c.installed ? `<span class="badge ok">${ic('check', 'sm')}</span>` : ''}
        <span class="sz">${bytes(c.bytes)}</span></button>`);
      row.onclick = () => { S.hub.sel[kind] = S.hub.sel[kind] === c.id ? '' : c.id; paintFound(kind); };
      box.appendChild(row);
    });
    slot.appendChild(box);
    paintHubSummary();
  }

  function paintHubSummary() {
    const summary = $('#hubSummary');
    if (!summary) return;
    let total = 0, count = 0;
    for (const kind of ['model', 'adapter']) {
      const found = S.hub[kind];
      if (!found || found.error) continue;
      const c = found.choices.find(x => x.id === S.hub.sel[kind]);
      if (c) { total += c.bytes; count++; }
    }
    summary.textContent = count ? t('hub_summary', { n: num(count), size: bytes(total) }) : '';
    $('#hubAdd').disabled = !count;
  }

  /* ---- settings ------------------------------------------------------------------ */

  const PROVIDER_LOOK = { tavily: 'T', brave: 'B', exa: 'E', serper: 'S', jina: 'J' };

  function renderSettings() {
    const view = $('#view-settings');
    const sections = [['internet', 'globe'], ['agent', 'shield'], ['model', 'cpu'], ['skills', 'sparkle'], ['look', 'sun'], ['about', 'info']];
    view.innerHTML = `<div class="page scroll" id="settingsPage"><div class="page-in">
      <h1>${t('settings_title')}</h1><p class="lead">${t('settings_lead')}</p>
      <div class="settings-layout"><nav class="snav">${sections.map(([id, icon]) => `<button data-s="${id}">${ic(icon, 'sm')}${t('s_' + id)}</button>`).join('')}</nav>
      <div>${sections.map(([id]) => `<section class="card sgroup" id="s-${id}"></section>`).join('')}</div></div></div></div>`;
    $$('.snav button', view).forEach(b => { b.onclick = () => { S.settingsTab = b.dataset.s; paintSnav(); $('#s-' + b.dataset.s).scrollIntoView({ behavior: 'smooth', block: 'start' }); }; });
    paintInternet(); paintAgent(); paintModelSettings(); paintSkills(); paintLook(); paintAbout();
    paintSnav();
    setTimeout(() => { const target = $('#s-' + S.settingsTab); if (target) target.scrollIntoView({ block: 'start' }); }, 30);
    $('#settingsPage').addEventListener('scroll', () => {
      const page = $('#settingsPage');
      const top = page.getBoundingClientRect().top;
      let current = sections[0][0];
      for (const [id] of sections) { if ($('#s-' + id).getBoundingClientRect().top - top < 140) current = id; }
      if (current !== S.settingsTab) { S.settingsTab = current; paintSnav(); }
    });
  }
  const paintSnav = () => $$('.snav button').forEach(b => b.classList.toggle('on', b.dataset.s === S.settingsTab));

  function srow(title, sub, control) {
    const row = h(`<div class="srow"><span class="t"><b>${title}</b>${sub ? `<small>${sub}</small>` : ''}</span><span class="ctl"></span></div>`);
    $('.ctl', row).appendChild(control);
    return row;
  }

  function paintInternet() {
    const box = $('#s-internet');
    const net = S.status.internet;
    box.innerHTML = `<h2>${ic('globe')}${t('s_internet')}</h2><p>${t('internet_lead')}</p>
      <div class="modes">${[['off', 'off'], ['direct', 'wifi'], ['provider', 'key'], ['custom', 'code']].map(([m, icon]) => `
        <button class="mode-card${net.mode === m ? ' on' : ''}" data-m="${m}"><span class="ico">${ic(icon)}</span>${ic('check', 'sm check')}
        <b>${t('net_' + m)}</b><small>${t('net_' + m + '_p')}</small></button>`).join('')}</div>
      <div id="netDetail"></div>
      <div class="srow" style="border-top:1px solid var(--line)"><span class="t"><b>${t('proxy')}</b><small>${t('proxy_p')}</small></span>
        <span class="ctl"><input class="input mono" dir="ltr" id="proxyIn" placeholder="http://127.0.0.1:10809" value="${esc(net.proxy || '')}"><button class="btn sm" id="proxySave">${t('save')}</button></span></div>
      <div id="localRow"></div>
      <div style="display:flex;gap:10px;align-items:center;margin-top:10px"><button class="btn" id="netTest">${ic('bolt', 'sm')}${t('test_connection')}</button><span class="muted" style="font-size:12.5px">${t('test_p')}</span></div>
      <div id="netResult"></div>`;
    $$('.mode-card', box).forEach(card => {
      card.onclick = async () => {
        await setting({ internet: { mode: card.dataset.m }, web_on: card.dataset.m !== 'off' ? true : settingsOf().web_on }, true);
        paintInternet();
        toast(t('net_mode_set', { mode: t('net_' + card.dataset.m) }), 'ok', 1800);
      };
    });
    $('#proxySave').onclick = () => setting({ internet: { proxy: $('#proxyIn').value } });
    $('#localRow').appendChild(srow(t('allow_local'), t('allow_local_p'), switcher(net.allow_local, v => setting({ internet: { allow_local: v } }, true))));
    $('#netTest').onclick = async () => {
      const button = $('#netTest');
      button.disabled = true;
      button.innerHTML = `<span class="spinner"></span>${t('testing')}`;
      const result = await api('/local/internet/test', {}).catch(e => ({ ok: false, error: String(e.message || e) }));
      button.disabled = false;
      button.innerHTML = ic('bolt', 'sm') + t('test_connection');
      $('#netResult').innerHTML = result.ok
        ? `<div class="test-result ok">${ic('check', 'sm')} ${t('test_ok', { n: num(result.results), s: num(result.seconds, 1), via: esc(result.via) })}${result.first ? `<br><small dir="auto">${esc(result.first.title)}</small>` : ''}</div>`
        : `<div class="test-result bad">${ic('alert', 'sm')} ${esc(result.error || '')}</div>`;
    };
    const detail = $('#netDetail');
    if (net.mode === 'provider') {
      const grid = h('<div class="providers"></div>');
      for (const id of ['tavily', 'brave', 'exa', 'serper', 'jina']) {
        const card = h(`<button class="prov${net.provider === id ? ' on' : ''}"><span class="logo">${PROVIDER_LOOK[id]}</span>
          <span><b>${t('prov_' + id)}</b><small>${t('prov_' + id + '_p')}</small></span>${net.keys[id] ? ic('check', 'sm set') : ''}</button>`);
        card.onclick = async () => { await setting({ internet: { provider: id } }, true); paintInternet(); };
        grid.appendChild(card);
      }
      detail.appendChild(grid);
      const id = net.provider;
      const sites = { tavily: 'https://app.tavily.com', brave: 'https://api-dashboard.search.brave.com', exa: 'https://dashboard.exa.ai', serper: 'https://serper.dev', jina: 'https://jina.ai' };
      const keyRow = h(`<div class="field"><span>${t('api_key_for', { name: t('prov_' + id) })} ${net.keys[id] ? `<span class="badge ok">${t('key_set')} · <span class="ltr">${esc(net.keys[id])}</span></span>` : ''}</span>
        <div class="with-btn"><input class="input mono" type="password" dir="ltr" placeholder="${net.keys[id] ? t('replace_key') : t('paste_key')}" autocomplete="off">
        <button class="btn icon-btn" data-a="show" title="${t('show')}">${ic('eye', 'sm')}</button><button class="btn primary" data-a="save">${t('save')}</button></div>
        <small><button class="ltr" style="color:var(--accent-ink)" data-a="site">${ic('external', 'sm')} ${sites[id]}</button> — ${t('get_key_p')}</small></div>`);
      const input = $('input', keyRow);
      $('[data-a="show"]', keyRow).onclick = () => { input.type = input.type === 'password' ? 'text' : 'password'; };
      $('[data-a="save"]', keyRow).onclick = async () => {
        if (!input.value.trim()) { input.focus(); return; }
        await setting({ internet: { keys: { [id]: input.value.trim() } } });
        paintInternet();
      };
      $('[data-a="site"]', keyRow).onclick = () => api('/local/open-url', { url: sites[id] }).catch(fail);
      detail.appendChild(keyRow);
    } else if (net.mode === 'custom') {
      const c = net.custom || {};
      const form = h(`<div class="form-grid">
        <label class="field"><span>${t('c_name')}</span><input class="input" name="name" value="${esc(c.name || '')}" placeholder="My search"></label>
        <label class="field"><span>${t('c_method')}</span><select class="input" name="method"><option>GET</option><option${c.method === 'POST' ? ' selected' : ''}>POST</option></select></label>
        <label class="field wide"><span>${t('c_url')}</span><input class="input mono" dir="ltr" name="url" value="${esc(c.url || '')}" placeholder="https://api.example.com/search?q={query}&amp;n={count}"><small>${t('c_url_p')}</small></label>
        <label class="field wide"><span>${t('c_body')}</span><input class="input mono" dir="ltr" name="body" value="${esc(c.body || '')}" placeholder='{"query": "{query}", "limit": {count}}'></label>
        <label class="field"><span>${t('c_key_header')}</span><input class="input mono" dir="ltr" name="key_header" value="${esc(c.key_header || 'Authorization')}"></label>
        <label class="field"><span>${t('c_key_prefix')}</span><input class="input mono" dir="ltr" name="key_prefix" value="${esc(c.key_prefix || '')}" placeholder="Bearer "></label>
        <label class="field wide"><span>${t('c_key')} ${c.key ? `<span class="badge ok">${t('key_set')}</span>` : ''}</span><input class="input mono" type="password" dir="ltr" name="key" placeholder="${c.key ? t('replace_key') : t('paste_key')}"></label>
        <label class="field"><span>${t('c_results')}</span><input class="input mono" dir="ltr" name="results" value="${esc(c.results || 'results')}"></label>
        <label class="field"><span>${t('c_title')}</span><input class="input mono" dir="ltr" name="title" value="${esc(c.title || 'title')}"></label>
        <label class="field"><span>${t('c_link')}</span><input class="input mono" dir="ltr" name="link" value="${esc(c.link || 'url')}"></label>
        <label class="field"><span>${t('c_snippet')}</span><input class="input mono" dir="ltr" name="snippet" value="${esc(c.snippet || 'content')}"></label>
        <div class="wide" style="display:flex;justify-content:flex-end"><button class="btn primary">${t('save')}</button></div></div>`);
      $('.btn.primary', form).onclick = async () => {
        const custom = {};
        $$('input,select', form).forEach(f => { if (f.name === 'key' && !f.value) return; custom[f.name] = f.value; });
        await setting({ internet: { custom } });
        paintInternet();
      };
      detail.appendChild(form);
    } else if (net.mode === 'direct') {
      detail.appendChild(h(`<div class="note" style="background:var(--accent-soft);color:var(--accent-ink);margin-bottom:6px">${ic('info', 'sm')} ${t('direct_note')}</div>`));
    }
  }

  function paintAgent() {
    const box = $('#s-agent');
    const st = settingsOf();
    const agent = st.agent || {};
    box.innerHTML = `<h2>${ic('shield')}${t('s_agent')}</h2><p>${t('agent_lead')}</p>`;
    box.appendChild(srow(t('permission'), t('permission_p'),
      seg([['ask', t('perm_ask')], ['session', t('perm_session')], ['auto', t('perm_auto')]], st.permission, v => setting({ permission: v }, true).then(paintComposer))));
    box.appendChild(srow(t('tool_files'), t('tool_files_p'), switcher(agent.files !== false, v => setting({ agent: { files: v } }, true))));
    box.appendChild(srow(t('tool_commands'), t('tool_commands_p'), switcher(agent.commands !== false, v => setting({ agent: { commands: v } }, true))));
    const steps = h(`<input class="input" type="number" min="4" max="120" value="${agent.max_steps || 40}" style="width:90px">`);
    steps.onchange = () => setting({ agent: { max_steps: Number(steps.value) } }, true);
    box.appendChild(srow(t('max_steps'), t('max_steps_p'), steps));
    const timeout = h(`<input class="input" type="number" min="10" max="3600" value="${agent.command_timeout || 120}" style="width:90px">`);
    timeout.onchange = () => setting({ agent: { command_timeout: Number(timeout.value) } }, true);
    box.appendChild(srow(t('cmd_timeout'), t('cmd_timeout_p'), timeout));
    const where = h(`<span style="display:flex;gap:8px"><input class="input mono" dir="ltr" style="width:260px" value="${esc(st.workspace || '')}" placeholder="${esc(S.status.workspace || '')}"><button class="btn sm">${t('save')}</button></span>`);
    $('button', where).onclick = () => setting({ workspace: $('input', where).value });
    box.appendChild(srow(t('workspace'), t('workspace_p'), where));
  }

  function paintModelSettings() {
    const box = $('#s-model');
    const st = settingsOf();
    box.innerHTML = `<h2>${ic('cpu')}${t('s_model')}</h2><p>${t('model_lead')}</p>`;
    const context = h(`<select class="input" style="width:140px">${[4096, 8192, 16384, 32768, 65536].map(n => `<option value="${n}"${st.context === n ? ' selected' : ''}>${num(n)}</option>`).join('')}</select>`);
    context.onchange = () => setting({ context: Number(context.value) }).then(() => toast(t('restarting'), 'info'));
    box.appendChild(srow(t('context'), t('context_p'), context));
    const memory = h(`<select class="input" style="width:140px">${[0, 2, 4, 6, 10, 20].map(n => `<option value="${n}"${st.memory === n ? ' selected' : ''}>${n ? t('n_turns', { n: num(n) }) : t('off')}</option>`).join('')}</select>`);
    memory.onchange = () => setting({ memory: Number(memory.value) }, true).then(paintComposer);
    box.appendChild(srow(t('memory'), t('memory_p'), memory));
    const threads = h(`<input class="input" type="number" min="0" max="256" value="${st.threads || 0}" style="width:90px">`);
    threads.onchange = () => setting({ threads: Number(threads.value) }).then(() => toast(t('restarting'), 'info'));
    box.appendChild(srow(t('threads'), t('threads_p', { n: num(S.status.machine.cpus) }), threads));
    box.appendChild(srow(t('gpu'), t('gpu_p') + (S.status.gpu ? ' ' + esc(t('gpu_now', { what: S.status.gpu })) : ''),
      seg([['auto', t('gpu_auto')], ['off', t('gpu_off')]], st.gpu || 'auto', v => setting({ gpu: v }).then(() => toast(t('restarting'), 'info')))));
    const gpu = h(`<input class="input" type="number" min="0" max="999" value="${st.gpu_layers || 0}" style="width:90px">`);
    gpu.onchange = () => setting({ gpu_layers: Number(gpu.value) }).then(() => toast(t('restarting'), 'info'));
    box.appendChild(srow(t('gpu_layers'), t('gpu_layers_p'), gpu));
    const token = h(`<span style="display:flex;gap:8px"><input class="input mono" type="password" dir="ltr" placeholder="${S.status.hf_token ? t('replace_key') : 'hf_…'}" style="width:200px"><button class="btn sm">${t('save')}</button></span>`);
    $('button', token).onclick = async () => { await setting({ hf_token: $('input', token).value }); paintModelSettings(); };
    box.appendChild(srow(t('hf_token') + (S.status.hf_token ? ` <span class="badge ok">${t('key_set')}</span>` : ''), t('hf_token_p'), token));
  }

  async function paintSkills() {
    const box = $('#s-skills');
    await loadChips();
    box.innerHTML = `<h2>${ic('sparkle')}${t('s_skills')}</h2><p>${t('skills_lead')}</p><div class="chips-list"></div>
      <div class="form-grid" style="margin-top:14px"><label class="field wide"><span>${t('new_skill')}</span><input class="input" id="chipName" placeholder="${t('skill_name')}" dir="auto"></label>
      <label class="field wide"><textarea class="input" id="chipText" rows="4" placeholder="${t('skill_text')}" dir="auto"></textarea></label>
      <div class="wide" style="display:flex;justify-content:flex-end"><button class="btn primary" id="chipAdd">${ic('plus', 'sm')}${t('add_skill')}</button></div></div>`;
    const list = $('.chips-list', box);
    for (const chip of S.chips) {
      const row = h(`<div class="chip-row"><span class="step-ico">${ic('sparkle', 'sm')}</span><span class="t"><b dir="auto">${esc(chip.name)} ${chip.builtin ? `<span class="badge">${t('builtin')}</span>` : ''}</b><small dir="auto">${esc(chip.summary || '')}</small></span></div>`);
      row.appendChild(switcher(chip.on, async on => { chip.on = on; await setting({ chips_on: S.chips.filter(c => c.on).map(c => c.id) }, true); paintComposer(); }));
      if (!chip.builtin) {
        const del = h(`<button class="icon-btn" title="${t('delete')}">${ic('trash', 'sm')}</button>`);
        del.onclick = async () => { await api('/local/chips/' + chip.id, undefined, 'DELETE').catch(fail); paintSkills(); };
        row.appendChild(del);
      }
      list.appendChild(row);
    }
    $('#chipAdd').onclick = async () => {
      try {
        const chip = await api('/local/chips', { name: $('#chipName').value, content: $('#chipText').value });
        await setting({ chips_on: [...S.chips.filter(c => c.on).map(c => c.id), chip.id] }, true);
        toast(t('skill_added'), 'ok');
        paintSkills();
      } catch (e) { fail(e); }
    };
  }

  function paintLook() {
    const box = $('#s-look');
    const st = settingsOf();
    box.innerHTML = `<h2>${ic('sun')}${t('s_look')}</h2><p>${t('look_lead')}</p>`;
    box.appendChild(srow(t('language'), '', seg([['fa', 'فارسی'], ['en', 'English']], S.lang, v => setting({ language: v }, true))));
    box.appendChild(srow(t('theme'), '', seg([['system', t('theme_system')], ['light', t('theme_light')], ['dark', t('theme_dark')]], st.theme || 'system', v => setting({ theme: v }, true))));
    box.appendChild(srow(t('motion'), t('motion_p'), seg([['full', t('motion_full')], ['reduced', t('motion_reduced')]], st.motion || 'full', v => setting({ motion: v }, true))));
  }

  function paintAbout() {
    const box = $('#s-about');
    const st = S.status;
    const m = st.machine;
    box.innerHTML = `<h2>${ic('info')}${t('s_about')}</h2><p>${t('about_lead')}</p>
      <dl class="about"><dt>${t('version')}</dt><dd>AhoosAI Studio ${esc(st.version)}</dd>
      <dt>${t('machine')}</dt><dd>${esc(m.system)} ${esc(m.machine)} · ${m.cpus} CPU · ${bytes(m.ram_bytes)} RAM · ${bytes(m.free_disk_bytes)} free</dd>
      <dt>${t('runtime')}</dt><dd>${esc(st.runtime || '—')}</dd><dt>${t('models_folder')}</dt><dd>${esc(st.paths.models)}</dd><dt>${t('data_folder')}</dt><dd>${esc(st.paths.data)}</dd></dl>
      <div style="display:flex;gap:8px;margin-top:14px"><button class="btn sm" data-w="models">${ic('folder', 'sm')}${t('models_folder')}</button><button class="btn sm" data-w="data">${ic('folder', 'sm')}${t('data_folder')}</button></div>`;
    $$('[data-w]', box).forEach(b => { b.onclick = () => api('/local/open-folder?which=' + b.dataset.w, {}).catch(fail); });
  }

  /* ---- everywhere ------------------------------------------------------------------- */

  function renderAll() {
    renderSide();
    renderTop(true);
    renderChatView();
    if (S.view === 'battle') renderBattle();
    if (S.view === 'models') renderModels();
    if (S.view === 'settings') renderSettings();
  }

  document.addEventListener('click', e => {
    const link = e.target.closest('a[href]');
    if (!link) return;
    const href = link.getAttribute('href');
    if (/^https?:\/\//.test(href)) { e.preventDefault(); api('/local/open-url', { url: href }).catch(fail); }
  });

  document.addEventListener('keydown', e => {
    const mod = e.ctrlKey || e.metaKey;
    if (e.key === 'Escape') {
      if (openPop) { closePop(); return; }
      if (S.run) { stopRun(); return; }
      if (S.battleRun && S.battleRun.runId) { api('/local/stop', { run_id: S.battleRun.runId }).catch(() => {}); return; }
    }
    if (mod && e.key.toLowerCase() === 'n') { e.preventDefault(); newChat(); }
    if (mod && e.key.toLowerCase() === 'b') { e.preventDefault(); toggleSide(); }
    if (mod && e.key === ',') { e.preventDefault(); go('settings'); }
    if (mod && e.key === '`') { e.preventDefault(); toggleTerminal(); }
    if (mod && e.key.toLowerCase() === 'k') { e.preventDefault(); if (S.view !== 'chat') go('chat'); setTimeout(() => $('#chatSearch') && $('#chatSearch').focus(), 60); }
  });

  window.addEventListener('resize', () => { movePill(); closePop(); });

  async function start() {
    if (store.get('side') === 'hidden') $('#app').classList.add('side-hidden');
    try { S.status = await api('/local/status'); } catch (e) { await sleep(600); return start(); }
    applyLook();
    renderSide();
    renderTop();
    renderChatView();
    await Promise.all([loadChats(), loadChips(), loadBattles()]);
    const hasModel = S.status.bases.length > 0 || S.status.engine !== 'local';
    go(hasModel ? 'chat' : 'models');
    if (!hasModel) toast(t('welcome_no_model'), 'info', 9000);
    renderEmpty();
    refresh();
  }

  start();
})();
