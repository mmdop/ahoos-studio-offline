/* AhoosAI Studio — client logic.
 *
 * Split out of index.php so the markup stays readable and the browser can cache
 * this separately. Configuration arrives on `window.CFG`, written by PHP.
 *
 * Everything that touches the database goes through Supabase directly from the
 * browser with the publishable key. That is safe only because every table has
 * row-level security: the key identifies the project, and the policies decide
 * what this signed-in person may read. There is no server-side check here to
 * forget, because there is no server-side path at all.
 *
 * The one thing PHP still does is hold the Nimbus key and stream the answer.
 */

const CFG = window.CFG;

/* ---- staying signed in -----------------------------------------------------
   Which Storage the client is handed IS "keep me signed in": localStorage
   outlives the browser, sessionStorage dies with the last tab. There is no
   third setting, and no amount of cookie work on the host side substitutes for
   it -- the gate is drawn from what the browser still holds.

   Read here, before the client is built, because a client cannot be moved to a
   different store afterwards. The OAuth redirect returns to a freshly loaded
   page, and whichever store this picked is where the returning session lands.

   The preference itself always lives in localStorage. Kept in sessionStorage it
   would forget that it had been asked to forget. */
const REMEMBER_KEY = 'keepSignedIn';
const SB_TOKEN = /^sb-.+-auth-token/;

function remembering(){
  try{ return localStorage.getItem(REMEMBER_KEY) !== 'no'; }
  catch(e){ return true; }   // private mode: behave as the box looks
}

const sb = window.supabase.createClient(CFG.url, CFG.key, {
  auth: {
    persistSession  : true,
    autoRefreshToken: true,
    detectSessionInUrl: true,
    storage: remembering() ? window.localStorage : window.sessionStorage
  }
});

