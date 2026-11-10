// Classic (non-module) script loaded in <head> so the saved theme applies before first paint.
(function () {
  var KEY = 'dba-theme';
  var root = document.documentElement;
  function read() { try { return localStorage.getItem(KEY) || 'auto'; } catch (e) { return 'auto'; } }
  function apply(t) {
    if (t === 'light' || t === 'dark') root.setAttribute('data-theme', t); else root.removeAttribute('data-theme');
  }
  var theme = read();
  apply(theme);
  document.addEventListener('DOMContentLoaded', function () {
    var btn = document.getElementById('theme-toggle');
    if (!btn) return;
    function label() { btn.textContent = 'Theme: ' + theme; }
    label();
    btn.addEventListener('click', function () {
      theme = theme === 'auto' ? 'light' : theme === 'light' ? 'dark' : 'auto';
      apply(theme);
      try { if (theme === 'auto') localStorage.removeItem(KEY); else localStorage.setItem(KEY, theme); } catch (e) { /* private mode */ }
      label();
    });
  });
})();
