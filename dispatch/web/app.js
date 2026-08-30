/* dispatch — local board UI. No build step, no dependencies, no network. */
'use strict';

let S = null;             // last snapshot
let TAB = 'board';
let OPEN = null;          // open card id
let WF = null;            // working copy of workflows (editor)
let WFSEL = null;
let DRAFT = null;         // {id, brief, acceptance} — unsaved drawer edits
let DSEQ = 0;             // guards against a stale drawer fetch clobbering a newer one

const STATUSES = ['queued', 'running', 'checkpoint', 'blocked', 'deadletter',
                  'failed', 'done', 'cancelled'];

const $  = (s, r = document) => r.querySelector(s);
const $$ = (s, r = document) => Array.from(r.querySelectorAll(s));
const esc = s => String(s ?? '').replace(/[&<>"']/g, c =>
  ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

async function api(path, opts) {
  const r = await fetch(path, {
    headers: { 'Content-Type': 'application/json' },
    ...opts,
    body: opts && opts.body ? JSON.stringify(opts.body) : undefined,
  });
  const t = await r.text();
  let j = null;
  try { j = t ? JSON.parse(t) : null; } catch (e) { j = { error: t }; }
  if (!r.ok) throw new Error((j && j.error) || r.statusText);
  return j;
}

function toast(msg, ms = 2600) {
  const el = $('#toast');
  el.textContent = msg;
  el.classList.remove('hide');
  clearTimeout(el._t);
  el._t = setTimeout(() => el.classList.add('hide'), ms);
}

/* ----------------------------------------------------------- little units */

const deep = o => JSON.parse(JSON.stringify(o));

function usd(v) {
  const n = Number(v);
  if (!isFinite(n)) return '—';
  return n >= 1 ? '$' + n.toFixed(2) : ('$' + n.toFixed(3)).replace(/0$/, '');
}

function dur(s) {
  const n = Number(s);
  if (!isFinite(n) || n <= 0) return '—';
  if (n < 60) return Math.round(n) + 's';
  const m = Math.floor(n / 60);
  return m < 60 ? m + 'm ' + Math.round(n % 60) + 's'
                : Math.floor(m / 60) + 'h ' + (m % 60) + 'm';
}

function ago(ts) {
  const s = Date.now() / 1000 - Number(ts || 0);
  if (!isFinite(s)) return '';
  if (s < 45) return 'just now';
  if (s < 5400) return Math.round(s / 60) + 'm ago';
  if (s < 172800) return Math.round(s / 3600) + 'h ago';
  return Math.round(s / 86400) + 'd ago';
}

const when = ts => new Date(Number(ts || 0) * 1000).toLocaleString();

/* A colour out of the workflow config is still config, and config reaches this
   page over HTTP. Only shapes we recognise are allowed into a style attribute. */
function cssColor(v, fallback) {
  const s = String(v ?? '').trim();
  if (/^#[0-9a-fA-F]{3,8}$/.test(s)) return s;
  if (/^[a-zA-Z]{3,20}$/.test(s)) return s;
  if (/^(?:rgb|rgba|hsl|hsla)\([0-9.,%\s/]{1,60}\)$/i.test(s)) return s;
  return fallback;
}

/* ============================================================== markdown ===
   A small CommonMark-ish renderer for agent-authored prose. Agents write
   headings, lists, bold, code spans and fenced blocks; before this they were
   dumped into a <pre> and read like a hex dump.

   Escaping strategy — the only rule here that really matters: every fragment
   of input goes through esc() BEFORE a single angle bracket of ours is added,
   and the inline pass then rewrites that already-escaped string. Raw input is
   never interpolated into HTML. The only attributes we emit are built from
   values we validated ourselves — a language token matched against
   [A-Za-z0-9_.+#-], an integer list start, a fixed alignment keyword, and a
   URL matched against an http(s)/mailto allowlist. So <script>, <img
   onerror=...> and javascript: links all come out as visible text. */

/* The info string after a fence is free text; only a clean token is ever
   allowed near an attribute (see the data-lang check below). */
const RE_FENCE = /^ {0,3}(`{3,}|~{3,})[ \t]*([^`]*?)[ \t]*$/;
const RE_HEAD  = /^ {0,3}(#{1,6})[ \t]+(.*?)[ \t]*#*[ \t]*$/;
const RE_HR    = /^ {0,3}(?:-[ \t]*){3,}$|^ {0,3}(?:\*[ \t]*){3,}$|^ {0,3}(?:_[ \t]*){3,}$/;
const RE_QUOTE = /^ {0,3}>[ \t]?(.*)$/;
const RE_UL    = /^( *)([-*+])[ \t]+(.*)$/;
const RE_OL    = /^( *)(\d{1,9})[.)][ \t]+(.*)$/;
const RE_ITEM  = /^ *(?:[-*+]|\d{1,9}[.)])[ \t]+/;
const RE_SAFE_URL = /^(?:https?:\/\/|mailto:)[^\s"'<>`]+$/i;
const FLANK    = /[A-Za-z0-9*]/;   /* emphasis must not be glued to a word */
const MAXD     = 8;

/** Render markdown to HTML. The input is untrusted. */
function md(src) {
  const text = String(src ?? '').replace(/\r\n?/g, '\n').replace(/\t/g, '    ');
  return mdBlocks(text.split('\n'), 0);
}

/** md() wrapped in the container the stylesheet scopes prose rules to. */
function prose(src, cap) {
  let text = String(src ?? '');
  let cut = false;
  if (cap && text.length > cap) { text = text.slice(0, cap); cut = true; }
  return '<div class="md">' + md(text) +
    (cut ? '<p class="muted">… truncated for display</p>' : '') + '</div>';
}

const indentOf = l => l.match(/^ */)[0].length;
const isMarker = l => RE_UL.test(l) || RE_OL.test(l);

function isFenceClose(line, ch, n) {
  const t = line.replace(/[ \t]+$/, '');
  if (/^ {4,}/.test(t)) return false;
  const s = t.replace(/^ {0,3}/, '');
  if (s.length < n) return false;
  for (let k = 0; k < s.length; k++) if (s[k] !== ch) return false;
  return true;
}

function rowCells(line) {
  let s = line.trim();
  if (s.indexOf('|') < 0) return null;
  if (s.charAt(0) === '|') s = s.slice(1);
  if (s.charAt(s.length - 1) === '|') s = s.slice(0, -1);
  return s.split('|');
}

function isTableStart(lines, i) {
  const head = rowCells(lines[i]);
  if (!head || head.length < 1 || i + 1 >= lines.length) return false;
  const delim = rowCells(lines[i + 1]);
  if (!delim || delim.length !== head.length) return false;
  return delim.every(c => /^\s*:?-+:?\s*$/.test(c));
}

function startsBlock(lines, i) {
  const l = lines[i];
  return RE_FENCE.test(l) || RE_HEAD.test(l) || RE_HR.test(l) ||
         RE_QUOTE.test(l) || isMarker(l) || isTableStart(lines, i);
}

function dedent(line, n) {
  let k = 0;
  while (k < n && line.charAt(k) === ' ') k++;
  return line.slice(k);
}

function mdBlocks(lines, depth) {
  const out = [];
  let i = 0;
  while (i < lines.length) {
    const line = lines[i];
    if (!line.trim()) { i++; continue; }
    let m;

    if ((m = RE_FENCE.exec(line))) {
      const ch = m[1].charAt(0), n = m[1].length, lang = m[2] || '';
      const body = [];
      i++;
      while (i < lines.length && !isFenceClose(lines[i], ch, n)) { body.push(lines[i]); i++; }
      if (i < lines.length) i++;                     // eat the closing fence
      const cls = /^[A-Za-z0-9_.+#-]+$/.test(lang) ? lang.toLowerCase() : '';
      out.push('<pre class="md-code"' + (cls ? ' data-lang="' + cls + '"' : '') +
               '><code>' + esc(body.join('\n')) + '</code></pre>');
      continue;
    }

    if (RE_HR.test(line)) { out.push('<hr class="mdhr">'); i++; continue; }

    if ((m = RE_HEAD.exec(line))) {
      const lv = m[1].length, tag = 'h' + Math.min(6, lv + 2);
      out.push('<' + tag + ' class="mdh mdh' + lv + '">' +
               inlineMd(esc(m[2]), 0) + '</' + tag + '>');
      i++;
      continue;
    }

    if (RE_QUOTE.test(line) && depth < MAXD) {
      const q = [];
      while (i < lines.length && (m = RE_QUOTE.exec(lines[i]))) { q.push(m[1]); i++; }
      out.push('<blockquote>' + mdBlocks(q, depth + 1) + '</blockquote>');
      continue;
    }

    if (isMarker(line) && depth < MAXD) {
      const r = takeList(lines, i, depth);
      out.push(r.html);
      i = r.i;
      continue;
    }

    if (isTableStart(lines, i)) {
      const r = takeTable(lines, i);
      out.push(r.html);
      i = r.i;
      continue;
    }

    /* Paragraph. Always eats its first line, so the loop cannot stall. Single
       newlines are kept (the stylesheet gives paragraphs `white-space:
       pre-wrap`): agents hard-wrap their prose and paste terminal output, and
       reflowing a stack trace into one long line loses more than it gains. */
    const buf = [lines[i].replace(/[ \t]+$/, '')];
    i++;
    while (i < lines.length && lines[i].trim() && !startsBlock(lines, i)) {
      buf.push(lines[i].replace(/[ \t]+$/, ''));
      i++;
    }
    out.push('<p>' + inlineMd(esc(buf.join('\n')), 0) + '</p>');
  }
  return out.join('');
}

function takeList(lines, start, depth) {
  const oh = RE_OL.exec(lines[start]);
  const ordered = !!oh;
  const base = (oh || RE_UL.exec(lines[start]))[1].length;
  const region = [];
  let i = start;

  while (i < lines.length) {
    const l = lines[i];
    if (!l.trim()) {                                  // blank: does the list go on?
      let j = i;
      while (j < lines.length && !lines[j].trim()) j++;
      const cont = j < lines.length &&
        (indentOf(lines[j]) > base ||
         (indentOf(lines[j]) === base && isMarker(lines[j]) && RE_OL.test(lines[j]) === ordered));
      if (!cont) break;
      while (i < j) { region.push(''); i++; }
      continue;
    }
    const ind = indentOf(l);
    if (isMarker(l) && ind === base) {
      if (RE_OL.test(l) !== ordered) break;            // a different list starts
      region.push(l); i++; continue;
    }
    if (ind > base) { region.push(l); i++; continue; } // nested or continued
    if (isMarker(l)) break;                            // marker further out
    if (lines[i - 1] && lines[i - 1].trim() && !startsBlock(lines, i)) {
      region.push(l); i++; continue;                   // lazy continuation
    }
    break;
  }

  const items = [];
  region.forEach(l => {
    if (isMarker(l) && indentOf(l) === base) items.push([l]);
    else if (items.length) items[items.length - 1].push(l);
  });

  const li = items.map(it => {
    const mm = RE_ITEM.exec(it[0]);
    const ci = mm ? mm[0].length : 0;
    const body = [it[0].slice(ci)].concat(it.slice(1).map(l => dedent(l, ci)));
    return '<li>' + mdBlocks(body, depth + 1) + '</li>';
  }).join('');

  if (!ordered) return { html: '<ul class="mdl">' + li + '</ul>', i };
  const n = Math.min(999999, Math.max(0, parseInt(oh[2], 10) || 1));
  return { html: '<ol class="mdl"' + (n !== 1 ? ' start="' + n + '"' : '') + '>' + li + '</ol>', i };
}

function takeTable(lines, start) {
  const head = rowCells(lines[start]);
  const align = rowCells(lines[start + 1]).map(d => {
    const t = d.trim();
    const l = t.charAt(0) === ':', r = t.charAt(t.length - 1) === ':';
    return l && r ? 'center' : r ? 'right' : 'left';
  });
  let i = start + 2;
  const body = [];
  while (i < lines.length && lines[i].trim() && rowCells(lines[i])) {
    body.push(rowCells(lines[i]));
    i++;
  }
  const cell = (tag, txt, k) =>
    '<' + tag + ' style="text-align:' + (align[k] || 'left') + '">' +
    inlineMd(esc(String(txt ?? '').trim()), 0) + '</' + tag + '>';
  const html =
    '<div class="mdtw"><table><thead><tr>' +
    head.map((h, k) => cell('th', h, k)).join('') +
    '</tr></thead><tbody>' +
    body.map(r => '<tr>' + head.map((_, k) => cell('td', r[k], k)).join('') + '</tr>').join('') +
    '</tbody></table></div>';
  return { html, i };
}

/** Inline pass. `s` is ALREADY escaped; nothing here re-introduces raw input. */
function inlineMd(s, depth) {
  /* One pass, alternatives in precedence order: code span, bold, emphasis,
     link. Emphasis content may not contain its own delimiter, which keeps the
     match to the nearest closer and the matching linear. */
  const re = /(`+)([\s\S]*?)\1|\*\*(?!\s)((?:[^*]|\*(?!\*))*[^\s*])\*\*|\*(?!\s)([^*\n]*[^\s*])\*|\[([^\]\n]*)\]\(([^()\s]*)\)/g;
  const d = depth || 0;
  const down = x => (d >= MAXD ? x : inlineMd(x, d + 1));
  let out = '', last = 0, m;

  while ((m = re.exec(s)) !== null) {
    const start = m.index, end = re.lastIndex;
    if (end === start) { re.lastIndex = start + 1; continue; }
    let piece;

    if (m[1] !== undefined) {
      piece = '<code>' + m[2].replace(/^ | $/g, '') + '</code>';
    } else if (m[3] !== undefined || m[4] !== undefined) {
      const prev = start > 0 ? s.charAt(start - 1) : '';
      const next = end < s.length ? s.charAt(end) : '';
      if (FLANK.test(prev) || FLANK.test(next)) {     // `src/**` is not emphasis
        out += s.slice(last, start + 1);
        last = start + 1;
        re.lastIndex = start + 1;
        continue;
      }
      piece = m[3] !== undefined
        ? '<strong>' + down(m[3]) + '</strong>'
        : '<em>' + down(m[4]) + '</em>';
    } else {
      const url = m[6];
      const label = down(m[5]);
      piece = RE_SAFE_URL.test(url)
        ? '<a href="' + url + '" target="_blank" rel="noopener noreferrer">' + (label || url) + '</a>'
        : '[' + label + '](' + url + ')';
    }
    out += s.slice(last, start) + piece;
    last = end;
  }
  return out + s.slice(last);
}

/* ------------------------------------------------------------------ state */

async function refresh() {
  try {
    S = await api('/api/state');
  } catch (e) {
    return toast('lost the board: ' + e.message);
  }
  renderTop();
  if (TAB === 'board') renderBoard();
  if (TAB === 'needs') renderNeeds();
  if (TAB === 'blocked') renderBlocked();
  if (TAB === 'workflows') { if (!WF) { WF = deep(S.workflows); WFSEL = Object.keys(WF)[0] || null; } renderWorkflows(); }
  if (TAB === 'log') renderLog();
  if (OPEN) renderDrawer(OPEN);
}

function renderTop() {
  const sc = S.scheduler;
  const dot = sc.running ? (sc.paused ? 'warn' : 'on') : 'off';
  const word = !sc.running ? 'scheduler down' : sc.paused ? 'paused' : 'running';
  const st = $('#schedState');
  st.className = 'stat' + (sc.running && !sc.paused ? '' : ' warn');
  st.innerHTML = '<span class="dot ' + dot + '"></span>' + word +
    ' · <b>' + sc.in_flight + '</b>/' + sc.max_concurrent + ' in flight';

  // A run's cost only exists once it ends, so a board with agents working is
  // always showing a figure that is behind. Saying by how much is the honest
  // version of "live spend" — inventing a number from token counts would need
  // a price table, and a stale price table reports confident nonsense.
  const inf = S.stats.in_flight_runs || 0;
  const pend = inf
    ? ' <span class="pending" title="cost is reported when a run ends, not' +
      ' while it works">+' + inf + ' running' +
      (S.stats.in_flight_since
        ? ' · ' + ago(S.stats.in_flight_since) : '') + '</span>'
    : '';
  $('#spend').innerHTML = '<b>' + usd(S.stats.usd) + '</b> · ' +
    S.stats.runs + ' runs' + pend;

  const r = S.stats.expansion_ratio;
  const lim = (S.config.containment || {}).expansion_ratio_limit || 2.5;
  $('#ratio').innerHTML = r
    ? 'expansion <b' + (r > lim ? ' class="over"' : '') + '>' + r.toFixed(2) + '×</b>'
    : '';

  const p = $('#pauseBtn');
  p.textContent = sc.paused ? 'Resume' : 'Pause';
  p.title = sc.paused ? 'Let the scheduler dispatch again' : 'Stop dispatching new work';

  const n = S.checkpoints.length + S.proposals.filter(x => x.status === 'escalated').length;
  const b = $('#cpCount');
  b.hidden = n === 0;
  b.textContent = n;
}

/* ------------------------------------------------------------------ board */

const typeColor = ct => cssColor((S.workflows[ct] || {}).color, 'var(--muted)');

function statusClass(t) {
  return STATUSES.indexOf(t.status) >= 0 ? t.status : '';
}

function cardEl(t) {
  const d = document.createElement('div');
  d.className = 'card' + (t.running ? ' running' : '') + ' ' + statusClass(t);
  d.draggable = true;
  d.dataset.id = t.id;
  d.tabIndex = 0;
  d.setAttribute('role', 'button');
  d.title = t.id + ' — open card';

  const wf = S.workflows[t.card_type] || {};
  const chips = [];
  if (t.status === 'checkpoint') chips.push('<span class="chip warn">needs you</span>');
  if (t.status === 'deadletter') chips.push('<span class="chip stop">dead letter</span>');
  if (t.status === 'blocked') chips.push('<span class="chip warn">blocked</span>');
  if (t.attempts > 0) chips.push('<span class="chip warn">try ' + (t.attempts + 1) + '/' + t.max_attempts + '</span>');
  chips.push('<span class="chip type"><i class="sw" style="background:' + typeColor(t.card_type) +
             '"></i>' + esc(wf.label || t.card_type) + '</span>');
  if (t.agent_type) chips.push('<span class="chip agent">' + esc(t.agent_type) + '</span>');
  if (t.model) chips.push('<span class="chip model" title="this card overrides the model">' +
    esc(t.model) + '</span>');
  (t.tags || []).slice(0, 3).forEach(g => chips.push('<span class="chip">#' + esc(g) + '</span>'));
  if (t.deps.length) chips.push('<span class="chip" title="waits on ' + t.deps.length +
                                ' card(s)">⇠ ' + t.deps.length + '</span>');

  let why = '';
  if (t.defer_reason && t.defer_until > Date.now() / 1000) why = 'deferred — ' + esc(t.defer_reason);
  else if (t.status === 'blocked' && t.block_reason) why = esc(t.block_reason);

  d.innerHTML =
    '<div class="t">' + esc(t.title) + '</div><div class="m">' + chips.join('') + '</div>' +
    (why ? '<div class="why">' + why + '</div>' : '');

  d.onclick = () => openDrawer(t.id);
  d.onkeydown = e => {
    if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); openDrawer(t.id); }
  };
  d.ondragstart = e => { e.dataTransfer.setData('text/plain', t.id); };
  return d;
}

// Board filters. Kept in localStorage so a board you have tidied stays tidy
// across a reload — but see `fNote`: a filter that hides cards silently is a
// filter that makes you think work vanished.
const FILT = {
  done: false, cancelled: false, text: '',
  load() {
    try {
      const v = JSON.parse(localStorage.getItem('dispatch.filters') || '{}');
      this.done = !!v.done; this.cancelled = !!v.cancelled;
    } catch (e) { /* private window, cleared storage — defaults are fine */ }
  },
  save() {
    try {
      localStorage.setItem('dispatch.filters',
        JSON.stringify({ done: this.done, cancelled: this.cancelled }));
    } catch (e) { /* not worth failing a render over */ }
  },
  // `hidden` is what a filter removes; a card matching the text box is never
  // hidden by it, so searching for a done card still finds it.
  hides(t) {
    if (this.text) {
      const hay = (t.title + ' ' + (t.id || '') + ' ' +
                   (parseTags(t).join(' '))).toLowerCase();
      if (!hay.includes(this.text)) return true;
    }
    if (this.done && t.status === 'done') return true;
    if (this.cancelled && t.status === 'cancelled') return true;
    return false;
  },
};

function parseTags(t) {
  try {
    return typeof t.tags === 'string' ? JSON.parse(t.tags) : (t.tags || []);
  } catch (e) { return []; }
}

function renderBoard() {
  const wrap = $('#columns');
  wrap.innerHTML = '';
  const live = {}, attn = {};
  S.tasks.forEach(t => {
    if (t.running) live[t.stage] = (live[t.stage] || 0) + 1;
    if (t.status === 'checkpoint' || t.status === 'deadletter')
      attn[t.stage] = (attn[t.stage] || 0) + 1;
  });

  let hidden = 0;
  S.stages.forEach(st => {
    const col = document.createElement('div');
    col.className = 'col';
    col.dataset.stage = st.id;
    const all = S.tasks.filter(t => t.stage === st.id &&
      !(st.id === 'done' && t.status === 'cancelled'));
    const items = all.filter(t => !FILT.hides(t));
    hidden += all.length - items.length;
    const wip = st.wip || 0;
    const inFlight = live[st.id] || 0;
    const nShown = items.length === all.length
      ? String(all.length)
      : items.length + '<span class="of">/' + all.length + '</span>';

    col.innerHTML =
      '<h3 class="colhead">' +
        '<span class="colname">' + esc(st.label) + '</span>' +
        (attn[st.id] ? '<span class="flag" title="cards waiting on you">' + attn[st.id] + ' ⚑</span>' : '') +
        '<span class="n" title="cards in this column">' + nShown + '</span>' +
        (wip ? '<span class="wip' + (inFlight >= wip ? ' over' : '') + '" title="work in progress limit">' +
               inFlight + '/' + wip + '</span>' : '') +
      '</h3><div class="cards"></div>';

    const box = $('.cards', col);
    if (!items.length) {
      box.innerHTML = '<div class="empty">' +
        (all.length ? 'all ' + all.length + ' hidden' : 'empty') + '</div>';
    }
    items.forEach(t => box.appendChild(cardEl(t)));

    col.ondragover = e => { e.preventDefault(); col.classList.add('dragover'); };
    col.ondragleave = () => col.classList.remove('dragover');
    col.ondrop = async e => {
      e.preventDefault();
      col.classList.remove('dragover');
      const id = e.dataTransfer.getData('text/plain');
      if (!id) return;
      try {
        await api('/api/task/' + id + '/move', { method: 'POST', body: { stage: st.id } });
        toast(id + ' → ' + st.label);
        refresh();
      } catch (err) { toast(err.message); }
    };
    wrap.appendChild(col);
  });
  // Say what is being hidden. A filtered board that looks like an unfiltered
  // one is how you conclude a card was never created.
  const note = $('#fNote');
  if (note) {
    note.textContent = hidden ? hidden + ' card' + (hidden === 1 ? '' : 's') +
      ' hidden' : '';
    note.classList.toggle('on', !!hidden);
  }
  updateBoardEdges();
}

function wireFilters() {
  FILT.load();
  const done = $('#fDone'), cancelled = $('#fCancelled'), text = $('#fText');
  if (!done) return;
  done.checked = FILT.done;
  cancelled.checked = FILT.cancelled;
  // The labels say what the box does, so they read as "hide Done" when ticked
  const sync = () => {
    done.parentElement.classList.toggle('on', done.checked);
    cancelled.parentElement.classList.toggle('on', cancelled.checked);
  };
  sync();
  done.onchange = () => { FILT.done = done.checked; FILT.save(); sync(); renderBoard(); };
  cancelled.onchange = () => {
    FILT.cancelled = cancelled.checked; FILT.save(); sync(); renderBoard();
  };
  let t = null;
  text.oninput = () => {
    clearTimeout(t);
    t = setTimeout(() => {
      FILT.text = text.value.trim().toLowerCase();
      renderBoard();
    }, 120);
  };
}

/* The board scrolls sideways when the stages outrun the viewport, but macOS
   hides overlay scrollbars — so without an edge fade the last column just
   looks clipped and people never discover the rest. */
function updateBoardEdges() {
  const wrap = $('#columns');
  const pane = $('#pane-board');
  if (!wrap || !pane) return;
  const slack = wrap.scrollWidth - wrap.clientWidth;
  pane.classList.toggle('more-left', wrap.scrollLeft > 2);
  pane.classList.toggle('more-right', slack > 2 && wrap.scrollLeft < slack - 2);
}

$('#columns').addEventListener('scroll', updateBoardEdges, { passive: true });
window.addEventListener('resize', updateBoardEdges);

/* ----------------------------------------------------------------- drawer */

function openDrawer(id) { if (OPEN !== id) DRAFT = null; OPEN = id; renderDrawer(id); }

function closeDrawer() {
  OPEN = null;
  DRAFT = null;
  DSEQ++;
  $('#drawer').className = 'drawer hide';
}

function diffHtml(diff) {
  return esc(diff).split('\n').map(l => {
    if (l.startsWith('+++') || l.startsWith('---') || l.startsWith('@@') || l.startsWith('diff '))
      return '<span class="h">' + l + '</span>';
    if (l.startsWith('+')) return '<span class="a">' + l + '</span>';
    if (l.startsWith('-')) return '<span class="d">' + l + '</span>';
    return l;
  }).join('\n');
}

const verdictChip = v =>
  '<span class="chip ' + (v === 'pass' ? 'ok' : v === 'defer' ? 'warn' : 'stop') + '">' + esc(v) + '</span>';

const idLink = id =>
  '<button class="linkish" type="button" data-act="open" data-id="' + esc(id) + '">' + esc(id) + '</button>';

function pipelineHtml(wf, stage) {
  const stages = wf.stages || [];
  if (!stages.length) return '<span class="muted">no pipeline for this card type</span>';
  const cur = stages.map(s => s.stage).indexOf(stage);
  return stages.map((s, i) => {
    const k = i === cur ? ' now' : (cur >= 0 && i < cur ? ' past' : '');
    return '<span class="st' + k + '" title="worked by ' + esc(s.agent || '—') + '">' +
           esc(s.stage) + '</span>';
  }).join('<span class="arrow">→</span>');
}

async function renderDrawer(id) {
  const seq = ++DSEQ;
  let t;
  try { t = await api('/api/task/' + id); }
  catch (e) { if (seq === DSEQ) closeDrawer(); return; }
  if (seq !== DSEQ || OPEN !== id) return;

  const el = $('#drawer');
  const oldBody = $('.dbody', el);
  const scroll = oldBody ? oldBody.scrollTop : 0;
  const act = document.activeElement;
  const keep = act && (act.id === 'dBrief' || act.id === 'dAcc')
    ? { id: act.id, s: act.selectionStart, e: act.selectionEnd } : null;

  const wf = S.workflows[t.card_type] || {};
  const scope = (t.workspace || {}).scope || [];
  const runs = t.runs || [];
  const gates = t.gate_runs || [];

  el.className = 'drawer' + (t.running ? ' st-running' : ' st-' + (statusClass(t) || 'queued'));
  el.innerHTML =
    '<header class="dhead">' +
      '<div class="who">' +
        '<h3>' + esc(t.title) + '</h3>' +
        '<div class="id">' +
          '<span>' + esc(t.id) + '</span><span class="sep">·</span>' +
          '<span>' + esc(wf.label || t.card_type) + '</span><span class="sep">·</span>' +
          '<span>' + esc(t.stage) + '</span><span class="sep">/</span>' +
          '<span>' + esc(t.status) + '</span>' +
          (t.running ? '<span class="chip ok">running</span>' : '') +
          '<span class="sep">·</span><span title="' + esc(when(t.updated_at)) + '">updated ' + esc(ago(t.updated_at)) + '</span>' +
        '</div>' +
      '</div>' +
      '<button class="btn ghost" id="dClose" title="Close (Esc)">Close ✕</button>' +
    '</header>' +

    '<div class="dbody">' +

      '<section><h4 class="lbl">Pipeline</h4>' +
        '<div class="pipeline">' + pipelineHtml(wf, t.stage) + '</div></section>' +

      '<section><h4 class="lbl">Card</h4>' +
        '<dl class="kv">' +
          '<dt>agent</dt><dd>' + esc(t.agent_type || '—') + '</dd>' +
          '<dt>attempts</dt><dd>' + t.attempts + ' / ' + t.max_attempts + '</dd>' +
          '<dt>priority</dt><dd>' + t.priority + '</dd>' +
          ((t.tags || []).length ? '<dt>tags</dt><dd>' +
            t.tags.map(g => '<span class="chip">#' + esc(g) + '</span>').join(' ') + '</dd>' : '') +
          '<dt>branch</dt><dd class="mono">' + esc((t.workspace || {}).branch || '—') + '</dd>' +
          '<dt>scope</dt><dd class="mono">' +
            (scope.length ? esc(scope.join(', ')) : '<span class="muted">none declared</span>') + '</dd>' +
          '<dt>provenance</dt><dd class="mono">' + esc(t.provenance) + '</dd>' +
          '<dt>created</dt><dd title="' + esc(when(t.created_at)) + '">' + esc(ago(t.created_at)) + '</dd>' +
          (t.block_reason ? '<dt>blocked</dt><dd class="warnv">' + esc(t.block_reason) + '</dd>' : '') +
          (t.defer_reason ? '<dt>deferred</dt><dd class="warnv">' + esc(t.defer_reason) + '</dd>' : '') +
        '</dl></section>' +

      '<section><h4 class="lbl">Brief</h4>' +
        '<textarea id="dBrief" rows="8" aria-label="Brief">' + esc(t.brief) + '</textarea>' +
        '<p class="hint">This is the literal prompt the agent receives.</p>' +
        '<h4 class="lbl" style="margin:14px 0 8px">Acceptance criteria</h4>' +
        '<textarea id="dAcc" rows="4" aria-label="Acceptance criteria" ' +
          'placeholder="one per line — at least one should be a runnable command">' +
          esc((t.acceptance || []).join('\n')) + '</textarea></section>' +

      (t.last_evidence
        ? '<section><h4 class="lbl">Why the last attempt was returned</h4>' +
          prose(t.last_evidence, 8000) + '</section>' : '') +

      (t.dep_titles && t.dep_titles.length
        ? '<section><h4 class="lbl">Waits on<span class="count">' + t.dep_titles.length + '</span></h4>' +
          t.dep_titles.map(d =>
            '<div class="gateline">' + idLink(d.id) +
            '<span class="chip">' + esc(d.status) + '</span>' +
            '<span class="r">' + esc(d.title) + '</span></div>').join('') +
          '</section>' : '') +

      '<section><h4 class="lbl">Gate history<span class="count">' + gates.length + '</span></h4>' +
        (gates.length
          ? gates.slice(0, 14).map(g =>
              '<div class="gateline">' + verdictChip(g.verdict) +
              '<span class="g">' + esc(g.gate) + '</span>' +
              (g.reason ? '<span class="r">' + esc(g.reason) + '</span>' : '') +
              '<span class="r" style="margin-left:auto" title="' + esc(when(g.ts)) + '">' +
                esc(ago(g.ts)) + '</span></div>').join('')
          : '<div class="muted">no gates have run yet</div>') +
      '</section>' +

      '<section><h4 class="lbl">Runs<span class="count">' + runs.length + '</span></h4>' +
        (runs.length ? runs.map(r =>
          '<div class="runitem">' +
            '<div class="runhead">' +
              '<span class="chip ' + (r.exit_code === 0 ? 'ok' : 'stop') + '">exit ' + esc(r.exit_code) + '</span>' +
              '<span class="chip">' + esc(r.stage) + ' / ' + esc(r.agent_type) + '</span>' +
              '<span class="chip">attempt ' + esc(r.attempt) + '</span>' +
              '<span class="chip">' + esc(usd(r.usd)) + '</span>' +
              '<span class="chip">' + esc(dur(r.duration_s)) + '</span>' +
              '<span class="grow"></span>' +
              '<span class="mono muted" title="' + esc(when(r.finished_at)) + '">' + esc(r.id) + '</span>' +
            '</div>' +
            (r.summary ? prose(r.summary, 8000) : '<div class="muted">no summary</div>') +
            (r.log_dir ? '<div class="mono muted" style="margin-top:8px">' + esc(r.log_dir) + '</div>' : '') +
          '</div>').join('')
          : '<div class="muted">not yet worked</div>') +
      '</section>' +

    '</div>' +

    '<footer class="dfoot">' +
      '<button class="btn primary" id="dSave">Save changes</button>' +
      (t.stage === 'backlog' ? '<button class="btn" id="dStart">Start card</button>' : '') +
      '<button class="btn danger" id="dCancel">Cancel card</button>' +
      '<span class="dirty" id="dDirty" hidden>unsaved edits</span>' +
    '</footer>';

  const body = $('.dbody', el);
  body.scrollTop = scroll;

  const brief = $('#dBrief'), acc = $('#dAcc'), dirty = $('#dDirty');
  if (DRAFT && DRAFT.id === t.id) {
    brief.value = DRAFT.brief;
    acc.value = DRAFT.acceptance;
    dirty.hidden = false;
  }
  if (keep) {
    const f = $('#' + keep.id);
    if (f) { f.focus(); try { f.setSelectionRange(keep.s, keep.e); } catch (_) { /* ignore */ } }
  }

  const note = () => {
    DRAFT = { id: t.id, brief: brief.value, acceptance: acc.value };
    dirty.hidden = false;
  };
  brief.oninput = note;
  acc.oninput = note;

  $('#dClose').onclick = closeDrawer;
  $('#dSave').onclick = async () => {
    try {
      await api('/api/task/' + t.id, {
        method: 'PUT',
        body: {
          brief: brief.value,
          acceptance: acc.value.split('\n').map(s => s.trim()).filter(Boolean),
        },
      });
      DRAFT = null;
      toast('saved');
      refresh();
    } catch (err) { toast(err.message); }
  };
  const start = $('#dStart');
  if (start) start.onclick = async () => {
    try {
      await api('/api/task/' + t.id + '/start', { method: 'POST' });
      toast('started');
      refresh();
    } catch (err) { toast(err.message); }
  };
  $('#dCancel').onclick = async () => {
    if (!confirm('Cancel ' + t.id + ' and every card beneath it?')) return;
    try {
      await api('/api/task/' + t.id + '/cancel', { method: 'POST' });
      closeDrawer();
      refresh();
    } catch (err) { toast(err.message); }
  };
}

/* -------------------------------------------------------------- needs you */

const OPT_LABEL = { approve: 'Approve', amend: 'Amend brief', reject: 'Reject' };
const OPT_CLASS = { approve: 'btn primary', amend: 'btn', reject: 'btn danger' };

function checkpointHtml(c) {
  const b = c.bundle || {};
  const opts = (Array.isArray(b.options) ? b.options : ['approve', 'amend', 'reject'])
    .filter(o => OPT_LABEL[o]);
  const buttons = (opts.length ? opts : ['approve', 'amend', 'reject']).map(o =>
    '<button class="' + OPT_CLASS[o] + '" type="button" data-act="respond" ' +
    'data-id="' + esc(c.id) + '" data-response="' + esc(o) + '">' + OPT_LABEL[o] + '</button>').join('');

  const part = (label, html) => '<div class="part"><h5 class="lbl">' + label + '</h5>' + html + '</div>';

  return '<article class="item attn">' +
    '<h3 class="item-title">' + esc(c.question) + '</h3>' +
    '<div class="meta">' +
      '<span>' + esc(c.id) + '</span><span class="sep">·</span>' +
      idLink(c.task_id) +
      (b.branch ? '<span class="sep">·</span><span>' + esc(b.branch) + '</span>' : '') +
      (b.usd != null ? '<span class="sep">·</span><span>' + esc(usd(b.usd)) + '</span>' : '') +
      '<span class="sep">·</span><span title="' + esc(when(c.created_at)) + '">waiting ' + esc(ago(c.created_at)) + '</span>' +
    '</div>' +
    (b.plan ? planHtml(b.plan) : '') +
    (b.summary && !b.plan ? prose(b.summary, 20000) : '') +
    (b.evidence ? part('Evidence', prose(b.evidence, 12000)) : '') +
    (b.acceptance && b.acceptance.length
      ? part('Acceptance criteria',
          '<ul class="crit">' + b.acceptance.map(a => '<li>' + esc(a) + '</li>').join('') + '</ul>') : '') +
    (b.gates && b.gates.length
      ? part('Gates', b.gates.slice(0, 8).map(g =>
          '<div class="gateline">' + verdictChip(g.verdict) +
          '<span class="g">' + esc(g.gate) + '</span>' +
          (g.reason ? '<span class="r">' + esc(g.reason) + '</span>' : '') + '</div>').join('')) : '') +
    (b.changed_files && b.changed_files.length
      ? part(b.changed_files.length + ' changed file' + (b.changed_files.length === 1 ? '' : 's'),
          '<div class="filelist">' + b.changed_files.slice(0, 40)
            .map(f => '<span class="f">' + esc(f) + '</span>').join('') +
          (b.changed_files.length > 40 ? '<span class="f">+' + (b.changed_files.length - 40) + ' more</span>' : '') +
          '</div>') : '') +
    (b.diff
      ? '<div class="part"><details class="fold"><summary>diff</summary><div class="foldbody">' +
        '<pre class="block diff">' + diffHtml(String(b.diff).slice(0, 120000)) + '</pre>' +
        '</div></details></div>' : '') +
    (b.payload
      ? part('Payload', '<pre class="block">' + esc(JSON.stringify(b.payload, null, 2).slice(0, 4000)) + '</pre>') : '') +
    '<label class="f" style="margin-top:16px"><span class="plain">' +
      (b.plan ? 'Note — &ldquo;Amend&rdquo; sends this back to be re-planned with what you write here'
              : 'Note — a rejection reason becomes the next agent&rsquo;s instruction') +
      '</span>' +
      '<textarea id="note-' + esc(c.id) + '" rows="2"></textarea></label>' +
    '<div class="actions">' + buttons + '</div>' +
  '</article>';
}

/* A brief you are being asked to approve must never be silently truncated, and
   a wall of them is unscannable — so long ones fold, with their first line as
   the summary, and nothing is lost. */
function pcBrief(brief) {
  const text = String(brief);
  const lead = text.trim().split('\n').find(l => l.trim()) || '';
  if (text.length <= 700) {
    return '<div class="pc-brief">' + prose(text, 40000) + '</div>';
  }
  return '<details class="fold pc-fold"><summary>' +
    esc(lead.slice(0, 150)) + (lead.length > 150 ? '…' : '') +
    '<span class="pc-more">' + Math.round(text.length / 100) / 10 + 'k more</span>' +
    '</summary><div class="foldbody pc-brief">' + prose(text, 40000) +
    '</div></details>';
}

/* A plan is the one thing here a human is asked to judge whole, so it gets
   read as a sequence of cards rather than as a wall of JSON. */
function planHtml(plan) {
  if (!plan) return '';
  if (typeof plan === 'string') {
    try { plan = JSON.parse(plan); } catch (e) { return ''; }
  }
  const cards = Array.isArray(plan.cards) ? plan.cards : [];
  const list = (label, items, cls) =>
    (items && items.length)
      ? '<div class="part"><h5 class="lbl">' + label + '</h5><ul class="' + cls + '">' +
        items.map(x => '<li>' + esc(String(x)) + '</li>').join('') + '</ul></div>'
      : '';

  return '<div class="plan">' +
    (plan.summary ? prose(plan.summary, 8000) : '') +
    '<div class="part"><h5 class="lbl">' + cards.length +
      ' card' + (cards.length === 1 ? '' : 's') + ' this would create</h5>' +
    '<ol class="plancards">' + cards.map(c => {
      const deps = Array.isArray(c.depends_on) ? c.depends_on : [];
      return '<li class="plancard">' +
        '<div class="pc-head">' +
          '<span class="pc-title">' + esc(c.title || '(untitled)') + '</span>' +
          (c.card_type ? '<span class="chip">' + esc(c.card_type) + '</span>' : '') +
          (deps.length ? '<span class="chip wait">after ' + esc(deps.join(', ')) + '</span>' : '') +
        '</div>' +
        (c.brief ? pcBrief(c.brief) : '') +
        ((c.acceptance || []).length
          ? '<ul class="crit">' + c.acceptance.map(a => '<li>' + esc(a) + '</li>').join('') + '</ul>'
          : '<div class="pc-warn">no acceptance criteria — this card cannot be judged</div>') +
        ((c.scope || []).length
          ? '<div class="pc-scope">' + c.scope.map(g => '<span class="f">' + esc(g) + '</span>').join('') + '</div>'
          : '') +
      '</li>';
    }).join('') + '</ol></div>' +
    list('Risks', plan.risks, 'risks') +
    list('Deliberately out of scope', plan.out_of_scope, 'oos') +
  '</div>';
}

function proposalHtml(p) {
  let payload = String(p.payload ?? '');
  try { payload = JSON.stringify(JSON.parse(payload), null, 2); } catch (_) { /* leave as text */ }
  return '<article class="item' + (p.status === 'escalated' ? ' attn' : '') + '">' +
    '<h3 class="item-title">' + esc(p.kind) + '</h3>' +
    '<div class="meta">' +
      '<span>' + esc(p.id) + '</span><span class="sep">·</span>' +
      (p.from_task ? idLink(p.from_task) + '<span class="sep">·</span>' : '') +
      '<span class="chip ' + (p.status === 'escalated' ? 'warn' : '') + '">' + esc(p.status) + '</span>' +
      '<span class="chip">' + esc(p.tier || 'unadjudicated') + '</span>' +
      (p.urgency ? '<span class="chip">' + esc(p.urgency) + '</span>' : '') +
      (p.confidence != null ? '<span class="chip">confidence ' + esc(Number(p.confidence).toFixed(2)) + '</span>' : '') +
      '<span class="sep">·</span><span title="' + esc(when(p.created_at)) + '">' + esc(ago(p.created_at)) + '</span>' +
    '</div>' +
    (p.rationale ? prose(p.rationale, 4000) : '') +
    '<div class="part"><h5 class="lbl">Payload</h5>' +
      '<pre class="block">' + esc(payload.slice(0, 4000)) + '</pre></div>' +
    '<div class="actions">' +
      '<button class="btn primary" type="button" data-act="decide" data-id="' + esc(p.id) + '" data-decision="accept">Accept</button>' +
      '<button class="btn danger" type="button" data-act="decide" data-id="' + esc(p.id) + '" data-decision="reject">Reject</button>' +
    '</div></article>';
}

function renderNeeds() {
  $('#checkpoints').innerHTML = S.checkpoints.length
    ? S.checkpoints.map(checkpointHtml).join('')
    : '<div class="item muted">Nothing is waiting on you.</div>';

  const props = S.proposals.filter(p => p.status === 'pending' || p.status === 'escalated');
  $('#proposals').innerHTML = props.length
    ? props.map(proposalHtml).join('')
    : '<div class="item muted">No proposals waiting.</div>';
}

async function respond(cid, response) {
  const box = $('#note-' + cid);
  const note = box ? box.value : '';
  if (response === 'reject' && !note.trim() &&
      !confirm('Reject with no reason? The agent gets nothing to work with.')) return;
  try {
    await api('/api/checkpoint/' + cid + '/respond', { method: 'POST', body: { response, note } });
    toast('recorded');
    refresh();
  } catch (e) { toast(e.message); }
}

async function decide(pid, decision) {
  try {
    await api('/api/proposal/' + pid + '/decide', { method: 'POST', body: { decision } });
    toast('recorded');
    refresh();
  } catch (e) { toast(e.message); }
}

/* ---------------------------------------------------------------- blocked */

async function renderBlocked() {
  let d;
  try { d = await api('/api/blocked'); } catch (e) { return toast(e.message); }
  $('#blocked').innerHTML = d.blocked.length
    ? d.blocked.map(b =>
        '<article class="item">' +
          '<h3 class="item-title">' + esc(b.title) + '</h3>' +
          '<div class="meta">' + idLink(b.id) + '<span class="sep">·</span>' +
            '<span>' + esc(b.stage) + '</span><span class="sep">/</span>' +
            '<span>' + esc(b.status) + '</span></div>' +
          b.blockers.map(x => '<div class="blockline">' + esc(x) + '</div>').join('') +
        '</article>').join('')
    : '<div class="item muted">Nothing is blocked. Every unfinished card is either running or ready.</div>';
}

/* -------------------------------------------------------------- workflows */

const MODELS = ['opus', 'sonnet', 'haiku', 'fable'];

function renderWorkflows() {
  const list = $('#wflist');
  list.innerHTML = Object.entries(WF).map(([ct, wf]) =>
    '<button type="button" class="' + (ct === WFSEL ? 'on' : '') + '" data-ct="' + esc(ct) + '"' +
      ' aria-pressed="' + (ct === WFSEL) + '">' +
      '<span class="sw" style="background:' + cssColor(wf.color, 'var(--muted)') + '"></span>' +
      '<span>' + esc(wf.label || ct) + '</span>' +
      '<span class="n">' + (wf.stages || []).length + '</span>' +
    '</button>').join('');
  $$('#wflist button').forEach(b => b.onclick = () => { WFSEL = b.dataset.ct; renderWorkflows(); });

  const ed = $('#wfedit');
  if (!WFSEL || !WF[WFSEL]) { ed.innerHTML = '<div class="muted">Pick or add a card type.</div>'; return; }
  const wf = WF[WFSEL];
  const stageOpts = S.stages.map(s => s.id);
  const agentOpts = Object.keys(S.agents);
  const gateOpts = ['tests_pass', 'lint_clean', 'build_ok', 'has_acceptance', 'diff_scope',
                    'no_secrets', 'arbiter_judges', 'wip_limit', 'quota_above',
                    'budget_remaining', 'time_window', 'mutex_free'];

  ed.innerHTML =
    '<datalist id="modelnames">' + MODELS.map(m =>
      '<option value="' + esc(m) + '">').join('') + '</datalist>' +
    '<div class="row">' +
      '<label class="f"><span>card type id</span><input type="text" id="wfId" value="' + esc(WFSEL) + '"></label>' +
      '<label class="f"><span>label</span><input type="text" id="wfLabel" value="' + esc(wf.label || '') + '"></label>' +
      '<label class="f"><span>colour</span><span class="swatchbox">' +
        '<span class="sw" style="background:' + cssColor(wf.color, 'var(--muted)') + '"></span>' +
        '<input type="text" id="wfColor" value="' + esc(wf.color || '#6B7A75') + '"></span></label>' +
    '</div>' +
    '<h4 class="lbl" style="margin:16px 0 4px">Pipeline</h4>' +
    '<p class="sub">A card of this type enters at the first stage and moves right as each stage clears its ' +
      'gates. An agent of <code class="mono">human</code> makes that stage a checkpoint.</p>' +
    '<div class="pipe" id="pipe"></div>' +
    '<div class="actions">' +
      '<button class="btn" id="wfAddStage">Add stage</button>' +
      '<button class="btn primary" id="wfSave">Save workflows</button>' +
      '<button class="btn danger" id="wfDel">Delete type</button>' +
    '</div>' +
    '<div id="wfProblems"></div>';

  const pipe = $('#pipe');
  (wf.stages || []).forEach((s, i) => {
    const row = document.createElement('div');
    row.className = 'step';
    row.innerHTML =
      '<div class="n">' + (i + 1) + '</div>' +
      '<div class="fields">' +
        '<label class="f"><span>stage (column)</span>' +
          '<select data-k="stage">' + stageOpts.map(o =>
            '<option ' + (o === s.stage ? 'selected' : '') + '>' + esc(o) + '</option>').join('') +
          '</select></label>' +
        '<label class="f"><span>worked by</span>' +
          '<select data-k="agent">' + agentOpts.map(o =>
            '<option ' + (o === s.agent ? 'selected' : '') + '>' + esc(o) + '</option>').join('') +
          '</select></label>' +
        '<label class="f wide"><span class="plain">gates (comma separated — <code>name</code> or <code>name:arg</code>)</span>' +
          '<input type="text" data-k="gates" value="' + esc((s.gates || []).join(', ')) + '" ' +
                 'placeholder="' + esc(gateOpts.slice(0, 4).join(', ')) + '"></label>' +
        '<label class="f"><span>lock (optional)</span>' +
          '<input type="text" data-k="lock" value="' + esc(s.lock || '') + '" placeholder="integration"></label>' +
        '<label class="f"><span>model</span>' +
          '<input type="text" data-k="model" list="modelnames" value="' +
          esc(s.model || '') + '" placeholder="role default"></label>' +
        '<label class="f"><span class="plain">auto-pass rule (human stages)</span>' +
          '<input type="text" data-k="auto_pass_if" value="' + esc(s.auto_pass_if || '') + '" placeholder="small_and_green"></label>' +
        '<label class="f"><span class="plain">answer within (human stages)</span>' +
          '<input type="text" data-k="sla" value="' + esc(s.sla || '') + '" placeholder="4h"></label>' +
        '<label class="f"><span>if unanswered</span>' +
          '<select data-k="on_sla">' + ['', 'block', 'approve', 'reject'].map(o =>
            '<option value="' + esc(o) + '" ' + (o === (s.on_sla || '') ? 'selected' : '') + '>' +
            esc(o || 'block (default)') + '</option>').join('') +
          '</select></label>' +
      '</div>' +
      '<div class="ctl">' +
        '<button type="button" data-a="up" title="move up" aria-label="move up">▲</button>' +
        '<button type="button" data-a="down" title="move down" aria-label="move down">▼</button>' +
        '<button type="button" data-a="del" title="remove" aria-label="remove">✕</button>' +
      '</div>';

    $$('select,input', row).forEach(inp => inp.onchange = () => {
      const k = inp.dataset.k;
      if (k === 'gates') s.gates = inp.value.split(',').map(x => x.trim()).filter(Boolean);
      else if (!inp.value) delete s[k];
      else s[k] = inp.value;
      renderWorkflows();
    });
    $$('.ctl button', row).forEach(b => b.onclick = () => {
      const a = b.dataset.a;
      if (a === 'del') wf.stages.splice(i, 1);
      if (a === 'up' && i > 0) wf.stages.splice(i - 1, 0, wf.stages.splice(i, 1)[0]);
      if (a === 'down' && i < wf.stages.length - 1) wf.stages.splice(i + 1, 0, wf.stages.splice(i, 1)[0]);
      renderWorkflows();
    });
    pipe.appendChild(row);
    if (i < wf.stages.length - 1) {
      const a = document.createElement('div');
      a.className = 'arrowdown';
      a.textContent = '↓';
      pipe.appendChild(a);
    }
  });
  if (!(wf.stages || []).length) pipe.innerHTML = '<div class="muted">No stages yet — add one.</div>';

  $('#wfAddStage').onclick = () => {
    wf.stages = wf.stages || [];
    wf.stages.push({ stage: stageOpts[Math.min(wf.stages.length + 1, stageOpts.length - 2)] || stageOpts[0],
                     agent: agentOpts[0], gates: [] });
    renderWorkflows();
  };
  $('#wfDel').onclick = () => {
    if (!confirm('Delete card type "' + WFSEL + '"? Existing cards of this type will stop moving.')) return;
    delete WF[WFSEL];
    WFSEL = Object.keys(WF)[0] || null;
    renderWorkflows();
  };
  ['wfId', 'wfLabel', 'wfColor'].forEach(id => {
    $('#' + id).onchange = () => {
      if (id === 'wfId') {
        const nid = $('#wfId').value.trim();
        if (nid && nid !== WFSEL) { WF[nid] = WF[WFSEL]; delete WF[WFSEL]; WFSEL = nid; }
      } else wf[id === 'wfLabel' ? 'label' : 'color'] = $('#' + id).value;
      renderWorkflows();
    };
  });
  $('#wfSave').onclick = async () => {
    try {
      const r = await api('/api/workflows', { method: 'PUT', body: { card_types: WF } });
      toast(r.problems && r.problems.length ? 'saved with ' + r.problems.length + ' warning(s)' : 'saved');
      showProblems(r.problems || []);
      refresh();
    } catch (e) { toast(e.message); }
  };
  showProblems(S.validation || []);
}

function showProblems(problems) {
  const el = $('#wfProblems');
  if (!el) return;
  el.innerHTML = problems.length
    ? '<div class="problems"><b>' + problems.length + ' problem(s)</b> — saved anyway, but these ' +
      'pipelines won\'t behave:<ul>' + problems.map(p => '<li>' + esc(p) + '</li>').join('') + '</ul></div>'
    : '';
}

/* -------------------------------------------------------------------- log */

function kindChip(kind) {
  if (/fail|reject|deadletter|error|cancel/.test(kind)) return 'stop';
  if (/block|defer|escalat|open|pause|retry/.test(kind)) return 'warn';
  if (/pass|done|approve|merge|complete|accept/.test(kind)) return 'ok';
  return '';
}

async function renderLog() {
  let d;
  try { d = await api('/api/log?limit=250'); } catch (e) { return toast(e.message); }
  $('#log').innerHTML = d.events.length ? d.events.map(e => {
    const data = (() => { try { return JSON.parse(e.data); } catch (_) { return {}; } })();
    const bits = Object.entries(data)
      .filter(([, v]) => v !== null && v !== '')
      .map(([k, v]) => k + '=' + esc(String(typeof v === 'object' ? JSON.stringify(v) : v).slice(0, 90)))
      .join('  ');
    return '<div class="logline">' +
      '<span class="t" title="' + esc(when(e.ts)) + '">' +
        esc(new Date(e.ts * 1000).toLocaleTimeString()) + '</span>' +
      '<span class="k"><span class="chip ' + kindChip(e.kind) + '">' + esc(e.kind) + '</span></span>' +
      '<span class="who">' + (e.task_id ? idLink(e.task_id) : '') + '</span>' +
      '<span class="d" title="' + esc(String(e.data || '').slice(0, 400)) + '">' + bits + '</span>' +
    '</div>';
  }).join('') : '<div class="item muted">Nothing has happened yet.</div>';
}

/* --------------------------------------------------------------- new card */

function newCard() {
  const types = Object.keys(S.workflows);
  $('#sheet').innerHTML =
    '<h3>New card</h3>' +
    '<p class="sub">A card is a unit of work with a brief and a way to tell whether it is done.</p>' +
    '<label class="f"><span>title</span><input type="text" id="nTitle" placeholder="what needs to happen"></label>' +
    '<div class="row">' +
      '<label class="f"><span>card type</span><select id="nType">' + types.map(t =>
        '<option value="' + esc(t) + '">' + esc(S.workflows[t].label || t) + '</option>').join('') +
      '</select></label>' +
      '<label class="f"><span>priority</span><input type="number" id="nPri" value="50"></label>' +
      '<label class="f"><span>model</span>' +
        '<input type="text" id="nModel" list="modelnames" placeholder="stage default">' +
        '<datalist id="modelnames">' + MODELS.map(m =>
          '<option value="' + esc(m) + '">').join('') + '</datalist></label>' +
      '<label class="f"><span>parent (optional)</span><input type="text" id="nParent" placeholder="t_xxxxxx"></label>' +
    '</div>' +
    '<label class="f"><span class="plain">brief — this is the literal prompt the agent receives</span>' +
      '<textarea id="nBrief" rows="6"></textarea></label>' +
    '<label class="f"><span class="plain">acceptance criteria, one per line — at least one should be runnable</span>' +
      '<textarea id="nAcc" rows="3" placeholder="pytest tests/test_auth.py passes"></textarea></label>' +
    '<label class="f"><span class="plain">scope globs, one per line (enforced by the diff_scope gate)</span>' +
      '<textarea id="nScope" rows="2" placeholder="src/auth/**"></textarea></label>' +
    '<label class="f"><span class="plain">depends on (comma separated card ids)</span>' +
      '<input type="text" id="nDeps" placeholder="t_abc123, t_def456"></label>' +
    '<div class="actions">' +
      '<button class="btn primary" id="nCreateStart">Create &amp; start</button>' +
      '<button class="btn" id="nCreate">Create in backlog</button>' +
      '<button class="btn ghost" id="nCancel">Cancel</button>' +
    '</div>';
  $('#modal').classList.remove('hide');
  $('#nTitle').focus();

  const lines = id => $('#' + id).value.split('\n').map(s => s.trim()).filter(Boolean);
  const make = async start => {
    const title = $('#nTitle').value.trim();
    if (!title) return toast('a card needs a title');
    try {
      await api('/api/task', {
        method: 'POST',
        body: {
          title, brief: $('#nBrief').value, card_type: $('#nType').value,
          priority: Number($('#nPri').value) || 50,
        model: $('#nModel').value.trim() || null,
          parent_id: $('#nParent').value.trim() || null,
          acceptance: lines('nAcc'), scope: lines('nScope'),
          depends_on: $('#nDeps').value.split(',').map(s => s.trim()).filter(Boolean),
          start,
        },
      });
      $('#modal').classList.add('hide');
      toast(start ? 'card created and started' : 'card created in backlog');
      refresh();
    } catch (e) { toast(e.message); }
  };
  $('#nCreateStart').onclick = () => make(true);
  $('#nCreate').onclick = () => make(false);
  $('#nCancel').onclick = () => $('#modal').classList.add('hide');
}

/* ------------------------------------------------------------------- wire */

$$('nav.tabs button').forEach(b => b.onclick = () => {
  TAB = b.dataset.tab;
  $$('nav.tabs button').forEach(x => {
    const on = x === b;
    x.classList.toggle('on', on);
    x.setAttribute('aria-selected', String(on));
  });
  $$('.pane').forEach(p => p.classList.toggle('on', p.id === 'pane-' + TAB));
  refresh();
});

function describeWork() {
  $('#sheet').innerHTML =
    '<h3>Describe what you want</h3>' +
    '<p class="sub">Direction, not a task list. A planner reads the repo and ' +
    'proposes the cards; you approve the plan before anything is built.</p>' +
    '<label class="f"><span class="plain">What should change, and why</span>' +
      '<textarea id="iText" rows="10" placeholder="Rate limiting on the public API.&#10;&#10;Per API key, not per IP — we have customers behind shared NAT. Existing endpoints must keep their latency budget. I do not care which algorithm as long as bursts are handled sanely."></textarea></label>' +
    '<div class="actions">' +
      '<button class="btn primary" id="iGo">Propose a plan</button>' +
      '<button class="btn ghost" id="iCancel">Cancel</button>' +
    '</div>';
  $('#modal').classList.remove('hide');
  $('#iText').focus();

  $('#iCancel').onclick = () => $('#modal').classList.add('hide');
  $('#iGo').onclick = async () => {
    const text = $('#iText').value.trim();
    if (!text) return toast('describe what you want first');
    const btn = $('#iGo');
    btn.disabled = true;
    btn.textContent = 'Reading the repo…';
    try {
      const r = await api('/api/intent', { method: 'POST', body: { text } });
      $('#modal').classList.add('hide');
      toast('planning ' + r.id + ' — it will appear in Needs You for approval');
      refresh();
    } catch (err) {
      toast(err.message);
      btn.disabled = false;
      btn.textContent = 'Propose a plan';
    }
  };
}

$('#newBtn').onclick = newCard;
$('#intentBtn').onclick = describeWork;
$('#pauseBtn').onclick = async () => {
  try {
    await api('/api/scheduler', { method: 'POST', body: { paused: !S.scheduler.paused } });
    refresh();
  } catch (e) { toast(e.message); }
};
$('#wfAdd').onclick = () => {
  const id = prompt('card type id (e.g. "docs")');
  if (!id) return;
  WF[id] = { label: id, color: '#6B7A75', stages: [] };
  WFSEL = id;
  renderWorkflows();
};
$('#wfExport').onclick = () => { window.location = '/api/workflows/export'; };
$('#wfImport').onclick = () => $('#wfFile').click();
$('#wfFile').onchange = async e => {
  const f = e.target.files[0];
  if (!f) return;
  const text = await f.text();
  try {
    const j = JSON.parse(text);
    const r = await api('/api/workflows/import', { method: 'POST', body: j });
    WF = null;
    toast(r.problems && r.problems.length ? 'imported with ' + r.problems.length + ' warning(s)' : 'imported');
    refresh();
  } catch (err) { toast('bad workflow file: ' + err.message); }
  finally { e.target.value = ''; }
};
$('#modal').onclick = e => { if (e.target.id === 'modal') $('#modal').classList.add('hide'); };

/* One delegated listener instead of inline onclick= handlers, so no id ever
   has to be interpolated into a string of JavaScript. */
document.addEventListener('click', e => {
  const el = e.target && e.target.closest ? e.target.closest('[data-act]') : null;
  if (!el) return;
  const act = el.dataset.act;
  if (act === 'open') { e.preventDefault(); openDrawer(el.dataset.id); }
  else if (act === 'respond') respond(el.dataset.id, el.dataset.response);
  else if (act === 'decide') decide(el.dataset.id, el.dataset.decision);
});

document.addEventListener('keydown', e => {
  if (e.metaKey || e.ctrlKey || e.altKey) return;
  if (e.key === 'Escape') {
    const modal = $('#modal');
    if (!modal.classList.contains('hide')) modal.classList.add('hide');
    else closeDrawer();
    return;
  }
  const a = document.activeElement;
  const typing = a && (/^(INPUT|TEXTAREA|SELECT)$/.test(a.tagName) || a.isContentEditable);
  if (e.key === 'n' && !typing && $('#modal').classList.contains('hide')) {
    e.preventDefault();
    newCard();
  }
});

let es;
function connect() {
  es = new EventSource('/api/events');
  let pending = null;
  es.onmessage = () => { clearTimeout(pending); pending = setTimeout(refresh, 250); };
  es.onerror = () => { es.close(); setTimeout(connect, 3000); };
}

/* handy from the console, and kept for anything that still calls them */
window.openDrawer = openDrawer;
window.respond = respond;
window.decide = decide;

wireFilters();
refresh();
connect();
setInterval(refresh, 15000);
