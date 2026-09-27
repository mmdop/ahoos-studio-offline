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

  function table(rows) {
    const cells = row => row.replace(/^\s*\|/, '').replace(/\|\s*$/, '').split('|').map(c => c.trim());
    const head = cells(rows[0]);
    const align = cells(rows[1]).map(c => (/^:-+:$/.test(c) ? 'center' : /-+:$/.test(c) ? 'end' : ''));
    const body = rows.slice(2).map(cells);
    const td = (tag, c, i) => `<${tag}${align[i] ? ` style="text-align:${align[i]}"` : ''} dir="auto">${inline(c)}</${tag}>`;
    return '<div class="table"><table><thead><tr>' + head.map((c, i) => td('th', c, i)).join('')
      + '</tr></thead><tbody>' + body.map(r => '<tr>' + r.map((c, i) => td('td', c, i)).join('') + '</tr>').join('')
      + '</tbody></table></div>';
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

  function render(src) {
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
        out.push(codeBlock(code.join('\n'), fence[2], (lines[i - code.length - 2] || '') + ' ' + before, open));
        i++;
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
        out.push(table(rows));
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
