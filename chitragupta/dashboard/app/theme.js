// Appearance before first paint: URL override (not stored) > saved choice > Night ink.
// Density and reduced motion are per-browser choices too.
(function () {
  var root = document.documentElement;
  function get(k) { try { return localStorage.getItem('cg-' + k); } catch (e) { return null; } }
  var t = null;
  try { var m = /[?&]theme=(light|dark)\b/.exec(location.search); if (m) t = m[1]; } catch (e) { /* ignore */ }
  t = t || get('theme') || 'dark';
  if (t !== 'light' && t !== 'dark' && t !== 'system') t = 'dark';
  root.setAttribute('data-theme', t);
  function tint() {
    var mode = root.getAttribute('data-theme');
    var dark = mode === 'dark' || (mode === 'system' && window.matchMedia && window.matchMedia('(prefers-color-scheme: dark)').matches);
    var meta = document.querySelector('meta[name="theme-color"]');
    if (meta) meta.setAttribute('content', dark ? '#0F1216' : '#FAFAF8');
  }
  tint();
  if (window.matchMedia) window.matchMedia('(prefers-color-scheme: dark)').addEventListener('change', tint);
  var d = get('density');
  if (d === 'compact') root.setAttribute('data-density', d);
  var mo = get('motion');
  if (mo === 'reduce' || mo === 'full') root.setAttribute('data-motion', mo);
})();
