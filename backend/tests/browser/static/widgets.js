// Test-only dropdown that can behave like most real ones (and like the broken ones).
// The TRUE choice is only written to the hidden input name=<name> when it's really committed,
// so tests can check what the site would receive, not what the box seems to show.
//
// makeDropdown(container, {
//   name, label, options: [..] | {Category: [..]},   // object = nested categories
//   mode: 'button' | 'input',      // clickable box, or a typeahead text input
//   aria: true,                    // false = plain divs with no roles at all
//   search: 'none' | 'popup',      // popup = search box inside the opened menu
//   portal: false,                 // menu appended to <body>, away from the control
//   keyboardOnly: false,           // clicks only highlight; Enter commits (ArrowDown/Enter menus)
//   failOpen: 0,                   // first N open attempts are ignored
//   revert: 0,                     // first N commits are undone by the "site" 300ms later
//   asyncMs: 0,                    // results arrive after a delay (shows "Searching...")
//   clearOnBlur: true,             // input mode: uncommitted typing is wiped on blur
//   multi: false,                  // chips; menu stays open
//   neverCommit: false,            // a broken widget: nothing ever sticks
// })
function makeDropdown(container, o) {
  o = Object.assign({mode: 'button', aria: true, search: 'none', portal: false, keyboardOnly: false, failOpen: 0,
                     revert: 0, asyncMs: 0, clearOnBlur: true, multi: false, neverCommit: false}, o);
  const id = 'dd_' + o.name;
  const wrap = document.createElement('div');
  wrap.className = 'field';
  const lab = document.createElement('label');
  lab.id = id + '_lbl'; lab.textContent = o.label; lab.htmlFor = id;
  wrap.appendChild(lab);
  const hidden = document.createElement('input');
  hidden.type = 'hidden'; hidden.name = o.name;
  let ctl, shown;
  if (o.mode === 'input') {
    ctl = document.createElement('input');
    ctl.id = id; ctl.autocomplete = 'off';
    shown = document.createElement('div'); shown.className = 'single-value';
    if (o.aria) { ctl.setAttribute('role', 'combobox'); ctl.setAttribute('aria-autocomplete', 'list'); }
    const row = document.createElement('div'); row.className = 'ctlrow';
    row.appendChild(shown); row.appendChild(ctl); wrap.appendChild(row);
  } else {
    ctl = document.createElement('div');
    ctl.id = id; ctl.tabIndex = 0; ctl.className = 'ctl'; ctl.textContent = 'Select...';
    if (o.aria) { ctl.setAttribute('role', 'combobox'); ctl.setAttribute('aria-haspopup', 'listbox');
                  ctl.setAttribute('aria-labelledby', lab.id); }
    shown = ctl;
    wrap.appendChild(ctl);
  }
  if (o.aria) { ctl.setAttribute('aria-expanded', 'false'); if (!o.portal) ctl.setAttribute('aria-controls', id + '_menu'); }
  const chips = document.createElement('div'); chips.className = 'chips';
  wrap.appendChild(chips); wrap.appendChild(hidden);
  container.appendChild(wrap);

  const menu = document.createElement('div');
  menu.id = id + '_menu'; menu.className = 'menu'; menu.hidden = true;
  if (o.aria) menu.setAttribute('role', 'listbox');
  (o.portal ? document.body : wrap).appendChild(menu);
  let searchBox = null;
  if (o.search === 'popup') {
    searchBox = document.createElement('input');
    searchBox.className = 'search'; searchBox.placeholder = 'Search';
    if (o.aria) searchBox.setAttribute('role', 'searchbox');
    searchBox.setAttribute('aria-label', 'Search ' + o.label);
    menu.appendChild(searchBox);
  }
  const list = document.createElement('div'); menu.appendChild(list);
  const nested = !Array.isArray(o.options);
  let level = null, items = [], hl = -1, committed = [], timer = null, query = '';
  let failOpen = o.failOpen, revert = o.revert;

  const all = () => nested ? (level ? o.options[level] : Object.keys(o.options)) : o.options;
  function render(results) {
    list.innerHTML = '';
    items = results;
    if (nested && level) { const b = document.createElement('div'); b.className = 'opt back'; b.textContent = '< Back';
      b.onclick = () => { level = null; render(all()); }; list.appendChild(b); }
    if (!results.length) { const n = document.createElement('div'); n.className = 'none'; n.textContent = 'No matches';
      list.appendChild(n); }
    results.forEach((t, i) => {
      const d = document.createElement('div');
      d.className = 'opt'; d.textContent = t; d.id = id + '_o' + i;
      if (o.aria) { d.setAttribute('role', 'option'); d.setAttribute('aria-selected', 'false'); }
      d.addEventListener('mousedown', e => e.preventDefault());
      d.onclick = () => { highlight(i); if (!o.keyboardOnly) choose(i); };
      list.appendChild(d);
    });
    hl = -1;
  }
  function highlight(i) {
    hl = i;
    [...list.querySelectorAll('.opt:not(.back)')].forEach((d, j) => {
      d.classList.toggle('hl', j === i);
      if (o.aria) d.setAttribute('aria-selected', j === i ? 'true' : 'false');   // highlighted, NOT chosen
    });
    if (o.aria && i >= 0) ctl.setAttribute('aria-activedescendant', id + '_o' + i);
  }
  function place() {
    if (!o.portal) return;
    const r = ctl.getBoundingClientRect();
    Object.assign(menu.style, {position: 'absolute', left: (r.left + scrollX) + 'px', top: (r.bottom + scrollY) + 'px',
                               width: r.width + 'px'});
  }
  function open() {
    if (failOpen > 0) { failOpen--; return; }
    if (!menu.hidden) return;
    menu.hidden = false; place();
    if (o.aria) ctl.setAttribute('aria-expanded', 'true');
    if (!o.multi || !items.length) { level = null; filter(query); }
    if (searchBox) setTimeout(() => searchBox.focus(), 0);
  }
  function close() {
    menu.hidden = true; if (o.aria) ctl.setAttribute('aria-expanded', 'false');
  }
  function filter(q) {
    const go = () => {
      const src = all();
      render(q ? src.filter(t => t.toLowerCase().includes(q.toLowerCase())) : src);
    };
    clearTimeout(timer);
    if (o.asyncMs && q) {
      list.innerHTML = '<div class="none">Searching...</div>'; items = [];
      timer = setTimeout(go, o.asyncMs);
    } else go();
  }
  function show() {
    const txt = committed.join(', ');
    if (o.multi) {
      chips.innerHTML = '';
      committed.forEach(c => { const s = document.createElement('span'); s.className = 'chip';
        s.setAttribute('data-jp-chosen', '1'); s.textContent = c; chips.appendChild(s); });
      if (o.mode !== 'input') ctl.textContent = committed.length ? committed.length + ' selected' : 'Select...';
    } else if (o.mode === 'input') { shown.textContent = txt; }
    else ctl.textContent = txt || 'Select...';
    hidden.value = committed.join('|');
  }
  function choose(i) {
    const t = items[i];
    if (t === undefined) return;
    if (nested && !level) { level = t; query = ''; if (searchBox) searchBox.value = ''; render(all()); return; }
    if (o.neverCommit) { close(); return; }
    const prev = committed.slice();
    if (o.multi) { if (!committed.includes(t)) committed.push(t); }
    else committed = [t];
    if (o.mode === 'input') { ctl.value = ''; query = ''; }
    show();
    if (!o.multi) close();
    if (revert > 0) { revert--; setTimeout(() => { committed = prev; show(); }, 300); }
  }
  const keys = e => {
    const n = items.length;
    if (e.key === 'ArrowDown') { e.preventDefault(); if (menu.hidden) open(); else highlight(Math.min(n - 1, hl + 1)); }
    else if (e.key === 'ArrowUp') { e.preventDefault(); highlight(Math.max(0, hl - 1)); }
    else if (e.key === 'Enter') { e.preventDefault(); if (menu.hidden) open(); else if (hl >= 0) choose(hl); }
    else if (e.key === ' ' && o.mode !== 'input' && e.target === ctl) { e.preventDefault(); open(); }
    else if (e.key === 'Escape') { close(); }
  };
  ctl.addEventListener('keydown', keys);
  if (searchBox) {
    searchBox.addEventListener('keydown', keys);
    searchBox.addEventListener('input', () => { query = searchBox.value; filter(query); });
  }
  ctl.addEventListener('click', () => { if (menu.hidden) open(); else if (o.mode !== 'input') close(); });
  if (o.mode === 'input') {
    ctl.addEventListener('input', () => { query = ctl.value; if (menu.hidden) open(); filter(query); });
    ctl.addEventListener('blur', () => setTimeout(() => {
      if (o.clearOnBlur && ctl.value) { ctl.value = ''; query = ''; }
      if (!menu.contains(document.activeElement)) close();
    }, 150));
  }
  document.addEventListener('mousedown', e => { if (!wrap.contains(e.target) && !menu.contains(e.target)) close(); });
  return {get value() { return hidden.value; }};
}
