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
  var GROUPS = [
    { name: 'Personal', items: [
      { href: 'https://mo.lan/every/',     label: 'Search' },
      { href: 'https://mo.lan/finance/',   label: 'Finance' },
      { href: 'https://mo.lan/reader/',    label: 'Local news' },
      { href: 'https://mo.lan/notif/',     label: 'Notifications' },
      { href: 'https://dash.odinlake.net/dashboard/training/', label: 'Training' }
    ] },
    { name: 'System', items: [
      { href: 'https://dash.odinlake.net/dashboard/system/', label: 'System' },
      { href: 'https://dash.odinlake.net/dashboard/agents/', label: 'Agents' },
      { href: 'https://mo.lan/inventory/', label: 'Process inventory' }
    ] }
  ];

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
    var html = link(TOP);
    GROUPS.forEach(function (g) {
      html += '<div class="bgroup sep">' + esc(g.name) + '</div>';
      g.items.forEach(function (it) { html += link(it); });
    });
    nav.innerHTML = html;

    if (!document.getElementById('bpop-menu-css')) {                // group-label styling, injected once
      var st = document.createElement('style'); st.id = 'bpop-menu-css';
      st.textContent =                                              // var fallbacks so it works in both apps' palettes
        '#bpop .bgroup{font-size:.64rem;text-transform:uppercase;letter-spacing:.05em;color:var(--muted,var(--mut,#8a8a8a));font-weight:600;padding:.35rem .7rem .1rem}' +
        '#bpop .bgroup.sep{margin-top:.2rem;border-top:1px solid var(--border,var(--bd,rgba(128,128,128,.25)));padding-top:.4rem}';
      document.head.appendChild(st);
    }
    var b = document.getElementById('bbtn');                        // toggle open/close
    if (b) {
      b.onclick = function (e) { e.stopPropagation(); nav.hidden = !nav.hidden; };
      document.addEventListener('click', function () { nav.hidden = true; });
    }
  }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', render); else render();
})();
