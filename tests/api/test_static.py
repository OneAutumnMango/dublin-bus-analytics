"""Static web UI: serving, headers, no shadowing of /api, import-time purity, static checks of the assets."""
import re
import subprocess
import sys
from html.parser import HTMLParser
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.core.config import Settings
from app.main import create_app

WEB = Path(__file__).resolve().parent.parent.parent / "web"
CSP = ("default-src 'self'; img-src 'self' data: blob: https://tiles.openfreemap.org; "
       "connect-src 'self' https://tiles.openfreemap.org; worker-src 'self' blob:; "
       "style-src 'self'; script-src 'self'; "
       "frame-ancestors 'none'; base-uri 'none'; form-action 'self'")


@pytest.fixture
def client(tmp_path):
    return TestClient(create_app(Settings("http://x", tmp_path), start_background=False))


@pytest.mark.parametrize("path,ctype", [("/", "text/html"), ("/index.html", "text/html"),
                                        ("/js/app.js", "javascript"), ("/js/lib.js", "javascript"),
                                        ("/css/style.css", "text/css")])
def test_static_served_with_content_type(client, path, ctype):
    r = client.get(path)
    assert r.status_code == 200, path
    assert ctype in r.headers["content-type"]
    assert r.content == (WEB / ("index.html" if path == "/" else path.lstrip("/"))).read_bytes()


@pytest.mark.parametrize("path", ["/", "/js/app.js", "/css/style.css"])
def test_security_headers_on_static(client, path):
    h = client.get(path).headers
    assert h["content-security-policy"] == CSP
    assert h["x-content-type-options"] == "nosniff"
    assert h["referrer-policy"] == "no-referrer"


def test_api_not_shadowed(client):
    assert client.get("/api/health").json() == {"ok": True}
    r = client.get("/api/nope")
    assert r.status_code == 404
    assert r.headers["content-type"].startswith("application/json")
    assert "<html" not in r.text.lower()
    assert client.get("/api/routes/tracked").json() == {"routes": []}
    assert client.get("/api/backup/download").status_code == 404  # the API's own 404, not index.html
    assert client.get("/api/routes/15/summary?ci=2").status_code == 422


def test_unknown_static_path_404_and_traversal(client):
    assert client.get("/nope.js").status_code == 404
    assert client.get("/js/").status_code == 404
    assert client.get("/%2e%2e/app/main.py").status_code == 404
    assert "create_app" not in client.get("/js/../../app/main.py").text


def test_post_to_static_is_rejected(client):
    assert client.post("/").status_code == 405


def test_web_dir_env_override_and_missing_dir(tmp_path, monkeypatch):
    (tmp_path / "web").mkdir()
    (tmp_path / "web" / "index.html").write_text("<h1>x</h1>")
    monkeypatch.setenv("WEB_DIR", str(tmp_path / "web"))
    c = TestClient(create_app(Settings("http://x", tmp_path / "d"), start_background=False))
    assert c.get("/").text == "<h1>x</h1>"
    monkeypatch.setenv("WEB_DIR", str(tmp_path / "missing"))
    c = TestClient(create_app(Settings("http://x", tmp_path / "d"), start_background=False))
    assert c.get("/").status_code == 404
    assert c.get("/api/health").status_code == 200


def test_import_main_does_no_io_with_web_and_without_data_dir():
    code = """
import sys, os
os.environ['DATA_DIR'] = '/nonexistent/data-dir-for-test'
events = []
def hook(ev, args):
    if ev in ('socket.connect', 'os.mkdir', 'sqlite3.connect') or (ev == 'open' and len(args) > 1 and args[1] and any(c in str(args[1]) for c in 'wax+')):
        events.append((ev, args))
    if ev in ('os.listdir', 'os.scandir') and any(s in str(args) for s in ('dublin-bus-analytics/web', 'data-dir-for-test')):
        events.append((ev, args))
sys.addaudithook(hook)
import app.main
assert not events, events
print('ok')
"""
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, cwd=WEB.parent, timeout=60)
    assert out.stdout.strip() == "ok", out.stderr + out.stdout
    assert not Path("/nonexistent").exists()


# ---- static checks of the assets ----

class _Index(HTMLParser):
    def __init__(self):
        super().__init__()
        self.tags, self.attrs, self.inline_script, self._in_script, self._in_style = [], [], False, False, False
        self.inline_style_tag = False

    def handle_starttag(self, tag, attrs):
        self.tags.append(tag)
        self.attrs.append((tag, dict(attrs)))
        if tag == "script" and "src" not in dict(attrs):
            self.inline_script = True
        if tag == "style":
            self.inline_style_tag = True


