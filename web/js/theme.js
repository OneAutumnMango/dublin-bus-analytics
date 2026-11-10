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
  var labelFn = function () {};
  function set(t) {
    theme = t; apply(t);
    try { if (t === 'auto') localStorage.removeItem(KEY); else localStorage.setItem(KEY, t); } catch (e) { /* private mode */ }
    labelFn();
  }
  window.dbaTheme = { get: function () { return theme; }, set: set };
  document.addEventListener('DOMContentLoaded', function () {
    var btn = document.getElementById('theme-toggle');
    if (!btn) return;
    function label() {
      var names = { auto: 'Auto', light: 'Light', dark: 'Dark' }, icons = { auto: '\u25D0', light: '\u2600', dark: '\u263E' };
      btn.textContent = icons[theme] + ' ' + names[theme];
      btn.setAttribute('aria-label', 'Theme: ' + names[theme].toLowerCase() + '. Click to change.');
    }
    labelFn = label;
    label();
    btn.addEventListener('click', function () {
      set(theme === 'auto' ? 'light' : theme === 'light' ? 'dark' : 'auto');
    });
  });
})();
