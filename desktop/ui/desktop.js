/* ---------------------------------------------------------------------------
   The desktop's own controls, added to a page it does not own.

   index.html and app.js are generated from the web studio by
   tools/port_studio_ui.py and rewritten on every build, so nothing here edits
   them. This file runs after app.js and adds what only the desktop has: the
   two dials of Nimbus 2 Apex, memory between messages, a working folder, a
   gate in front of anything that touches it, and a shell.

   It reaches app.js in exactly one place -- window.EventSource. app.js builds
   its own stream URL and connects immediately, so a facade over EventSource is
   the only way to post the conversation first and to watch the reasoning
   arrive. Everything else is done by adding elements beside the page's own.
   --------------------------------------------------------------------------- */
(() => {
  'use strict';

  const $ = (s, r = document) => r.querySelector(s);
  const el = (tag, cls, html) => {
    const node = document.createElement(tag);
    if (cls) node.className = cls;
    if (html != null) node.innerHTML = html;
    return node;
  };
  const esc = s => String(s).replace(/[&<>"]/g, c =>
    ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));

  // What the level promises, word for word the same arithmetic as
  // nimbus2/controls.py. Two copies of one formula is a risk, so the number is
  // shown next to the level rather than hidden: a drift would be visible.
  const words = level => Math.max(12, Math.round(15 * Math.pow(level, 1.5)));

  const state = {
    model: '', engine: 'studio', models: [],
    level: 5, temperature: 5, memory: 0, mode: 'normal',
    permission: 'ask', folder: '',
    // This session's turns, kept here rather than read back from the page:
    // what was sent is known exactly, and the rendered bubble is not.
    turns: [],
  };

  const api = async (path, body) => {
    const options = body === undefined
      ? { method: 'POST' }
      : { method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(body) };
    const response = await fetch(path, options);
    if (!response.ok) throw new Error((await response.text()).slice(0, 300));
    return response.json();
  };

  const save = patch => {
    Object.assign(state, patch);
    api('/local/controls', patch).catch(() => {});
  };

  /* ---- the model picker ------------------------------------------------- */

  function paintModels() {
    const pop = $('#modelPop');
    const button = $('#modelBtn');
    if (!pop || !button) return;
    const active = state.models.find(m => m.id === state.model);
    if (active) {
      // The mark stays; only the name after it changes.
      const svg = button.querySelector('svg');
      button.textContent = ' ' + active.name;
      if (svg) button.prepend(svg);
    }
    pop.innerHTML = '<div class="pop-h">On this computer</div>';
    state.models.forEach(model => {
      const ready = model.installed && model.adapter;
      // One line each. Switching models is the thing this list is for, and a
      // sentence under every name turns four words into four paragraphs.
      const mark = model.id === state.model
        ? '<span class="st on" title="in use"></span>'
        : (ready ? '<span class="st"></span>'
                 : `<svg class="st dl" width="13" height="13" viewBox="0 0 24 24" fill="none"
                      stroke="currentColor" stroke-width="1.9"><title>not downloaded</title>
                      <path d="M12 3v12M7 11l5 5 5-5M5 21h14"/></svg>`);
      const row = el('button', 'pick', `<span class="nm">${esc(model.name)}</span>${mark}`);
      if (!ready || model.id === state.model) row.disabled = true;
      row.onclick = async () => {
        try {
          const status = await api('/local/setup/model?model=' + encodeURIComponent(model.id));
          adopt(status);
          paintAll();
        } catch (error) { alert(String(error.message || error)); }
      };
      pop.appendChild(row);
    });
    pop.appendChild(el('hr'));
    const link = el('a', 'pop-link', 'Model and downloads…');
    link.href = 'setup.html';
    pop.appendChild(link);
  }

  /* ---- the thermometer -------------------------------------------------- */

  // Cold to warm, picked rather than computed. Interpolating the hue from blue
  // to amber runs through the muddy yellows at 7 and 8 and looks like a fault;
  // ten chosen steps read as the scale on a weather map, and the green at 5 is
  // where the model was measured.
  const HEAT = ['#2b5fb8', '#3576cf', '#3f8fbf', '#57a08a', '#6fae5e',
                '#9fb84a', '#c9b83f', '#e0a038', '#e07a2f', '#d2552b'];
  const heat = t => HEAT[Math.max(1, Math.min(10, t)) - 1];

  const TEMPERATURE_SAYS = {
    1: ['Exact', 'the same answer every time, as nearly as sampling allows'],
    2: ['Exact', 'barely any room to wander'],
    3: ['Careful', 'a safe default for code and for facts'],
    4: ['Careful', 'a little room, still predictable'],
    5: ['Balanced', 'what the model was measured at'],
    6: ['Balanced', 'a wider choice of words'],
    7: ['Loose', 'for drafting and for ideas'],
    8: ['Loose', 'noticeably more varied'],
    9: ['Wild', 'surprising, and sometimes wrong about it'],
    10: ['Wild', 'as far as this model goes'],
  };

  function thermometer() {
    const holder = el('div', 'holder');
    const button = el('button', 'tool', `<svg width="14" height="14" viewBox="0 0 24 24" fill="none"
        stroke="currentColor" stroke-width="1.8" aria-hidden="true"><path
        d="M10 13.5V5a2 2 0 1 1 4 0v8.5a4 4 0 1 1-4 0z"/><path d="M12 9v5"/></svg>
      <span id="tempLabel">5/10</span>`);
    button.id = 'tempBtn';
    button.title = 'Temperature — how widely it picks its words';

    const pop = el('div', 'pop therm-pop');
    pop.id = 'tempPop';
    pop.innerHTML = `
      <div class="pop-h">Temperature &mdash; sampling, not thinking</div>
      <div class="therm-wrap">
        <div class="therm" id="therm" tabindex="0" role="slider"
             aria-label="Temperature" aria-valuemin="1" aria-valuemax="10" aria-valuenow="5">
          <div class="therm-tube"><div class="therm-fill" id="thermFill"></div></div>
          <div class="therm-bulb" id="thermBulb">5</div>
        </div>
        <div class="therm-scale" id="thermScale"></div>
        <div class="therm-read">
          <b id="thermName">Balanced</b>
          <div class="sub" id="thermSay">what the model was measured at</div>
          <div class="num" id="thermNum"></div>
        </div>
      </div>`;
    holder.append(button, pop);

    const scale = $('#thermScale', pop);
    for (let t = 10; t >= 1; t--) {
      const tick = el('span', t % 2 ? '' : 'major', t % 2 ? '' : String(t));
      tick.dataset.t = String(t);
      scale.appendChild(tick);
    }

    const paint = () => {
      const t = state.temperature;
      const colour = heat(t);
      pop.style.setProperty('--therm', colour);
      $('#thermFill', pop).style.height = (t / 10 * 100) + '%';
      $('#thermBulb', pop).textContent = String(t);
      $('#tempLabel').textContent = t + '/10';
      const [name, say] = TEMPERATURE_SAYS[t];
      $('#thermName', pop).textContent = name;
      $('#thermSay', pop).textContent = say;
      $('#thermNum', pop).textContent = 'step ' + t + ' of 10';
      const therm = $('#therm', pop);
      therm.setAttribute('aria-valuenow', String(t));
      therm.setAttribute('aria-valuetext', `${t} of 10, ${name}`);
      scale.querySelectorAll('span').forEach(s =>
        s.classList.toggle('on', Number(s.dataset.t) === t));
    };

    const therm = $('#therm', pop);
    const fromY = event => {
      const box = therm.getBoundingClientRect();
      // Measured against the tube and the bulb together, top is hot: the whole
      // instrument is the control, which is how a thermometer is read.
      const share = 1 - (event.clientY - box.top) / box.height;
      return Math.max(1, Math.min(10, Math.round(share * 9) + 1));
    };
    let dragging = false;
    const set = t => { if (t !== state.temperature) { save({ temperature: t }); paint(); } };
    therm.addEventListener('pointerdown', e => {
      dragging = true; therm.setPointerCapture(e.pointerId); set(fromY(e)); e.preventDefault();
    });
    therm.addEventListener('pointermove', e => { if (dragging) set(fromY(e)); });
    therm.addEventListener('pointerup', () => { dragging = false; });
    therm.addEventListener('keydown', e => {
      const step = { ArrowUp: 1, ArrowRight: 1, ArrowDown: -1, ArrowLeft: -1 }[e.key];
      if (step) { set(Math.max(1, Math.min(10, state.temperature + step))); e.preventDefault(); }
      if (e.key === 'Home') { set(1); e.preventDefault(); }
      if (e.key === 'End') { set(10); e.preventDefault(); }
    });
    scale.addEventListener('click', e => {
      const tick = e.target.closest('span');
      if (tick) set(Number(tick.dataset.t));
    });

    button.onclick = e => { e.stopPropagation(); togglePop(pop); };
    return { holder, paint };
  }

  /* ---- the ladder, 1 to 20 --------------------------------------------- */

  function ladder() {
    const holder = el('div', 'holder');
    const button = el('button', 'tool', `<svg width="14" height="14" viewBox="0 0 24 24" fill="none"
        stroke="currentColor" stroke-width="1.8" aria-hidden="true"><path
        d="M9 18h6M10 22h4M12 2a7 7 0 0 0-4 12.7V17h8v-2.3A7 7 0 0 0 12 2z"/></svg>
      <span id="ladderLabel">Level 5</span>`);
    button.id = 'ladderBtn';
    button.title = 'Thinking level — how long it reasons before answering';

    const pop = el('div', 'pop ladder-pop');
    pop.id = 'ladderPop';
    pop.innerHTML = `
      <div class="ladder">
        <div class="ladder-head"><b>Thinking level</b><span class="lvl" id="ladderNum">5</span></div>
        <div class="ladder-sub" id="ladderSay"></div>
        <input type="range" min="1" max="20" step="1" value="5" id="ladderRange"
               aria-label="Thinking level, 1 to 20">
        <div class="ladder-ticks"><span>1</span><span>5</span><span>10</span><span>15</span><span>20</span></div>
      </div>`;
    holder.append(button, pop);

    const paint = () => {
      const level = state.level;
      $('#ladderRange', pop).value = String(level);
      $('#ladderNum', pop).textContent = String(level);
      $('#ladderLabel').textContent = 'Level ' + level;
      $('#ladderSay', pop).textContent =
        `about ${words(level).toLocaleString()} words of reasoning, and a ceiling at two and a half `
        + `times that. Level 5 is what it was trained and measured at.`;
    };
    $('#ladderRange', pop).addEventListener('input', e => {
      state.level = Number(e.target.value); paint();
    });
    $('#ladderRange', pop).addEventListener('change', e => save({ level: Number(e.target.value) }));
    button.onclick = e => { e.stopPropagation(); togglePop(pop); };
    return { holder, paint };
  }

  /* ---- plan against normal, and memory --------------------------------- */

  function modeToggle() {
    const wrap = el('div', 'mode');
    wrap.id = 'modeToggle';
    wrap.title = 'Plan first, or answer directly';
    // The lit half is one element that slides, not a background that jumps
    // between two buttons: the movement is what says the two are one switch.
    wrap.appendChild(el('i', 'mode-slide'));
    const make = (value, label, hint) => {
      const button = el('button', '', label);
      button.title = hint;
      button.onclick = () => { save({ mode: value }); paintAll(); };
      button.dataset.mode = value;
      return button;
    };
    wrap.append(
      make('normal', 'Normal', 'Answer the message'),
      make('plan', 'Plan', 'Write a plan first and wait for it to be accepted'));
    const paint = () => {
      wrap.dataset.mode = state.mode;
      wrap.querySelectorAll('button').forEach(b =>
        b.classList.toggle('on', b.dataset.mode === state.mode));
    };
    return { holder: wrap, paint };
  }

  function memoryRow() {
    const pop = $('#gearPop');
    if (!pop) return { paint() {} };
    pop.appendChild(el('hr'));
    pop.appendChild(el('div', 'pop-h', 'This computer'));

    const memory = el('button', '', '');
    memory.onclick = () => {
      // Off, four turns, or twelve. A number nobody picked is a number nobody
      // can reason about, so there are three settings and they are named.
      const next = { 0: 4, 4: 12, 12: 0 }[state.memory] ?? 0;
      save({ memory: next }); paintAll();
    };
    const permission = el('button', '', '');
    permission.onclick = () => {
      save({ permission: state.permission === 'ask' ? 'session' : 'ask' }); paintAll();
    };
    const term = el('button', '', '<b>Terminal</b><span class="sub">a shell in the working folder</span>');
    term.onclick = () => { toggleTerminal(); closePops(); };
    pop.append(memory, permission, term);

    const paint = () => {
      const said = { 0: 'off — each message stands alone',
                     4: 'the last four turns go back with each message',
                     12: 'the last twelve turns go back with each message' }[state.memory];
      memory.innerHTML = `<b>Memory</b><span class="sub">${esc(said)}</span>`;
      permission.innerHTML = '<b>Before it changes anything</b><span class="sub">'
        + (state.permission === 'ask'
          ? 'ask every single time'
          : 'ask once per thing, until the app closes') + '</span>';
      const hint = document.querySelector('.hint');
      if (hint) {
        hint.textContent = 'Enter sends · Shift+Enter for a new line · '
          + (state.memory ? `remembers ${state.memory} turns` : 'no memory between messages');
      }
    };
    return { paint };
  }

  /* ---- the working folder ---------------------------------------------- */

  function folderChip() {
    const chip = el('div', 'folder none');
    chip.id = 'folderChip';
    const pick = async () => {
      try {
        const answer = await api('/local/pick-folder');
        if (answer.folder) { state.folder = answer.folder; paintAll(); }
      } catch (error) {
        // No window means the app is running in a browser, where the operating
        // system will not open a folder dialog for a web page. Saying which
        // folder is then the only way, and it is still checked server-side.
        const typed = prompt('Path of the folder to work in:', state.folder || '');
        if (!typed) return;
        try {
          const answer = await api('/local/set-folder', { folder: typed });
          state.folder = answer.folder; paintAll();
        } catch (inner) { alert(String(inner.message || inner)); }
      }
    };
    const paint = () => {
      chip.classList.toggle('none', !state.folder);
      if (state.folder) {
        const name = state.folder.split(/[\\/]/).filter(Boolean).pop() || state.folder;
        chip.innerHTML = `<svg width="13" height="13" viewBox="0 0 24 24" fill="none"
            stroke="currentColor" stroke-width="1.8" aria-hidden="true"><path
            d="M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"/></svg>
          <b title="${esc(state.folder)}">${esc(name)}</b>`;
        const change = el('button', '', 'Change');
        change.onclick = pick;
        chip.appendChild(change);
      } else {
        chip.innerHTML = '<span>No working folder</span>';
        const choose = el('button', '', 'Choose…');
        choose.onclick = pick;
        chip.appendChild(choose);
      }
    };
    return { holder: chip, paint };
  }

  /* ---- the gate --------------------------------------------------------- */

  // Asked in the conversation, where the action was asked for, and left behind
  // afterwards as the record of what was allowed. Returns what the person said.
  function askPermission(what, detail) {
    return new Promise(resolve => {
      const card = el('div', 'ask');
      card.innerHTML = `
        <div class="ask-h"><svg width="15" height="15" viewBox="0 0 24 24" fill="none"
            stroke="currentColor" stroke-width="1.8"><path d="M12 9v4M12 17h.01"/><path
            d="M10.3 3.9 2.6 17a2 2 0 0 0 1.7 3h15.4a2 2 0 0 0 1.7-3L13.7 3.9a2 2 0 0 0-3.4 0z"/></svg>
          ${esc(what)}</div>
        <div class="ask-what">${esc(detail)}</div>
        <div class="ask-row"></div>
        <div class="ask-said"></div>`;
      const row = $('.ask-row', card);
      const settle = (answer, said) => {
        card.classList.add('settled');
        $('.ask-said', card).textContent = said;
        resolve(answer);
      };
      const yes = el('button', 'yes', 'Allow');
      yes.onclick = () => settle({ allowed: true, remember: false }, 'Allowed once.');
      const once = el('button', 'once', 'Allow for this session');
      once.onclick = () => settle({ allowed: true, remember: true },
        'Allowed, and not asked again until the app closes.');
      const no = el('button', 'no', 'Refuse');
      no.onclick = () => settle({ allowed: false, remember: false }, 'Refused.');
      row.append(yes, state.permission === 'session' ? once : el('span'), no);

      const stream = $('#stream');
      if (stream) {
        stream.appendChild(card);
        const log = $('#log');
        if (log) log.scrollTop = log.scrollHeight;
      }
    });
  }

  /* ---- the terminal ----------------------------------------------------- */

  let socket = null;

  function terminalPanel() {
    const panel = el('div');
    panel.id = 'term';
    panel.innerHTML = `
      <div class="term-bar">
        <span>Terminal</span><span class="where" id="termWhere">no working folder</span>
        <button id="termSystem">Open the system terminal</button>
        <button id="termClose">Close</button>
      </div>
      <div class="term-out" id="termOut"></div>
      <div class="term-in"><span>&gt;</span><input id="termIn" spellcheck="false"
        placeholder="a command, run in the working folder" autocomplete="off"></div>`;
    return panel;
  }

  const termSay = (text, cls) => {
    const out = $('#termOut');
    if (!out) return;
    out.appendChild(el('div', cls || '', esc(text)));
    out.scrollTop = out.scrollHeight;
  };

  function connectShell() {
    if (socket && socket.readyState <= 1) return socket;
    const url = (location.protocol === 'https:' ? 'wss://' : 'ws://') + location.host + '/local/shell';
    socket = new WebSocket(url);
    socket.onmessage = async event => {
      const message = JSON.parse(event.data || '{}');
      if (message.folder) { $('#termWhere').textContent = message.folder; return; }
      if (message.ask) {
        const answer = await askPermission('Run this command?', message.ask);
        if (!answer.allowed) { termSay('refused', 'bad'); return; }
        socket.send(JSON.stringify({ command: message.ask, allowed: true,
                                     remember: answer.remember }));
        return;
      }
      if (message.started) { termSay('> ' + message.started, 'cmd'); return; }
      if (message.line !== undefined) { termSay(message.line); return; }
      if (message.cut) { termSay('… too much output; the rest was not shown', 'bad'); return; }
      if (message.error) { termSay(message.error, 'bad'); return; }
      if (message.code !== undefined) {
        termSay(message.code === 0 ? 'done' : 'exit code ' + message.code,
                message.code === 0 ? 'code' : 'bad');
      }
    };
    socket.onerror = () => termSay('the shell connection failed', 'bad');
    socket.onclose = () => { socket = null; };
    return socket;
  }

  function toggleTerminal() {
    const panel = $('#term');
    if (!panel) return;
    const opening = !panel.classList.contains('on');
    panel.classList.toggle('on', opening);
    if (!opening) return;
    if (!state.folder) { termSay('Choose a working folder first.', 'bad'); return; }
    connectShell();
    $('#termIn').focus();
  }

  /* ---- the stream: memory in, reasoning out ----------------------------- */

  const Native = window.EventSource;

  async function rewrite(url) {
    // Only the message stream is touched, and only when there is something to
    // add to it. Anything else app.js asks for goes through untouched.
    if (!/^stream\.php\?prompt=/.test(String(url))) return url;
    const query = new URLSearchParams(String(url).split('?')[1] || '');
    const prompt = query.get('prompt') || '';
    state.turns.push({ role: 'user', content: prompt });
    if (!state.memory) return url;
    const history = state.turns.slice(0, -1).slice(-state.memory * 2);
    try {
      const answer = await api('/local/turn', { prompt, history });
      const kept = new URLSearchParams();
      kept.set('turn', answer.turn);
      if (query.get('effort')) kept.set('effort', query.get('effort'));
      return 'stream.php?' + kept.toString();
    } catch (error) {
      return url;                      // the message still goes, without its history
    }
  }

  class Relay {
    constructor(url) {
      this.waiting = [];
      this.closed = false;
      this.real = null;
      this.onerror = null;
      this.onmessage = null;
      rewrite(url).then(final => {
        if (this.closed) return;
        const source = new Native(final);
        this.real = source;
        this.waiting.forEach(([name, fn]) => source.addEventListener(name, fn));
        watch(source);
        source.onerror = event => { if (this.onerror) this.onerror(event); };
        if (this.onmessage) source.onmessage = this.onmessage;
      }).catch(() => { if (this.onerror) this.onerror(new Event('error')); });
    }
    addEventListener(name, fn) {
      this.waiting.push([name, fn]);
      if (this.real) this.real.addEventListener(name, fn);
    }
    removeEventListener(name, fn) { if (this.real) this.real.removeEventListener(name, fn); }
    close() { this.closed = true; if (this.real) this.real.close(); }
    get readyState() { return this.real ? this.real.readyState : 0; }
  }

  // The reasoning, as it is written. app.js renders stages and the finished
  // answer; this fills the stage it already made with what the model is
  // actually thinking, and keeps the answer for memory.
  function watch(source) {
    let box = null, count = null, said = '';
    const stage = () => document.querySelector('#stream .turn:last-child .stages .stage:first-child');
    source.addEventListener('apex:think', event => {
      const data = JSON.parse(event.data || '{}');
      said += data.text || '';
      if (!box) {
        const host = stage();
        if (!host) return;
        box = el('div', 'apex-think');
        count = el('div', 'apex-count');
        host.append(box, count);
      }
      box.textContent = said;
      box.scrollTop = box.scrollHeight;
      const n = said.split(/\s+/).filter(Boolean).length;
      count.textContent = `${n.toLocaleString()} words · level ${state.level} asks for about `
        + `${words(state.level).toLocaleString()}`;
      count.classList.toggle('over', n > words(state.level) * 2);
    });
    source.addEventListener('apex:closed', event => {
      const data = JSON.parse(event.data || '{}');
      if (count) {
        count.textContent = `${(data.words || 0).toLocaleString()} words against a target of `
          + `${(data.target || 0).toLocaleString()}`
          + (data.forced ? ' — closed at the ceiling' : ' — it stopped on its own');
        count.classList.toggle('over', !!data.forced);
      }
    });
    source.addEventListener('done', event => {
      const data = JSON.parse(event.data || '{}');
      state.turns.push({ role: 'assistant', content: data.answer || '' });
    });
  }

  /* ---- plan mode -------------------------------------------------------- */

  // One model and no tools, so a plan is not a delegation: it is the model
  // saying what it would do, shown before it does it. The instruction is added
  // to the message rather than to the system prompt, because the system prompt
  // is the one the adapter was trained under and is not ours to edit.
  const PLAN_ASK = '\n\nFirst write the plan you would follow, as a short numbered list, '
    + 'and nothing else. Do not carry it out yet.';

  function hookPlanMode() {
    const input = $('#input');
    const send = $('#send');
    if (!input || !send) return;
    const wrap = handler => function (event) {
      if (state.mode === 'plan' && !input.dataset.planned) {
        const typed = input.innerText.trim();
        if (typed) {
          input.dataset.planned = '1';
          input.innerText = typed + PLAN_ASK;
        }
      } else {
        delete input.dataset.planned;
      }
      return handler.call(this, event);
    };
    // The page's own submit runs after this, and sees the edited box.
    send.addEventListener('click', wrap(() => {}), true);
    input.addEventListener('keydown', wrap(() => {}), true);
  }

  /* ---- putting it on the page ------------------------------------------ */

  const pops = [];
  const closePops = () => document.querySelectorAll('.pop').forEach(p => p.classList.remove('on'));
  const togglePop = pop => {
    const wasOn = pop.classList.contains('on');
    closePops();
    pop.classList.toggle('on', !wasOn);
  };

  let parts = {};
  const paintAll = () => Object.values(parts).forEach(p => p && p.paint && p.paint());

  function adopt(status) {
    state.model = status.model || state.model;
    state.engine = status.model_engine || state.engine;
    state.models = status.models || state.models;
    const controls = status.controls || {};
    Object.assign(state, {
      level: controls.level ?? state.level,
      temperature: controls.temperature ?? state.temperature,
      memory: controls.memory ?? state.memory,
      mode: controls.mode || state.mode,
      permission: controls.permission || state.permission,
      folder: controls.folder ?? state.folder,
    });
    // The two dials belong to Apex. Nimbus 1.1 was never trained under them,
    // so for that model the page keeps the effort ladder it always had.
    const apex = state.engine === 'apex';
    document.body.dataset.apex = apex ? '1' : '0';
    const show = (node, on) => { if (node) node.style.display = on ? '' : 'none'; };
    show(parts.temp && parts.temp.holder, apex);
    show(parts.ladder && parts.ladder.holder, apex);
    const think = $('#thinkBtn');
    show(think && think.parentElement, !apex);
  }

  async function start() {
    const tools = document.querySelector('.tools');
    const bar = $('#bar');
    const main = $('#main');
    if (!tools || !bar || !main) return;

    parts.temp = thermometer();
    parts.ladder = ladder();
    parts.mode = modeToggle();
    parts.folder = folderChip();
    parts.memory = memoryRow();

    // In the composer, before the attach button, so the dials sit with the
    // other things that change what this message will be.
    const attach = $('#attachBtn');
    tools.insertBefore(parts.ladder.holder, attach || null);
    tools.insertBefore(parts.temp.holder, attach || null);
    tools.insertBefore(parts.mode.holder, attach || null);

    // In the bar, before the theme buttons.
    bar.insertBefore(parts.folder.holder, $('#toggleTheme') || null);

    main.appendChild(terminalPanel());
    $('#termClose').onclick = () => $('#term').classList.remove('on');
    $('#termSystem').onclick = () => api('/local/open-terminal')
      .catch(error => termSay(String(error.message || error), 'bad'));
    $('#termIn').addEventListener('keydown', event => {
      if (event.key !== 'Enter') return;
      const command = event.target.value.trim();
      if (!command) return;
      event.target.value = '';
      const live = connectShell();
      const send = () => live.send(JSON.stringify({ command }));
      if (live.readyState === 1) send();
      else live.addEventListener('open', send, { once: true });
    });

    hookPlanMode();
    window.EventSource = Relay;

    try {
      const response = await fetch('/local/status');
      adopt(await response.json());
    } catch (error) { /* the page still works; the defaults are the trained ones */ }
    paintModels();
    paintAll();

    // A new chat is a new conversation, and memory does not cross it.
    const fresh = $('#newChat');
    if (fresh) fresh.addEventListener('click', () => { state.turns = []; });
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', start);
  } else {
    start();
  }
})();