const GEARS = {
  'CR-image': 'Not supported',
  'CR-file' : 'Delivers the work as files, not chat code blocks'
};
const $  = s => document.querySelector(s);
const $$ = s => Array.from(document.querySelectorAll(s));
const RTL = /[؀-ۿݐ-ݿ֐-׿]/;
const esc = s => String(s).replace(/[&<>"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));

let user = null;
let chatId = null;
let live = null;
// Resolves once stream.php has a session of its own. submit() waits on it, so
// the first message cannot outrun the exchange and come back "Not signed in".
let hostSession = null;

/** The empty-chat mark hides as soon as there is anything to read. */
function paintBlank(){
  const b = document.getElementById('blank');
  if(b) b.classList.toggle('gone', $('#stream').children.length > 0);
}

/* ---- theme ---------------------------------------------------------------- */
const body = document.body;
try{
  body.dataset.theme  = localStorage.getItem('theme')  || 'light';
  body.dataset.accent = localStorage.getItem('accent') || 'blue';
}catch(e){}
const paintSwatches = () =>
  $$('.swatch').forEach(s => s.classList.toggle('on', s.dataset.accent === body.dataset.accent));
paintSwatches();
$('#toggleTheme').onclick = () => {
  body.dataset.theme = body.dataset.theme === 'dark' ? 'light' : 'dark';
  try{ localStorage.setItem('theme', body.dataset.theme); }catch(e){}
};
$$('.swatch').forEach(s => s.onclick = () => {
  body.dataset.accent = s.dataset.accent;
  try{ localStorage.setItem('accent', s.dataset.accent); }catch(e){}
  paintSwatches();
});
/* On a phone the sidebar covers the conversation, so it starts closed and
   closes again after any choice: leaving it open over the thing you just
   picked is the bug people actually notice. */
const NARROW = () => window.innerWidth <= 760;
if(NARROW()) $('#side').classList.add('hidden');

const closeSide = () => { if(NARROW()) $('#side').classList.add('hidden'); };
$('#toggleSide').onclick = () => $('#side').classList.toggle('hidden');
$('#backdrop').onclick = closeSide;
$$('.tabs button').forEach(b => b.onclick = () => {
  $$('.tabs button').forEach(x => x.classList.toggle('on', x === b));
  $$('.pane').forEach(p => p.classList.toggle('on', p.id === b.dataset.pane));
  if(b.dataset.pane === 'explPane') loadFiles();
});

/* ---- auth ----------------------------------------------------------------- */
async function signIn(provider){
  const { error } = await sb.auth.signInWithOAuth({
    provider,
    // Back to this exact page. Supabase only honours it if the same URL is
    // listed under Authentication -> URL Configuration.
    options: { redirectTo: window.location.href.split('#')[0].split('?')[0] }
  });
  if(error) alert(error.message);
}
const keepBox = $('#keepSignedIn');
if(keepBox){
  keepBox.checked = remembering();
  keepBox.onchange = () => {
    try{
      localStorage.setItem(REMEMBER_KEY, keepBox.checked ? 'yes' : 'no');
      // Turning it off has to take away what turning it on already wrote.
      // Otherwise the next visit finds the old session sitting in the store it
      // was just told to stop using, and the box reads as broken.
      if(!keepBox.checked){
        Object.keys(localStorage).filter(k => SB_TOKEN.test(k))
              .forEach(k => localStorage.removeItem(k));
      }
    }catch(e){}
  };
}

$('#gGoogle').onclick = () => signIn('google');
$('#gGithub').onclick = () => signIn('github');
$('#signOut').onclick = async () => { await sb.auth.signOut(); location.reload(); };

/* A failed sign-in returns to this page with the reason in the URL, and without
 * this the person just sees the login screen again and learns nothing. Silent
 * failure is the expensive kind: the answer is already in the response. */
(function reportAuthError(){
  const p = new URLSearchParams(location.search);
  const h = new URLSearchParams(location.hash.replace(/^#/, ''));
  const code = p.get('error') || h.get('error');
  if(!code) return;
  const detail = p.get('error_description') || h.get('error_description') || '';
  const box = document.getElementById('authError');
  if(box){
    box.textContent = decodeURIComponent(detail.replace(/\+/g, ' ')) || code;
    box.style.display = 'block';
  }
  console.error('[auth]', code, detail);
})();

// Named so the sequence is visible in the console when a redirect lands: the
// exchange is asynchronous, so the gate showing first is normal and only a
// missing SIGNED_IN afterwards is a fault.
sb.auth.onAuthStateChange((event, session) =>
  console.log('[auth]', event, session ? 'session present' : 'no session'));

function showApp(session){
  user = session ? session.user : null;
  $('#gate').style.display = user ? 'none' : 'grid';
  $('#shell').style.display = user ? 'flex' : 'none';
  if(!user) return;
  // stream.php runs on this host and cannot see the Supabase session, so the
  // token is exchanged once for an ordinary PHP session cookie. Done here
  // rather than per request: EventSource cannot set headers, and a token in a
  // query string ends up in access logs.
  hostSession = sb.auth.getSession().then(({ data }) => {
    const token = data.session?.access_token;
    if(!token) return;
    return fetch('session.php', {
      method : 'POST',
      headers: {'Content-Type':'application/json'},
      // The host cookie is told the same thing the browser store was, so the
      // two halves expire together instead of one outliving the other.
      body   : JSON.stringify({access_token: token, remember: remembering()})
    }).catch(err => console.error('[auth] session exchange failed', err));
  });
  const name = user.user_metadata?.name || user.email || 'Signed in';
  $('#who').textContent = name;
  const pic = user.user_metadata?.avatar_url;
  if(pic) $('#avatar').src = pic; else $('#avatar').style.display = 'none';
  loadChats();
}
sb.auth.getSession().then(({ data }) => showApp(data.session));
sb.auth.onAuthStateChange((_e, session) => showApp(session));

/* ---- chats ---------------------------------------------------------------- */
async function loadChats(){
  const { data, error } = await sb.from('chats')
    .select('id,title,updated_at').order('updated_at', { ascending: false }).limit(20);
  const box = $('#history');
  if(error){ box.innerHTML = `<div class="empty">${esc(error.message)}</div>`; return; }
  if(!data.length){ box.innerHTML = '<div class="empty">No chats yet. They are kept on this computer.</div>'; return; }
  box.innerHTML = '';
  data.forEach(c => {
    const b = document.createElement('button');
    b.className = 'row' + (c.id === chatId ? ' on' : '');
    b.innerHTML = '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"><path d="M21 11.5a8.4 8.4 0 0 1-9 8.4 8.4 8.4 0 0 1-3.8-.9L3 21l2-4.9A8.4 8.4 0 0 1 12 3a8.4 8.4 0 0 1 9 8.5z"/></svg><span>' + esc(c.title) + '</span>';
    b.onclick = () => { openChat(c.id, c.title); closeSide(); };
    box.appendChild(b);
  });
}

async function newChat(title){
  const { data, error } = await sb.from('chats')
    .insert({ user_id: user.id, title: title.slice(0, 60) || 'New chat' })
    .select('id,title').single();
  if(error){ console.error(error); return null; }
  chatId = data.id;
  $('#chatTitle').textContent = data.title;
  loadChats();
  return data.id;
}

async function openChat(id, title){
  chatId = id;
  $('#chatTitle').textContent = title;
  $('#stream').innerHTML = '';
  paintBlank();
  loadChats();
  const { data } = await sb.from('messages')
    .select('role,content,meta').eq('chat_id', id).order('created_at');
  for(const m of (data || [])){
    if(m.role === 'user') addTurn(m.content, 'user', metaHtml(m.meta));
    else await addModelTurn(m.content, 'bot', metaHtml(m.meta));
  }
}

async function saveMessage(role, content, meta){
  if(!chatId) return;
  await sb.from('messages').insert({ chat_id: chatId, user_id: user.id, role, content, meta: meta || {} });
  await sb.from('chats').update({ updated_at: new Date().toISOString() }).eq('id', chatId);
}

$('#newChat').onclick = () => {
  closeSide();
  chatId = null;
  $('#stream').innerHTML = '';
  paintBlank();
  $('#chatTitle').textContent = 'New chat';
  $('#input').innerHTML = '';
  $('#input').focus();
  loadChats();
};

/* ---- files ----------------------------------------------------------------
   Anything the model writes as a fenced block with a language becomes a file.
   Storage keeps it under <user id>/..., which is what the storage policies
   check, and the row carries an expiry the read policy enforces.             */
/* Images are not supported offline: an answer is shown as written. */
async function ownDomain(text){ return String(text).trim(); }

const EXT = {javascript:'js',js:'js',typescript:'ts',python:'py',py:'py',html:'html',css:'css',
             json:'json',bash:'sh',sh:'sh',php:'php',toml:'toml',yaml:'yml',sql:'sql',svg:'svg',
             markdown:'md',md:'md',java:'java',go:'go',rust:'rs',c:'c',cpp:'cpp'};

async function storeFile(name, text, mime){
  const path = `${user.id}/${Date.now()}-${name}`;
  const { error } = await sb.storage.from('files')
    .upload(path, new Blob([text], { type: mime || 'text/plain' }), { upsert: false });
  if(error){ console.error(error); return; }
  await sb.from('files').insert({
    user_id: user.id, chat_id: chatId, name, path,
    mime: mime || 'text/plain', size: new Blob([text]).size
  });
}

async function captureFiles(answer){
  const re = /```(\w+)\n([\s\S]*?)```/g;
  let m, n = 0;
  while((m = re.exec(answer)) !== null){
    const ext = EXT[m[1].toLowerCase()];
    if(!ext) continue;                     // prose fences are not files
    n += 1;
    await storeFile(`nimbus-${Date.now()}-${n}.${ext}`, m[2], 'text/plain');
  }
  if(n) loadFiles();
}

async function loadFiles(){
  if(!user) return;
  const box = $('#files');
  const { data, error } = await sb.from('files')
    .select('id,name,path,size,expires_at').order('created_at', { ascending: false });
  if(error){ box.innerHTML = `<div class="empty">${esc(error.message)}</div>`; return; }
  if(!data.length){
    box.innerHTML = '<div class="empty">Files the model writes appear here. They are saved on this computer.</div>';
    return;
  }
  box.innerHTML = '';
  data.forEach(f => {
    const b = document.createElement('button');
    b.className = 'row';
    b.innerHTML = '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><path d="M14 2v6h6"/></svg>'
                + `<span>${esc(f.name)}</span>`;
    b.onclick = () => downloadOne(f);
    box.appendChild(b);
  });
}

async function fileBlob(f){
  const { data, error } = await sb.storage.from('files').download(f.path);
  if(error){ alert(error.message); return null; }
  return data;
}
async function downloadOne(f){
  const blob = await fileBlob(f);
  if(!blob) return;
  const a = document.createElement('a');
  a.href = URL.createObjectURL(blob); a.download = f.name; a.click();
  URL.revokeObjectURL(a.href);
}
$('#dlAll').onclick = async () => {
  const { data } = await sb.from('files').select('id,name,path');
  for(const f of (data || [])) await downloadOne(f);
};
$('#dlZip').onclick = async () => {
  const { data } = await sb.from('files').select('id,name,path');
  if(!data || !data.length){ alert('No files yet.'); return; }
  const zip = new JSZip();
  for(const f of data){
    const blob = await fileBlob(f);
    if(blob) zip.file(f.name, blob);
  }
  const out = await zip.generateAsync({ type: 'blob' });
  const a = document.createElement('a');
  a.href = URL.createObjectURL(out); a.download = 'ahoos-studio-files.zip'; a.click();
  URL.revokeObjectURL(a.href);
};

/* ---- attachments -----------------------------------------------------------
   The model reads text, so a text-ish file is inlined into the prompt. Anything
   else is stored and referenced by name: sending bytes it cannot read would
   spend tokens to no purpose.                                                */
let attached = [];
$('#attachBtn').onclick = () => $('#file').click();
$('#file').onchange = async e => {
  for(const f of e.target.files){
    const textish = /^text\/|json|xml|javascript|csv/.test(f.type) || /\.(md|txt|py|js|ts|css|html|json|toml|yml|yaml|sql|php|sh)$/i.test(f.name);
    attached.push({ name: f.name, size: f.size, text: textish ? await f.text() : null });
    if(!textish) await storeFile(f.name, await f.text().catch(() => ''), f.type);
  }
  paintAttachments();
  e.target.value = '';
};
function paintAttachments(){
  const box = $('#attached');
  box.innerHTML = attached.map((a, i) =>
    `<span class="chip-file">${esc(a.name)}<button data-i="${i}">×</button></span>`).join('');
  box.querySelectorAll('button').forEach(b =>
    b.onclick = () => { attached.splice(+b.dataset.i, 1); paintAttachments(); });
}

/* ---- composer ------------------------------------------------------------- */
const input = $('#input');
function gearNode(id){
  const n = document.createElement('span');
  n.className = 'gear-tok'; n.contentEditable = 'false';
  n.dataset.gear = id; n.dataset.label = '@' + id + ' — ' + GEARS[id];
  n.innerHTML = '<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M12 2.5 9.8 4.7a3 3 0 0 0 0 4.2l.1.1V20a2 2 0 0 0 4 0V9l.1-.1a3 3 0 0 0 0-4.2z"/></svg>';
  return n;
}
function insertGear(id){
  input.focus();
  const sel = window.getSelection();
  let range;
  if(sel.rangeCount && input.contains(sel.anchorNode)) range = sel.getRangeAt(0);
  else { range = document.createRange(); range.selectNodeContents(input); range.collapse(false); }
  range.deleteContents();
  const node = gearNode(id), space = document.createTextNode(' ');
  range.insertNode(node); node.after(space);
  range.setStartAfter(space); range.collapse(true);
  sel.removeAllRanges(); sel.addRange(range);
}
$$('#gearPop [data-gear]').forEach(b =>
  b.onclick = () => { insertGear(b.dataset.gear); $('#gearPop').classList.remove('on'); });

function readInput(){
  let out = '';
  input.childNodes.forEach(function walk(n){
    if(n.nodeType === 3) out += n.nodeValue;
    else if(n.dataset && n.dataset.gear) out += '@' + n.dataset.gear;
    else if(n.tagName === 'BR') out += '\n';
    else if(n.childNodes) n.childNodes.forEach(walk);
  });
  return out.trim();
}
input.addEventListener('keydown', e => {
  if(e.key === 'Enter' && !e.shiftKey){ e.preventDefault(); submit(); }
});
input.addEventListener('paste', e => {
  e.preventDefault();
  document.execCommand('insertText', false, (e.clipboardData || window.clipboardData).getData('text'));
});

function wirePop(btn, pop){
  $(btn).onclick = e => {
    e.stopPropagation();
    const open = $(pop).classList.contains('on');
    $$('.pop').forEach(p => p.classList.remove('on'));
    if(!open) $(pop).classList.add('on');
  };
}
wirePop('#modelBtn','#modelPop'); wirePop('#gearBtn','#gearPop'); wirePop('#chipBtn','#chipPop');
wirePop('#thinkBtn','#thinkPop');

/* ---- effort and team mode --------------------------------------------------
   Two request options the backend already understood and nothing could reach.
   Both are per-person and sticky: a person who works at xhigh works at xhigh
   tomorrow too, and having to set it every message is how a setting becomes a
   thing nobody uses.

   localStorage and not the session, because this is a preference rather than
   state the server needs. It is read defensively -- a private window or a
   browser set to block site data throws on access rather than returning null,
   and a settings read must never be what stops the page loading. */
const EFFORTS = { '': 'Medium', low: 'Low', medium: 'Medium',
                  high: 'High', xhigh: 'xHigh', max: 'Max' };
let effort = '', solo = false;

try {
  const e = localStorage.getItem('nimbus.effort');
  if (e !== null && e in EFFORTS) effort = e;
} catch (err) { /* blocked storage: the defaults above stand */ }

function remember(key, value){
  try { localStorage.setItem(key, value); } catch (err) {}
}

function paintEffort(){
  $('#thinkLabel').textContent = EFFORTS[effort] || 'Medium';
  // The label reads "Medium" for the default too, because that is what the
  // models mostly do -- but only an explicit choice is marked as chosen.
  $$('#thinkPop [data-effort]').forEach(b =>
    b.classList.toggle('on', b.dataset.effort === effort));
}

function paintSolo(){
  const b = $('#soloBtn');
  b.classList.toggle('lit', solo);
  b.setAttribute('aria-pressed', solo ? 'true' : 'false');
  b.title = 'One model answers. Team mode needs the online studio.';
}

$$('#thinkPop [data-effort]').forEach(b => b.onclick = () => {
  effort = b.dataset.effort;
  remember('nimbus.effort', effort);
  paintEffort();
  $('#thinkPop').classList.remove('on');
});

/* Offline, one model answers every message: there is no manager and no
   team to delegate to on this computer. Always on, and not a switch. */
solo = true;

paintEffort();
paintSolo();
document.addEventListener('click', () => $$('.pop').forEach(p => p.classList.remove('on')));
$$('.pop').forEach(p => p.onclick = e => e.stopPropagation());

/* ---- rendering ------------------------------------------------------------ */
const KW = /\b(function|const|let|var|return|if|else|for|while|class|import|from|export|def|async|await|new|try|catch|public|private|echo|require|use|null|true|false)\b/g;
const highlight = code => esc(code)
  .replace(/(&quot;[^&]*?&quot;|&#39;[^&]*?&#39;|`[^`]*?`)/g, '<span class="s">$1</span>')
  .replace(/(\/\/[^\n]*|#[^\n]*|\/\*[\s\S]*?\*\/)/g, '<span class="c">$1</span>')
  .replace(KW, '<span class="k">$1</span>')
  .replace(/\b(\d+(?:\.\d+)?)\b/g, '<span class="n">$1</span>');

window.__code = {};
function codeBlock(lang, code){
  const id = 'c' + Math.random().toString(36).slice(2, 9);
  window.__code[id] = code;
  return `<div class="code"><div class="code-head"><span>${esc(lang || 'text')}</span><span class="sp"></span>`
       + `<button onclick="copyCode('${id}',this)">Copy</button>`
       + `<button onclick="dlCode('${id}','${esc(lang || 'txt')}')">Download</button></div>`
       + `<pre><code>${highlight(code)}</code></pre></div>`;
}
window.copyCode = (id, btn) => navigator.clipboard.writeText(window.__code[id])
  .then(() => { btn.textContent = 'Copied'; setTimeout(() => btn.textContent = 'Copy', 1400); });
window.dlCode = (id, lang) => {
  const a = document.createElement('a');
  a.href = URL.createObjectURL(new Blob([window.__code[id]], { type: 'text/plain' }));
  a.download = 'nimbus.' + (EXT[lang] || 'txt'); a.click(); URL.revokeObjectURL(a.href);
};
function render(text){
  const parts = String(text).split(/```(\w*)\n?([\s\S]*?)```/g);
  let html = '';
  for(let i = 0; i < parts.length; i++){
    if(i % 3 === 0){
      html += esc(parts[i])
        .replace(/!\[([^\]]*)\]\(([^)]+)\)/g, '<img src="$2" alt="$1" style="max-width:100%;border-radius:9px;margin:8px 0">')
        .replace(/`([^`\n]+)`/g, '<code>$1</code>')
        .replace(/\*\*([^*\n]+)\*\*/g, '<strong>$1</strong>')
        .replace(/\n/g, '<br>');
    } else if(i % 3 === 1){ const lang = parts[i]; html += codeBlock(lang, parts[++i]); }
  }
  return html;
}
function metaHtml(meta){
  if(!meta) return '';
  const bits = [];
  if((meta.gears || []).length) bits.push(meta.gears.map(g => `<span class="tag">${esc(g)}</span>`).join(''));
  bits.push((meta.routed || []).length
    ? 'routed to ' + meta.routed.map(m => `<code>${esc(m)}</code>`).join(', ')
    : 'answered directly by the manager');
  return `<div class="meta">${bits.join(' · ')}</div>`;
}
/** addTurn for model output: provider URLs are swapped before anything paints. */
async function addModelTurn(text, cls, meta){
  return addTurn(await ownDomain(text), cls, meta);
}

function addTurn(text, cls, meta){
  const t = document.createElement('div');
  t.className = 'turn ' + cls + (RTL.test(text) ? ' rtl' : '');
  t.innerHTML = `<div class="bubble">${render(text)}${meta || ''}</div>`;
  $('#stream').appendChild(t);
  paintBlank();
  $('#log').scrollTop = $('#log').scrollHeight;
  return t;
}
function stageEl(kind, label, detail){
  const d = document.createElement('div');
  d.className = 'stage';
  const art = kind === 'think'
    ? '<div class="orbit"><div class="ring"></div><div class="cl"><svg width="13" height="13" viewBox="124 124 752 752" aria-hidden="true"><circle cx="500" cy="500" r="330" fill="none" stroke="currentColor" stroke-width="42" mask="url(#ahoosGap)"/><path d="M 505 292 L 692 700 L 732 700 L 732 722 L 548 722 L 548 700 L 588 700 L 505 388 L 356 700 L 396 700 L 396 722 L 278 722 L 278 700 L 318 700 Z" fill="currentColor"/><g transform="rotate(-20 500 500)" mask="url(#ahoosDip)"><ellipse cx="500" cy="500" rx="372" ry="152" fill="none" stroke="currentColor" stroke-width="42"/></g></svg></div></div>'
    : kind === 'plan'
    ? '<svg class="scroll" width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7"><path d="M8 3H5a2 2 0 0 0 0 4h3M8 3v14a4 4 0 0 0 4 4h5a2 2 0 0 0 2-2V7M8 3h9a2 2 0 0 1 2 2v2M11 21a4 4 0 0 1-3-3.9"/></svg>'
    : '<svg class="lamp" width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7"><path d="M9 18h6M10 22h4M12 2a7 7 0 0 0-4 12.7V17h8v-2.3A7 7 0 0 0 12 2z"/></svg>';
  d.innerHTML = art + '<div style="flex:1"><span>' + esc(label) + '</span>'
              + (detail ? '<div class="brief">' + esc(detail) + '</div>' : '') + '</div>';
  return d;
}

/* ---- one exchange --------------------------------------------------------- */
async function submit(){
  let prompt = readInput();
  if(!prompt || live) return;

  if(attached.length){
    const inline = attached.filter(a => a.text !== null)
      .map(a => `\n\n--- ${a.name} ---\n${a.text.slice(0, 20000)}`).join('');
    const named = attached.filter(a => a.text === null).map(a => a.name);
    if(named.length) prompt += `\n\n(attached, not readable as text: ${named.join(', ')})`;
    prompt += inline;
    attached = []; paintAttachments();
  }

  // Wait for the host session before the first stream; afterwards it is
  // already resolved and costs nothing.
  if(hostSession) await hostSession;

  if(!chatId) await newChat(prompt);

  addTurn(prompt, 'user');
  saveMessage('user', prompt, {});
  input.innerHTML = '';
  $('#send').disabled = true;

  const turn = document.createElement('div');
  turn.className = 'turn bot';
  const stages = document.createElement('div');
  turn.appendChild(stages);
  $('#stream').appendChild(turn);
  paintBlank();

  const thinking = stageEl('think', 'Planning');
  thinking.querySelector('span').innerHTML = 'Planning<span class="dots"></span>';
  stages.appendChild(thinking);
  $('#log').scrollTop = $('#log').scrollHeight;

  const gearsSeen = [], routed = [];
  let url = 'stream.php?prompt=' + encodeURIComponent(prompt);
  if (effort) url += '&effort=' + encodeURIComponent(effort);
  if (solo)   url += '&solo=1';
  live = new EventSource(url);
  const on = (n, fn) => live.addEventListener(n, e => { try{ fn(JSON.parse(e.data || '{}')); }catch(err){} });

  on('gears:parsed', d => (d.gears || []).forEach(g => gearsSeen.push(g)));

  on('plan:done', d => {
    thinking.classList.add('done');
    thinking.querySelector('span').textContent = 'Planned';
    stages.appendChild(stageEl('plan',
      d.handle_directly ? 'Answering directly' : 'Delegating to ' + (d.delegations || []).length + ' specialist(s)',
      d.rationale || ''));
    $('#log').scrollTop = $('#log').scrollHeight;
  });

  on('delegate:start', d => {
    const s = stageEl('think', d.model, d.brief || '');
    s.querySelector('span').innerHTML = esc(d.model) + ' working<span class="dots"></span>';
    s.dataset.model = d.model;
    stages.appendChild(s);
    $('#log').scrollTop = $('#log').scrollHeight;
  });

  on('delegate:done', d => {
    const s = stages.querySelector('[data-model="' + d.model + '"]');
    if(s){
      s.classList.add('done');
      // The payload carries `error`, not `ok`. Reading a field the API never
      // sends made every answer claim the manager had handled it directly, even
      // when a specialist plainly had.
      s.querySelector('span').textContent = d.model + (d.error ? ' failed' : ' finished');
    }
    if(!d.error) routed.push(d.model);
  });

  on('synthesis:start', () => {
    const s = stageEl('think', 'Assembling the answer');
    s.querySelector('span').innerHTML = 'Assembling the answer<span class="dots"></span>';
    stages.appendChild(s);
  });

  on('done', async d => {
    finish(); stages.remove();
    const meta = { gears: gearsSeen, routed };
    const shown = await ownDomain(d.answer || '');
    turn.className = 'turn bot' + (RTL.test(shown) ? ' rtl' : '');
    turn.innerHTML = `<div class="bubble">${render(shown)}${metaHtml(meta)}</div>`;
    $('#log').scrollTop = $('#log').scrollHeight;
    saveMessage('assistant', d.answer || '', meta);
    captureFiles(d.answer || '');
  });

  on('error', d => {
    finish(); stages.remove();
    turn.className = 'turn err';
    turn.innerHTML = `<div class="bubble">${esc(d.detail || 'Something went wrong.')}</div>`;
  });

  on('closed', () => finish());

  live.onerror = () => {
    if(!live) return;
    finish();
    if(stages.isConnected){
      stages.remove();
      turn.className = 'turn err';
      turn.innerHTML = '<div class="bubble">The connection dropped before the answer arrived.</div>';
    }
  };

  function finish(){
    if(live){ live.close(); live = null; }
    $('#send').disabled = false;
    input.focus();
  }
}
$('#send').onclick = submit;

/* ---- chips ----------------------------------------------------------------
   A chip is emphasis: text appended after a model's own prompt, refining a
   model that already knows its job rather than redefining it. Which ones are
   on is a per-browser choice, so it lives in localStorage; the content lives
   in Supabase, shared or private as its owner chose.                          */
const MAX_ACTIVE = 5;
let chipRows = [];

function activeIds(){
  try{ return JSON.parse(localStorage.getItem('chips') || '[]'); }catch(e){ return []; }
}
function setActive(ids){
  try{ localStorage.setItem('chips', JSON.stringify(ids)); }catch(e){}
  paintChips();
  pushChips();
}

/* The chips that will actually reach the model: switched on, still in the
   library, and carrying something. The count on the composer button used to
   come from localStorage instead, which also holds ids of chips that have since
   been deleted or made private -- so it could read "3 chips active" while one
   chip was sent, and there was nothing on screen to explain the gap. */
function sendable(){
  const on = activeIds();
  return chipRows
    .filter(c => !c.builtin && on.includes(c.id) && (c.content || '').trim())
    .slice(0, MAX_ACTIVE);
}

/** Send the active set to the host, where stream.php reads it back. */
async function pushChips(){
  const chips = sendable().map(c => ({name: c.name, content: c.content}));
  try{
    await fetch('chips.php', {
      method : 'POST',
      headers: {'Content-Type':'application/json'},
      body   : JSON.stringify({chips})
    });
  }catch(e){ console.error('[chips] could not send', e); }
}

async function loadChips(){
  const { data, error } = await sb.from('chips')
    .select('id,slug,name,summary,licence_holder,visibility,builtin,content,owner_id')
    .order('builtin', { ascending: false }).order('created_at');
  if(error){ $('#library').innerHTML = '<div class="empty">' + esc(error.message) + '</div>'; return; }
  chipRows = data || [];

  // Ids the library no longer contains have to go, or they stay forever:
  // invisible, unremovable, and still counting against the five-chip cap, until
  // nothing new can be switched on and nothing on screen says why. A chip
  // leaves the library when its owner deletes it or makes it private again --
  // and every built-in id written by the version of this page that wrongly
  // offered them as switches leaves the same way.
  const real = new Set(chipRows.filter(c => !c.builtin).map(c => c.id));
  const kept = activeIds().filter(id => real.has(id));
  if(kept.length !== activeIds().length){
    setActive(kept);        // paints and pushes on its way through
    return;
  }
  paintChips();
  pushChips();
}

const CHIP_ICON = '<svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"><rect x="6" y="6" width="12" height="12" rx="2"/><path d="M9 2v3M15 2v3M9 19v3M15 19v3M2 9h3M2 15h3M19 9h3M19 15h3"/></svg>';
const TICK = '<svg class="check" width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4"><path d="M20 6 9 17l-5-5"/></svg>';

/* A built-in chip is not a switch, and drawing it as one was the bug.
 *
 * It lives on disk beside the runtime, which attaches it to the models its
 * manifest names -- rtl-web to nimbus-frontend-rza-1.0 -- and applies it to
 * every request. Nothing the studio posts can turn that on or off. The row in
 * Supabase is description only: the schema forbids it any content at all
 * (chips_builtin_has_no_content), so pushChips dropped it before sending, and
 * chips.php would have dropped it again on arrival.
 *
 * So the one public chip that ships with the product was a control that lit up,
 * consumed one of the five slots, and did the same nothing in either position.
 * It is shown now as what it is: on, and not yours to change.
 */
function paintChips(){
  const on = activeIds();

  const lib = $('#library');
  lib.innerHTML = '';
  if(!chipRows.length) lib.innerHTML = '<div class="empty">No chips yet.</div>';
  chipRows.forEach(c => {
    const b = document.createElement('button');
    b.className = 'row' + (c.builtin ? ' fixed on' : on.includes(c.id) ? ' on' : '');
    b.title = (c.summary || '')
      + (c.licence_holder ? '\nLicence: ' + c.licence_holder : '')
      + (c.builtin ? '\nAlways on. This chip ships with the model it belongs to.' : '');
    b.innerHTML = CHIP_ICON + '<span>' + esc(c.name) + '</span>'
      + (c.builtin ? '<span class="hintlet">always on</span>'
                   : c.visibility === 'public' ? '<span class="hintlet">public</span>' : '');
    if(c.builtin) b.disabled = true;
    else b.onclick = () => toggleChip(c.id);
    lib.appendChild(b);
  });

  // The composer popover is for choosing, so it lists only what can be chosen.
  // Built-ins stay in the Library, where they are described rather than offered.
  const pop = $('#chipList');
  if(pop){
    pop.innerHTML = '';
    chipRows.filter(c => !c.builtin).forEach(c => {
      const b = document.createElement('button');
      b.innerHTML = esc(c.name) + (on.includes(c.id) ? TICK : '');
      b.onclick = () => toggleChip(c.id);
      pop.appendChild(b);
    });
  }

  const n = sendable().length;
  $('#chipBtn').title = n ? n + ' chip' + (n > 1 ? 's' : '') + ' active' : 'Skill chips';
  $('#chipBtn').classList.toggle('lit', n > 0);
}

function toggleChip(id){
  // Nothing draws a built-in as clickable any more, but the active set is
  // written to localStorage and read back on the next load: one id that slips
  // in here is permanent until something removes it.
  if(chipRows.some(c => c.id === id && c.builtin)) return;
  const on = activeIds();
  const i = on.indexOf(id);
  if(i >= 0) on.splice(i, 1);
  else {
    if(on.length >= MAX_ACTIVE){ alert('At most ' + MAX_ACTIVE + ' chips can be active at once.'); return; }
    on.push(id);
  }
  setActive(on);
}

/* ---- creating and importing ------------------------------------------------ */

function slugify(name){
  return name.toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/^-|-$/g, '').slice(0, 40)
       || 'chip-' + Math.random().toString(36).slice(2, 8);
}

async function saveChip(spec){
  const { error } = await sb.from('chips').insert({
    owner_id: user.id,
    slug: slugify(spec.name) + '-' + Math.random().toString(36).slice(2, 6),
    name: spec.name,
    summary: spec.summary || '',
    content: spec.content,
    licence_holder: spec.holder || user.user_metadata?.name || user.email || '',
    visibility: spec.visibility,
    builtin: false
  });
  if(error){ alert(error.message); return false; }
  await loadChips();
  return true;
}

$('#chipNew').onclick = e => { e.preventDefault(); $('#chipPop').classList.remove('on'); openChipForm(); };

function openChipForm(){
  $('#chipForm').classList.add('on');
  $('#cfName').value = ''; $('#cfSummary').value = ''; $('#cfContent').value = '';
  $('#cfPublic').checked = false;
  $('#cfName').focus();
}
$('#cfCancel').onclick = () => $('#chipForm').classList.remove('on');
$('#chipForm').onclick = e => { if(e.target.id === 'chipForm') $('#chipForm').classList.remove('on'); };

$('#cfSave').onclick = async () => {
  const name = $('#cfName').value.trim();
  const content = $('#cfContent').value.trim();
  if(!name || !content){ alert('A chip needs a name and some content.'); return; }
  if(content.length > 8000){ alert('Content is limited to 8000 characters.'); return; }
  const ok = await saveChip({
    name,
    summary: $('#cfSummary').value.trim(),
    content,
    visibility: $('#cfPublic').checked ? 'public' : 'private'
  });
  if(ok) $('#chipForm').classList.remove('on');
};

/* A .zip is read here rather than installed. The runtime's importer is a trust
   boundary -- it refuses a chip whose licence is missing or does not name its
   declared holder -- and none of that can run in a browser. So what lands is
   the Markdown, owned by whoever imported it, and private until they say
   otherwise. It is not presented as a package anyone vouched for. */
$('#chipUp').onclick = e => { e.preventDefault(); $('#chipPop').classList.remove('on'); $('#chipZip').click(); };

$('#chipZip').onchange = async e => {
  const file = e.target.files[0];
  e.target.value = '';
  if(!file) return;
  try{
    const zip = await JSZip.loadAsync(file);
    let name = file.name.replace(/\.(chip\.)?zip$/i, '');
    let summary = '', holder = '';
    const parts = [];

    for(const path of Object.keys(zip.files)){
      if(zip.files[path].dir) continue;
      const text = await zip.files[path].async('string');
      if(/chip\.toml$/i.test(path)){
        // Enough of the manifest to label it. A real parse belongs in the
        // runtime, which is where a chip is actually validated.
        const m1 = text.match(/^\s*name\s*=\s*"([^"]+)"/m);
        const m2 = text.match(/^\s*summary\s*=\s*"([^"]+)"/m);
        const m3 = text.match(/^\s*holder\s*=\s*"([^"]+)"/m);
        if(m1) name = m1[1];
        if(m2) summary = m2[1];
        if(m3) holder = m3[1];
      } else if(/\.md$/i.test(path) && !/licen[cs]e/i.test(path)){
        parts.push(text.trim());
      }
    }

    const content = parts.join('\n\n').slice(0, 8000);
    if(!content){ alert('No Markdown content found in that zip.'); return; }
    if(await saveChip({name, summary, content, visibility: 'private', holder}))
      alert('Imported "' + name + '". It is private until you change that.');
  }catch(err){
    alert('That file could not be read as a chip zip: ' + err.message);
  }
};