def _index():
    p = _Index()
    p.feed((WEB / "index.html").read_text())
    return p


def test_index_landmarks_lang_viewport():
    p = _index()
    html = dict(p.attrs[p.tags.index("html")][1])
    assert html.get("lang") == "en"
    for t in ("header", "nav", "main", "footer", "h1", "title"):
        assert t in p.tags, t
    assert p.tags.count("h1") == 1
    metas = [a for t, a in p.attrs if t == "meta"]
    assert any(m.get("name") == "viewport" and "width=device-width" in m.get("content", "") for m in metas)
    assert any(a.get("role") == "status" or a.get("aria-live") for t, a in p.attrs)
    assert any(a.get("type") == "module" and a.get("src") for t, a in p.attrs if t == "script")


def test_index_same_origin_and_no_inline_code():
    text = (WEB / "index.html").read_text()
    p = _index()
    assert not re.search(r"https?://|//cdn", text)
    assert not p.inline_script and not p.inline_style_tag
    for t, a in p.attrs:
        assert "style" not in a, t
        assert not [k for k in a if k.startswith("on")], (t, a)
        for k in ("src", "href"):
            v = a.get(k, "")
            assert not v.startswith(("http:", "https:", "//")), v


def test_css_and_js_have_no_remote_urls():
    for f in list((WEB / "css").glob("*.css")) + list((WEB / "js").rglob("*.js")):
        t = f.read_text()
        assert not re.search(r"""(?:url\(|import\s|from\s|src=|href=)\s*['"]?https?:""", t), f


def test_js_imports_resolve():
    files = list((WEB / "js").rglob("*.js"))
    assert files
    for f in files:
        for m in re.finditer(r"""(?:from|import)\s*\(?\s*['"]([^'"]+)['"]""", f.read_text()):
            spec = m.group(1)
            assert spec.startswith(("./", "../")), (f, spec)  # same-origin relative modules only
            assert (f.parent / spec).resolve().is_file(), (f, spec)
    html = (WEB / "index.html").read_text()
    for ref in re.findall(r"""(?:src|href)="(/[^"]+)\"""", html):
        assert (WEB / ref.lstrip("/")).is_file(), ref


def test_forbidden_dom_apis_not_used_in_js():
    pat = re.compile(r"innerHTML|outerHTML|insertAdjacentHTML|\beval\s*\(|new Function|document\.write|setAttribute\(\s*['\"]on|\.on[a-z]+\s*=[^=]|srcdoc|javascript:")
    for f in (WEB / "js").rglob("*.js"):
        for i, line in enumerate(f.read_text().splitlines(), 1):
            assert not pat.search(line), f"{f}:{i}: {line.strip()}"


def test_js_modules_pass_node_check():
    node = subprocess.run(["node", "--version"], capture_output=True)
    if node.returncode != 0:
        pytest.skip("node not installed")
    for f in (WEB / "js").rglob("*.js"):
        r = subprocess.run(["node", "--check", str(f)], capture_output=True, text=True)
        assert r.returncode == 0, f"{f}: {r.stderr}"


@pytest.mark.parametrize("path", ["/package.json", "/test/fakedom.mjs", "/test/lib.test.mjs", "/js", "/css/", "/nope"])
def test_only_index_css_js_are_served(client, path):
    assert client.get(path).status_code == 404


def test_error_responses_carry_security_headers(client):
    for r in (client.get("/nope"), client.post("/"), client.get("/package.json"), client.get("/api/nope")):
        assert r.status_code in (404, 405)
        assert r.headers["content-security-policy"] == CSP
        assert r.headers["x-content-type-options"] == "nosniff"
    assert client.get("/api/nope").json() == {"detail": "Not Found"}


def test_static_responses_revalidate(client):
    for p in ("/", "/js/app.js", "/css/style.css"):
        assert client.get(p).headers["cache-control"] == "no-cache"


@pytest.mark.parametrize("path,ctype", [("/fonts/inter-400.ttf", "font"), ("/vendor/maplibre/maplibre-gl-csp.js", "javascript"),
                                        ("/vendor/maplibre/maplibre-gl-csp-worker.js", "javascript"),
                                        ("/vendor/maplibre/maplibre-gl.css", "text/css")])
def test_fonts_and_vendored_map_library_are_served(client, path, ctype):
    r = client.get(path)
    assert r.status_code == 200, path
    assert ctype in r.headers["content-type"], r.headers["content-type"]
    assert r.headers["content-security-policy"] == CSP


def test_other_top_level_dirs_still_404(client):
    for p in ("/vendor", "/vendor/../app/main.py", "/test/mappage.test.mjs", "/docs/plans/x.md"):
        assert client.get(p).status_code == 404, p
