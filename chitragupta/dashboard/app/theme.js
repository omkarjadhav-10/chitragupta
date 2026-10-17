// Appearance before first paint (classic script, runs in <head>): ?theme= (not stored) > saved choice > the system.
// Density and reduced motion are per-browser choices too.
(function () {
  var root = document.documentElement;
  function get(k) { try { return localStorage.getItem('cg-' + k); } catch (e) { return null; } }
  var t = null;
  try { var m = /[?&]theme=(light|dark)\b/.exec(location.search); if (m) t = m[1]; } catch (e) { /* ignore */ }
  t = t || get('theme');
  if (t === 'light' || t === 'dark') root.setAttribute('data-theme', t);
  var d = get('density');
  if (d === 'compact') root.setAttribute('data-density', d);
  var mo = get('motion');
  if (mo === 'reduce' || mo === 'full') root.setAttribute('data-motion', mo);
})();
