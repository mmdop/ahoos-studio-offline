/* ---------------------------------------------------------------------------
   Markdown, and code that is readable at a glance -- with nothing from a CDN.

   The studio runs offline, so the renderer is here: the subset of Markdown
   models actually write (headings, emphasis, lists, quotes, tables, links,
   fenced code) and a small highlighter for the languages they write code in.
   It renders text that is still arriving, so an unclosed fence is a code block
   that has not ended yet, not a paragraph full of backticks.

   Everything is escaped first and built up from there; no model output reaches
   innerHTML unescaped.
   --------------------------------------------------------------------------- */
(() => {
  'use strict';

  const esc = s => String(s).replace(/[&<>"']/g, c =>
    ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

  /* ---- the highlighter --------------------------------------------------- */

  const KW = {
    py: 'and as assert async await break class continue def del elif else except False finally for from global if import in is lambda None nonlocal not or pass raise return True try while with yield match case self print len range',
    js: 'async await break case catch class const continue debugger default delete do else export extends false finally for from function if import in instanceof let new null of return static super switch this throw true try typeof undefined var void while with yield interface type enum implements private public protected readonly as',
    go: 'break case chan const continue default defer else fallthrough for func go goto if import interface map package range return select struct switch type var nil true false',
    rs: 'as async await break const continue crate else enum extern false fn for if impl in let loop match mod move mut pub ref return self Self static struct super trait true type unsafe use where while',
    c: 'auto break case char const continue default do double else enum extern float for goto if int long register return short signed sizeof static struct switch typedef union unsigned void volatile while class public private protected virtual override new delete namespace using template typename bool true false null nullptr this string var foreach in out ref static readonly',
    sql: 'select from where insert into values update set delete create table drop alter add primary key foreign references not null unique index join left right inner outer on group by order having limit offset as and or in is like between distinct count sum avg min max case when then else end',
    sh: 'if then else elif fi for while do done case esac function in return export local echo cd exit set unset source',
    php: 'abstract and array as break callable case catch class clone const continue declare default do echo else elseif empty enddeclare endfor endforeach endif endswitch endwhile extends final finally fn for foreach function global goto if implements include instanceof insteadof interface isset list match namespace new or print private protected public readonly require return static switch throw trait try unset use var while yield true false null',
  };
  const FAMILY = {
    python: 'py', py: 'py', javascript: 'js', js: 'js', jsx: 'js', ts: 'js', tsx: 'js', typescript: 'js',
    json: 'js', go: 'go', golang: 'go', rust: 'rs', rs: 'rs', c: 'c', h: 'c', cpp: 'c', 'c++': 'c',
    cs: 'c', csharp: 'c', java: 'c', kotlin: 'c', swift: 'c', dart: 'c', sql: 'sql', bash: 'sh',
    sh: 'sh', shell: 'sh', zsh: 'sh', powershell: 'sh', ps1: 'sh', bat: 'sh', cmd: 'sh', php: 'php',
    yaml: 'sh', yml: 'sh', toml: 'sh', ini: 'sh', dockerfile: 'sh', docker: 'sh', ruby: 'sh', rb: 'sh',
  };
  const kwSets = {};
  const words = fam => kwSets[fam] || (kwSets[fam] = new Set((KW[fam] || '').split(' ')));

  function highlight(code, lang) {
    lang = String(lang || '').toLowerCase();
    if (['html', 'xml', 'svg', 'vue', 'xhtml'].includes(lang)) return markup(code);
    if (lang === 'css' || lang === 'scss' || lang === 'less') return css(code);
    if (lang === 'diff' || lang === 'patch') return diffLines(code);
    const fam = FAMILY[lang] || (lang ? 'c' : '');
    if (!fam) return esc(code);
    const hash = ['py', 'sh', 'php'].includes(fam);
    const slash = !['py', 'sh', 'sql'].includes(fam);
    const dash = fam === 'sql';
    const parts = [
      fam === 'py' ? String.raw`"""[\s\S]*?(?:"""|$)|'''[\s\S]*?(?:'''|$)` : null,
      slash ? String.raw`\/\*[\s\S]*?(?:\*\/|$)|\/\/[^\n]*` : null,
      hash ? String.raw`#[^\n]*` : null,
      dash ? String.raw`--[^\n]*` : null,
      String.raw`"(?:\\.|[^"\\\n])*"?|'(?:\\.|[^'\\\n])*'?` + (fam === 'js' || fam === 'go' ? '|`(?:\\\\.|[^`\\\\])*`?' : ''),
      String.raw`\b(?:0x[\da-fA-F]+|\d[\d_]*(?:\.\d+)?(?:e[+-]?\d+)?)\b`,
      String.raw`[A-Za-z_$][\w$]*`,
    ].filter(Boolean);
    const re = new RegExp(parts.map(p => '(' + p + ')').join('|'), 'g');
    const set = words(fam);
    let out = '', last = 0, m;
    while ((m = re.exec(code))) {
      out += esc(code.slice(last, m.index));
      const tok = m[0];
      let cls = '';
      if (/^(\/\/|\/\*|#|--)/.test(tok) && !(fam === 'c' && tok[0] === '#' && /^#(include|define|if|pragma)/.test(tok))) cls = 'c';
      else if (/^("""|''')/.test(tok) || /^["'`]/.test(tok)) cls = 's';
      else if (/^\d|^0x/.test(tok)) cls = 'n';
      else if (set.has(fam === 'sql' ? tok.toLowerCase() : tok)) cls = 'k';
      else if (code[re.lastIndex] === '(') cls = 'f';
      else if (/^[A-Z][a-z]/.test(tok) && fam !== 'sql') cls = 't';
      out += cls ? `<span class="t-${cls}">${esc(tok)}</span>` : esc(tok);
      last = re.lastIndex;
    }
    return out + esc(code.slice(last));
  }

  function markup(code) {
    return esc(code)
      .replace(/(&lt;!--[\s\S]*?(?:--&gt;|$))/g, '<span class="t-c">$1</span>')
      .replace(/(&lt;\/?)([\w:-]+)([\s\S]*?)(\/?&gt;)/g, (all, open, tag, attrs, close) =>
        `${open}<span class="t-k">${tag}</span>${attrs.replace(/([\w:-]+)(=)(&quot;[^&]*?&quot;|&#39;[^&]*?&#39;)/g,
          '<span class="t-f">$1</span>$2<span class="t-s">$3</span>')}${close}`);
  }

  function css(code) {
    return esc(code)
      .replace(/(\/\*[\s\S]*?(?:\*\/|$))/g, '<span class="t-c">$1</span>')
      .replace(/([\w-]+)(\s*:\s*)([^;{}\n]+)/g, '<span class="t-f">$1</span>$2<span class="t-s">$3</span>')
      .replace(/(^|\n)([^\n{]+)(\{)/g, '$1<span class="t-k">$2</span>$3');
  }

  function diffLines(code) {
    return code.split('\n').map(line => {
      const cls = line.startsWith('+') ? 'd-add' : line.startsWith('-') ? 'd-del' : line.startsWith('@@') ? 'd-hunk' : '';
      return cls ? `<span class="${cls}">${esc(line)}</span>` : esc(line);
    }).join('\n');
  }

  /* ---- inline --------------------------------------------------------------- */

  function inline(text) {
    const codes = [];
    let s = String(text).replace(/`([^`\n]+)`/g, (_, c) => { codes.push(c); return `\u0000${codes.length - 1}\u0000`; });
    s = esc(s);
    s = s.replace(/\[([^\]]+)\]\((https?:\/\/[^\s)]+)\)/g, '<a href="$2" rel="noreferrer">$1</a>');
    s = s.replace(/(^|[\s(])(https?:\/\/[^\s<)]+[^\s<).,;:!?'"])/g, '$1<a href="$2" rel="noreferrer">$2</a>');
    s = s.replace(/\*\*([^*\n]+)\*\*|__([^_\n]+)__/g, (_, a, b) => `<strong>${a || b}</strong>`);
    s = s.replace(/(^|[^*\w])\*([^*\n]+)\*(?!\w)/g, '$1<em>$2</em>');
    s = s.replace(/(^|[^_\w])_([^_\n]+)_(?!\w)/g, '$1<em>$2</em>');
    s = s.replace(/~~([^~\n]+)~~/g, '<del>$1</del>');
    return s.replace(/\u0000(\d+)\u0000/g, (_, i) => `<code>${esc(codes[Number(i)])}</code>`);
  }

  /* ---- blocks ----------------------------------------------------------------- */

  const FILE_HINT = /(?:^|[\s*`#>:])([\w./\\-]+\.(?:py|js|ts|tsx|jsx|mjs|cjs|json|html?|css|scss|md|txt|ya?ml|toml|ini|cfg|sh|bat|ps1|go|rs|java|kt|c|h|cpp|hpp|cs|php|rb|swift|sql|xml|svg|vue|dart|lua|r|env|gitignore|dockerfile))\b/i;

  function guessFile(before, code, lang) {
    const first = (code.split('\n')[0] || '').trim();
    const comment = first.match(/^(?:#|\/\/|--|<!--|\/\*)\s*([\w./\\-]+\.\w+)\s*(?:-->|\*\/)?$/);
    if (comment) return comment[1];
    const hint = (before || '').match(FILE_HINT);
    if (hint) return hint[1];
    if (/^dockerfile$/i.test(lang || '')) return 'Dockerfile';
    return '';
  }

  function codeBlock(code, lang, before, open) {
    const file = guessFile(before, code, lang);
    const label = esc(lang || 'text');
    return `<div class="code${open ? ' open' : ''}" data-lang="${label}"${file ? ` data-file="${esc(file)}"` : ''}>`
      + `<div class="code-head"><span class="code-lang">${label}${file ? ` · <b>${esc(file)}</b>` : ''}</span>`
      + '<span class="code-acts"><button type="button" data-act="copy"></button>'
      + '<button type="button" data-act="save"></button></span></div>'
      + `<pre dir="ltr"><code>${highlight(code, lang)}</code></pre></div>`;
  }

  const fa = () => (document.documentElement.lang || 'fa') !== 'en';
  const say = (f, e) => (fa() ? f : e);

  /* A table can be read across (a row a thing) or down (a column a thing): the bar above it turns it,
     and copies it as CSV. ":::vertical" around a table asks for it turned from the start. */
  function table(rows, vertical) {
    const cells = row => row.replace(/^\s*\|/, '').replace(/\|\s*$/, '').split('|').map(c => c.trim());
    const head = cells(rows[0]);
    const align = cells(rows[1]).map(c => (/^:-+:$/.test(c) ? 'center' : /-+:$/.test(c) ? 'end' : ''));
    const body = rows.slice(2).map(cells).map(r => head.map((_, i) => r[i] ?? ''));
    const data = esc(JSON.stringify([head, ...body]));
    return `<div class="table${vertical ? ' down' : ''}" data-rows="${data}">`
      + `<div class="table-bar"><button type="button" data-tact="turn">${vertical ? say('افقی', 'Across') : say('عمودی', 'Down')}</button>`
      + `<button type="button" data-tact="csv">${say('کپی CSV', 'Copy CSV')}</button></div>`
      + tableBody(head, body, align, vertical) + '</div>';
  }

  function tableBody(head, body, align, vertical) {
    const td = (tag, c, i) => `<${tag}${align && align[i] ? ` style="text-align:${align[i]}"` : ''} dir="auto">${inline(c)}</${tag}>`;
    if (vertical) {
      // Down: the headings make the first column, each thing a column of its own.
      return '<table><tbody>' + head.map((h, i) => '<tr>' + td('th', h) + body.map(r => td('td', r[i], i)).join('') + '</tr>').join('')
        + '</tbody></table>';
    }
    return '<table><thead><tr>' + head.map((c, i) => td('th', c, i)).join('')
      + '</tr></thead><tbody>' + body.map(r => '<tr>' + r.map((c, i) => td('td', c, i)).join('') + '</tr>').join('')
      + '</tbody></table>';
  }

  function tableAct(button) {
    const box = button.closest('.table');
    let rows;
    try { rows = JSON.parse(box.dataset.rows); } catch (e) { return; }
    if (button.dataset.tact === 'csv') {
      const cell = c => (/[",\n]/.test(c) ? '"' + c.replace(/"/g, '""') + '"' : c);
      const text = rows.map(r => r.map(c => cell(String(c).replace(/\*\*|`/g, ''))).join(',')).join('\n');
      (navigator.clipboard ? navigator.clipboard.writeText(text) : Promise.reject()).then(() => {
        const was = button.textContent;
        button.textContent = say('کپی شد', 'Copied');
        button.classList.add('done');
        setTimeout(() => { button.textContent = was; button.classList.remove('done'); }, 1400);
      }, () => {});
      return;
    }
    const down = !box.classList.contains('down');
    box.classList.toggle('down', down);
    box.querySelector('table').outerHTML = tableBody(rows[0], rows.slice(1), null, down);
    button.textContent = down ? say('افقی', 'Across') : say('عمودی', 'Down');
  }

  document.addEventListener('click', e => {
    const button = e.target.closest && e.target.closest('.table-bar button');
    if (button) tableAct(button);
  });

  /* ---- charts: a ```chart block of JSON, drawn flat --------------------------------- */

  const COLOURS = ['#2f6fed', '#e8590c', '#2b8a3e', '#ae3ec9', '#f59f00', '#0c8599', '#c92a2a', '#5c7cfa', '#868e96'];

  function numberText(n) {
    try { return new Intl.NumberFormat(fa() ? 'fa-IR' : 'en-US', { maximumFractionDigits: 2 }).format(n); } catch (e) { return String(n); }
  }

  function chartData(spec) {
    // Accepts {labels, series:[{name, data}]}, {labels, data:[...]}, {data:{label: value}}, and [[label, value], ...].
    if (Array.isArray(spec)) spec = { data: spec };
    let labels = spec.labels || spec.x || [];
    let series = spec.series || spec.datasets || null;
    if (!series) {
      let data = spec.data ?? spec.values ?? spec.y ?? [];
      if (data && !Array.isArray(data) && typeof data === 'object') { labels = Object.keys(data); data = Object.values(data); }
      if (Array.isArray(data) && data.length && Array.isArray(data[0])) { labels = data.map(d => d[0]); data = data.map(d => d[1]); }
      series = [{ name: spec.name || spec.label || '', data }];
    }
    series = series.map(s => ({ name: String(s.name ?? s.label ?? ''), data: (s.data || s.values || []).map(Number) }));
    const n = Math.max(labels.length, ...series.map(s => s.data.length));
    labels = Array.from({ length: n }, (_, i) => String(labels[i] ?? i + 1));
    return { type: String(spec.type || spec.kind || 'bar').toLowerCase(), title: spec.title || '', unit: spec.unit || '', labels, series };
  }

  function niceMax(v) {
    if (v <= 0) return 1;
    const p = Math.pow(10, Math.floor(Math.log10(v)));
    for (const m of [1, 1.2, 1.5, 2, 2.5, 3, 4, 5, 6, 8, 10]) if (m * p >= v) return m * p;
    return 10 * p;
  }

  const svgText = (x, y, s, attrs = '') => `<text x="${x}" y="${y}" ${attrs}>${esc(s)}</text>`;
  const short = (s, k) => (s.length > k ? s.slice(0, k - 1) + '…' : s);

  function chart(code) {
    let spec;
    try { spec = JSON.parse(code); } catch (e) { return ''; }
    const c = chartData(spec);
    if (!c.series.length || !c.labels.length || c.series.every(s => s.data.every(v => !isFinite(v)))) return '';
    let svg;
    if (['pie', 'donut', 'doughnut'].includes(c.type)) svg = pie(c);
    else if (['hbar', 'barh', 'horizontal'].includes(c.type)) svg = hbar(c);
    else svg = columns(c);
    const legend = (['pie', 'donut', 'doughnut'].includes(c.type)
      ? c.labels.map((l, i) => [l, COLOURS[i % COLOURS.length], c.series[0].data[i]])
      : c.series.length > 1 || c.series[0].name ? c.series.map((s, i) => [s.name, COLOURS[i % COLOURS.length]]) : []);
    const total = c.series[0].data.reduce((a, b) => a + (b > 0 ? b : 0), 0) || 1;
    return `<figure class="chart" data-spec="${esc(code)}">`
      + (c.title ? `<figcaption dir="auto">${inline(c.title)}</figcaption>` : '')
      + svg
      + (legend.length ? '<div class="chart-legend">' + legend.map(([name, colour, v]) =>
        `<span dir="auto"><i style="background:${colour}"></i>${esc(name)}${v !== undefined ? ` <b>${numberText(Math.round(v / total * 1000) / 10)}${fa() ? '٪' : '%'}</b>` : ''}</span>`).join('') + '</div>' : '')
      + '</figure>';
  }

  function columns(c) {
    const W = 640, H = 300, left = 52, right = 14, top = 14, bottom = 46;
    const line = ['line', 'area'].includes(c.type);
    const all = c.series.flatMap(s => s.data.filter(isFinite));
    const low = Math.min(0, ...all);
    const max = niceMax(Math.max(...all, 0) - low) + low;
    const y = v => top + (H - top - bottom) * (1 - (v - low) / (max - low || 1));
    const n = c.labels.length;
    const slot = (W - left - right) / n;
    let out = `<svg viewBox="0 0 ${W} ${H}" class="chart-svg" role="img" direction="ltr">`;
    for (let k = 0; k <= 4; k++) {
      const v = low + (max - low) * k / 4;
      out += `<line class="grid" x1="${left}" x2="${W - right}" y1="${y(v)}" y2="${y(v)}"/>`
        + svgText(left - 6, y(v) + 4, numberText(v), 'class="tick" text-anchor="end"');
    }
    const every = Math.ceil(n / 12);
    c.labels.forEach((l, i) => {
      if (i % every) return;
      out += svgText(left + slot * (i + .5), H - bottom + 18, short(l, Math.max(4, Math.floor(slot * every / 7))), 'class="tick" text-anchor="middle"');
    });
    if (line) {
      c.series.forEach((s, j) => {
        const colour = COLOURS[j % COLOURS.length];
        const pts = s.data.map((v, i) => isFinite(v) ? [left + slot * (i + .5), y(v)] : null).filter(Boolean);
        if (c.type === 'area' && pts.length)
          out += `<path d="M${pts[0][0]},${y(Math.max(low, 0))} L${pts.map(p => p.join(',')).join(' L')} L${pts[pts.length - 1][0]},${y(Math.max(low, 0))} Z" fill="${colour}" opacity=".14"/>`;
        out += `<polyline fill="none" stroke="${colour}" stroke-width="2.2" points="${pts.map(p => p.join(',')).join(' ')}"/>`;
        s.data.forEach((v, i) => {
          if (isFinite(v)) out += `<circle cx="${left + slot * (i + .5)}" cy="${y(v)}" r="3.4" fill="${colour}"><title>${esc(c.labels[i])}${s.name ? ' · ' + esc(s.name) : ''}: ${numberText(v)}${esc(c.unit ? ' ' + c.unit : '')}</title></circle>`;
        });
      });
    } else {
      const k = c.series.length;
      const bar = Math.min(42, slot * .76 / k);
      c.series.forEach((s, j) => s.data.forEach((v, i) => {
        if (!isFinite(v)) return;
        const x = left + slot * (i + .5) - bar * k / 2 + bar * j;
        const y0 = y(Math.max(0, low)), y1 = y(v);
        out += `<rect x="${x}" y="${Math.min(y0, y1)}" width="${Math.max(1, bar - 2)}" height="${Math.max(1, Math.abs(y1 - y0))}" rx="2" fill="${COLOURS[j % COLOURS.length]}">`
          + `<title>${esc(c.labels[i])}${s.name ? ' · ' + esc(s.name) : ''}: ${numberText(v)}${esc(c.unit ? ' ' + c.unit : '')}</title></rect>`;
        if (k === 1 && n <= 12) out += svgText(x + (bar - 2) / 2, Math.min(y0, y1) - 5, numberText(v), 'class="val" text-anchor="middle"');
      }));
    }
    out += `<line class="axis" x1="${left}" x2="${W - right}" y1="${y(Math.max(0, low))}" y2="${y(Math.max(0, low))}"/>`;
    return out + '</svg>';
  }

  function hbar(c) {
    const n = c.labels.length, k = c.series.length;
    const row = Math.max(22, 10 + 14 * k), W = 640, left = 150, right = 60, top = 6;
    const H = top * 2 + row * n;
    const max = niceMax(Math.max(...c.series.flatMap(s => s.data.filter(isFinite)), 0));
    let out = `<svg viewBox="0 0 ${W} ${H}" class="chart-svg" role="img" direction="ltr">`;
    c.labels.forEach((l, i) => {
      const y0 = top + row * i;
      out += svgText(left - 8, y0 + row / 2 + 4, short(l, 22), 'class="tick" text-anchor="end"');
      c.series.forEach((s, j) => {
        const v = s.data[i];
        if (!isFinite(v)) return;
        const h = (row - 8) / k, w = Math.max(1, (W - left - right) * Math.max(0, v) / max);
        out += `<rect x="${left}" y="${y0 + 4 + h * j}" width="${w}" height="${Math.max(2, h - 2)}" rx="2" fill="${COLOURS[j % COLOURS.length]}">`
          + `<title>${esc(l)}${s.name ? ' · ' + esc(s.name) : ''}: ${numberText(v)}</title></rect>`
          + svgText(left + w + 5, y0 + 4 + h * j + h / 2 + 3, numberText(v) + (c.unit ? ' ' + c.unit : ''), 'class="val"');
      });
    });
    return out + `<line class="axis" x1="${left}" x2="${left}" y1="${top}" y2="${H - top}"/></svg>`;
  }

  function pie(c) {
    const data = c.series[0].data.map(v => (isFinite(v) && v > 0 ? v : 0));
    const total = data.reduce((a, b) => a + b, 0) || 1;
    const R = 110, r = c.type === 'pie' ? 0 : 62, cx = 320, cy = 125;
    let a = -Math.PI / 2, out = `<svg viewBox="0 0 640 250" class="chart-svg pie" role="img" direction="ltr">`;
    const pt = (rad, ang) => `${cx + rad * Math.cos(ang)},${cy + rad * Math.sin(ang)}`;
    data.forEach((v, i) => {
      if (!v) return;
      const b = a + v / total * Math.PI * 2 - (v === total ? 1e-4 : 0);
      const big = b - a > Math.PI ? 1 : 0;
      const d = r ? `M${pt(R, a)} A${R},${R} 0 ${big} 1 ${pt(R, b)} L${pt(r, b)} A${r},${r} 0 ${big} 0 ${pt(r, a)} Z`
        : `M${cx},${cy} L${pt(R, a)} A${R},${R} 0 ${big} 1 ${pt(R, b)} Z`;
      out += `<path d="${d}" fill="${COLOURS[i % COLOURS.length]}" stroke="var(--panel)" stroke-width="2"><title>${esc(c.labels[i])}: ${numberText(v)}${esc(c.unit ? ' ' + c.unit : '')}</title></path>`;
      a = b;
    });
    return out + '</svg>';
  }

  /* ---- parts of a message: :::note, :::tip, :::warning, :::danger, :::success, :::section, :::details,
     :::steps, :::vertical -- each a block with a title, around Markdown of its own. */

  const PART = { note: ['i', 'یادداشت', 'Note'], info: ['i', 'یادداشت', 'Note'], tip: ['✓', 'نکته', 'Tip'],
    success: ['✓', 'انجام شد', 'Done'], warning: ['!', 'هشدار', 'Warning'], caution: ['!', 'هشدار', 'Warning'],
    danger: ['×', 'خطر', 'Danger'], important: ['!', 'مهم', 'Important'] };

  function part(kind, title, inner) {
    kind = kind.toLowerCase();
    if (kind === 'vertical' || kind === 'down') return render(inner, { vertical: true });
    if (kind === 'details' || kind === 'more')
      return `<details class="part-more"><summary dir="auto">${inline(title || say('بیشتر', 'More'))}</summary>${render(inner)}</details>`;
    if (kind === 'section' || kind === 'part')
      return `<section class="part">${title ? `<h4 class="part-title" dir="auto">${inline(title)}</h4>` : ''}${render(inner)}</section>`;
    if (kind === 'steps') return `<div class="part-steps">${render(inner)}</div>`;
    const known = PART[kind] || PART.note;
    return `<div class="callout ${PART[kind] ? kind : 'note'}"><div class="callout-title" dir="auto"><i>${known[0]}</i>`
      + `${inline(title || say(known[1], known[2]))}</div>${render(inner)}</div>`;
  }

  function list(lines) {
    // Indentation decides nesting: a deeper line opens a list inside the item before it.
    const out = [];
    const stack = [];
    for (const line of lines) {
      const m = line.match(/^(\s*)([-*+]|\d+[.)])\s+(.*)$/);
      if (!m) {
        if (out.length) out[out.length - 1] = out[out.length - 1].replace(/<\/li>$/, ' ' + inline(line.trim()) + '</li>');
        continue;
      }
      const depth = Math.floor(m[1].replace(/\t/g, '    ').length / 2);
      const kind = /\d/.test(m[2]) ? 'ol' : 'ul';
      while (stack.length && stack[stack.length - 1].depth > depth) out.push(`</${stack.pop().kind}>`);
      if (!stack.length || stack[stack.length - 1].depth < depth) {
        const start = kind === 'ol' && parseInt(m[2], 10) !== 1 ? ` start="${parseInt(m[2], 10)}"` : '';
        stack.push({ depth, kind });
        out.push(`<${kind}${start}>`);
      }
      const task = m[3].match(/^\[([ xX])\]\s+(.*)$/);
      out.push(task
        ? `<li class="task" dir="auto"><span class="box${task[1] !== ' ' ? ' on' : ''}"></span>${inline(task[2])}</li>`
        : `<li dir="auto">${inline(m[3])}</li>`);
    }
    while (stack.length) out.push(`</${stack.pop().kind}>`);
    return out.join('');
  }

  function render(src, opts = {}) {
    const lines = String(src || '').replace(/\r\n?/g, '\n').split('\n');
    const out = [];
    let i = 0;
    let para = [];
    const flush = () => {
      if (para.length) out.push(`<p dir="auto">${para.map(inline).join('<br>')}</p>`);
      para = [];
    };
    while (i < lines.length) {
      const line = lines[i];
      const fence = line.match(/^\s*(`{3,}|~{3,})\s*([\w+#.-]*)/);
      if (fence) {
        flush();
        const before = out.length ? out[out.length - 1].replace(/<[^>]+>/g, ' ') : '';
        const code = [];
        i++;
        while (i < lines.length && !lines[i].trim().startsWith(fence[1])) code.push(lines[i++]);
        const open = i >= lines.length;
        // A chart: a ```chart block, or a ```json one that is plainly a chart's description (models
        // reach for json out of habit).
        const body = code.join('\n');
        const drawn = open ? '' : /^(chart|graph)$/i.test(fence[2]) ? chart(body)
          : /^(json)?$/i.test(fence[2]) && /"type"\s*:\s*"(bar|hbar|barh|line|area|pie|donut|doughnut)"/i.test(body)
            && /"(labels|series|data)"\s*:/.test(body) ? chart(body) : '';
        out.push(drawn || codeBlock(code.join('\n'), fence[2], (lines[i - code.length - 2] || '') + ' ' + before, open));
        i++;
        continue;
      }
      const opened = line.match(/^\s*:::\s*([A-Za-z]+)\s*(.*)$/);
      if (opened) {
        flush();
        const inner = [];
        let depth = 1;
        i++;
        while (i < lines.length) {
          if (/^\s*:::\s*[A-Za-z]/.test(lines[i])) depth++;
          else if (/^\s*:::\s*$/.test(lines[i]) && !--depth) break;
          inner.push(lines[i++]);
        }
        i++;
        out.push(part(opened[1], opened[2].trim(), inner.join('\n')));
        continue;
      }
      if (/^\s*$/.test(line)) { flush(); i++; continue; }
      const heading = line.match(/^(#{1,6})\s+(.*)$/);
      if (heading) {
        flush();
        out.push(`<h${heading[1].length} dir="auto">${inline(heading[2].replace(/\s+#+\s*$/, ''))}</h${heading[1].length}>`);
        i++; continue;
      }
      if (/^\s*([-*_])(\s*\1){2,}\s*$/.test(line)) { flush(); out.push('<hr>'); i++; continue; }
      if (/^\s*>/.test(line)) {
        flush();
        const quote = [];
        while (i < lines.length && /^\s*>/.test(lines[i])) quote.push(lines[i++].replace(/^\s*>\s?/, ''));
        out.push(`<blockquote>${render(quote.join('\n'))}</blockquote>`);
        continue;
      }
      if (/^\s*\|.*\|\s*$/.test(line) && /^\s*\|?\s*:?-{2,}/.test(lines[i + 1] || '')) {
        flush();
        const rows = [];
        while (i < lines.length && /^\s*\|.*\|\s*$/.test(lines[i])) rows.push(lines[i++]);
        out.push(table(rows, opts.vertical));
        continue;
      }
      if (/^\s*([-*+]|\d+[.)])\s+/.test(line)) {
        flush();
        const items = [];
        while (i < lines.length && (/^\s*([-*+]|\d+[.)])\s+/.test(lines[i])
               || (/^\s{2,}\S/.test(lines[i]) && items.length))) items.push(lines[i++]);
        out.push(list(items));
        continue;
      }
      para.push(line);
      i++;
    }
    flush();
    return out.join('');
  }

  window.MD = { render, highlight, esc, inline, diffLines };
})();
