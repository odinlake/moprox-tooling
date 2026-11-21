/* mo.lan site menu — SINGLE SOURCE OF TRUTH (across BOTH the mo.lan static site AND the
   dash.odinlake.net dashboard). Add / rename / regroup items HERE only.
   - The mo.lan pages serve this at /menu.js.
   - The dashboard (separate repo/origin) publishes this SAME file to /dashboard/menu.js:
     moprox-tooling services/update.py copies it from ~/projects/private-web/site/mo/menu.js.
   Any page with <button id="bbtn"> + <nav id="bpop"> that loads this renders the identical menu.
   Hrefs are ABSOLUTE so the file is origin-portable; the current page is highlighted by matching
   host+path (scheme-independent), so external links never falsely light up. */
(function () {
  var TOP = { href: 'https://mo.lan/', label: 'mo.lan home' };   // ungrouped, pinned on top
  // Four short groups instead of one long list (operator, 2026-10-02: "grown unwieldy"). `desc` and
  // `gate` also drive the home page's tiles (index.html renders from window.MO_MENU), so the home
  // page and the menu cannot drift apart again. gate: "login" = Authelia; "lan" = open on the LAN;
  // "public" = on the internet (dash.odinlake.net).
  var GROUPS = [
    { name: 'Search', items: [
      { href: 'https://mo.lan/every/',  label: 'Everything',    gate: 'login', desc: 'mail, documents, news, finance and notifications at once' },
      { href: 'https://mo.lan/mail/',   label: 'Mail',          gate: 'login', desc: 'every email, hybrid search' },
      { href: 'https://mo.lan/docs/',   label: 'Documents',     gate: 'login', desc: 'Drive documents, summaries and tags' }
    ] },
    { name: 'Life', items: [
      { href: 'https://mo.lan/finance/', label: 'Finance',       gate: 'login', desc: 'spending dashboard' },
      { href: 'https://mo.lan/notif/',   label: 'Notifications', gate: 'login', desc: 'phone notifications captured by Home Assistant' },
      { href: 'https://mo.lan/reader/',  label: 'Local news',    gate: 'lan',   desc: 'Nextdoor distilled, with AI blurbs' },
      { href: 'https://dash.odinlake.net/dashboard/training/', label: 'Training', gate: 'public', desc: 'runs, rides and strength sessions' },
      { href: 'https://mo.lan/yoto/',    label: 'Yoto',          gate: 'lan',   desc: 'pick stories to play; listening stats' }
    ] },
    { name: 'Estate', items: [
      { href: 'https://dash.odinlake.net/dashboard/system/', label: 'System', gate: 'public', desc: 'hypervisor and guest stats' },
      { href: 'https://dash.odinlake.net/dashboard/agents/', label: 'Agents', gate: 'public', desc: 'agent activity and MCP usage' },
      { href: 'https://mo.lan/incidents/', label: 'Incidents',   gate: 'login', desc: 'machine-detected problems across the fleet' },
      { href: 'https://mo.lan/issues/',    label: 'Issues',      gate: 'login', desc: 'the curated work list: owner and next step' },
      { href: 'https://mo.lan/logs/',      label: 'Logs',        gate: 'login', desc: 'whole-fleet journal, search and live tail' }
    ] },
    { name: 'Tools', items: [
      { href: 'https://mo.lan/inventory/', label: 'Process inventory', gate: 'login', desc: 'scheduled and always-on processes' },
      { href: 'https://mo.lan/docs/mcp/',  label: 'MCP & API',         gate: 'login', desc: 'every server and tool, generated' },
      { href: 'https://mo.lan/tty/',       label: 'Terminal',          gate: 'login', desc: 'shell on claude-dev, persistent tmux' },
      { href: 'https://mo.lan/claude-login/', label: 'Claude login',   gate: 'login', desc: 'sign each box in to Claude' }
    ] }
  ];
  window.MO_MENU = { top: TOP, groups: GROUPS };

  function norm(p) { var n = p.replace(/index\.html$/, '').replace(/\/+$/, ''); return n || '/'; }
  function isCur(href) {
    var u; try { u = new URL(href, location.href); } catch (e) { return false; }
    if (u.host !== location.host) return false;                     // other origin (mo.lan <-> dash) never "current"
    if (norm(u.pathname) !== norm(location.pathname)) return false;
    return u.hash ? (location.hash === u.hash) : !location.hash;    // /mail/ vs /mail/#docs
  }
  function esc(s) { return String(s).replace(/[&<>"]/g, function (c) { return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]; }); }
  function link(it) { return '<a href="' + esc(it.href) + '"' + (isCur(it.href) ? ' class="cur"' : '') + '>' + esc(it.label) + '</a>'; }

  function render() {
    var nav = document.getElementById('bpop');
    if (!nav) return;
    var html = '<div class="btop">' + link(TOP) + '</div>';
    GROUPS.forEach(function (g) {                                   // one column per group on wide screens
      html += '<div class="bcol"><div class="bgroup sep">' + esc(g.name) + '</div>';
      g.items.forEach(function (it) { html += link(it); });
      html += '</div>';
    });
    nav.innerHTML = html;

    if (!document.getElementById('bpop-menu-css')) {                // group-label styling, injected once
      var st = document.createElement('style'); st.id = 'bpop-menu-css';
      st.textContent =                                              // var fallbacks so it works in both apps' palettes
        '#bpop .bgroup{font-size:.64rem;text-transform:uppercase;letter-spacing:.05em;color:var(--muted,var(--mut,#8a8a8a));font-weight:600;padding:.35rem .7rem .1rem}' +
        '#bpop .bgroup.sep{margin-top:.2rem;border-top:1px solid var(--border,var(--bd,rgba(128,128,128,.25)));padding-top:.4rem}' +
        '#bpop .bcol,#bpop .btop{display:flex;flex-direction:column}' +
        '#bpop{max-height:calc(100vh - 5rem);overflow:auto}' +
        // two columns (Search+Life | Estate+Tools), phones included, so the menu is half as tall
        '@media (min-width:360px){#bpop:not([hidden]){display:grid!important;grid-template-columns:repeat(2,minmax(140px,auto));column-gap:.5rem;min-width:300px}' +
        '#bpop .btop{grid-column:1/-1}}';
      document.head.appendChild(st);
    }
    var b = document.getElementById('bbtn');                        // toggle open/close
    if (b) {
      b.onclick = function (e) { e.stopPropagation(); nav.hidden = !nav.hidden; };
      document.addEventListener('click', function () { nav.hidden = true; });
    }
  }
  /* Reload button. Pages added to the iOS home screen run standalone — no address bar, no pull-to-
     refresh, no way to reload at all. Injected from here so every page that loads menu.js gets it,
     including the dashboard, without touching each page.
     Bottom-right rather than mirroring the menu at top-left: it is thumb-reachable on a phone, and
     the top-right corner is already occupied on some pages (logview's filter bar runs the full
     width). Colours use the same var-with-fallback chain as the menu CSS above so it works in both
     palettes and in either theme. */
  function reloadButton() {
    if (document.getElementById('mo-reload')) return;
    var st = document.createElement('style'); st.id = 'mo-reload-css';
    st.textContent =
      '#mo-reload{position:fixed;z-index:98;right:calc(.8rem + env(safe-area-inset-right,0px));' +
      'bottom:calc(.8rem + env(safe-area-inset-bottom,0px));width:40px;height:40px;border-radius:50%;' +
      'display:inline-flex;align-items:center;justify-content:center;cursor:pointer;' +
      'background:var(--panel,var(--bg,#fff));color:var(--muted,var(--mut,var(--dim,#6b7280)));' +
      'border:1px solid var(--border,var(--bd,var(--line,rgba(128,128,128,.35))));' +
      'box-shadow:0 2px 10px rgba(0,0,0,.18);opacity:.75;transition:opacity .15s,color .15s;' +
      '-webkit-tap-highlight-color:transparent}' +
      '#mo-reload:hover,#mo-reload:focus-visible{opacity:1;color:var(--fg,#111)}' +
      '#mo-reload svg{width:19px;height:19px;display:block}' +
      '#mo-reload.spin svg{animation:mo-spin .6s linear infinite}' +
      '@keyframes mo-spin{to{transform:rotate(360deg)}}' +
      '@media print{#mo-reload{display:none}}';
    document.head.appendChild(st);

    var b = document.createElement('button');
    b.id = 'mo-reload'; b.type = 'button';
    b.setAttribute('aria-label', 'Reload this page');
    b.title = 'Reload';
    b.innerHTML =
      '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.1" ' +
      'stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">' +
      '<path d="M20.5 12a8.5 8.5 0 1 1-2.6-6.1"/><path d="M20.5 4.2V10h-5.8"/></svg>';
    b.addEventListener('click', function () {
      b.classList.add('spin');            // the reload can take a moment; show it registered
      /* Cache-bust without touching the URL. location.reload() honours the HTTP cache, and these
         pages ship no Cache-Control — only ETag/Last-Modified — so browsers apply HEURISTIC
         freshness (~10% of the age since Last-Modified) and can skip revalidation for hours. That
         is the staleness you see in a standalone home-screen app.
         fetch(cache:'reload') goes to the network unconditionally AND writes the result back into
         the HTTP cache, so the reload that follows renders the fresh copy. Preferred over appending
         ?_=timestamp, which would linger in the URL and in anything bookmarked or shared.
         credentials matter: every gated page 302s to Authelia without the session cookie, and
         caching THAT would be worse than the staleness we came to fix. */
      var fired = false;
      var go = function () { if (!fired) { fired = true; location.reload(); } };
      setTimeout(go, 3000);               // never leave it spinning on a stalled network
      try { fetch(location.href, { cache: 'reload', credentials: 'same-origin' }).then(go, go); }
      catch (e) { go(); }
    });
    document.body.appendChild(b);
  }

  function init() { render(); reloadButton(); }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init); else init();
})();
