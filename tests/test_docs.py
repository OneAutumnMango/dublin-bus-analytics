"""Docs stay true: env example, sample compose and README agree with app/config.py and the repo."""
import re
from pathlib import Path

import pytest

from app.config import Settings

ROOT = Path(__file__).resolve().parent.parent
README = (ROOT / "README.md").read_text()
COMPOSE_TXT = (ROOT / "docker-compose.example.yml").read_text()
CONFIG_TXT = (ROOT / "app" / "config.py").read_text()
STATIC_TXT = (ROOT / "app" / "static.py").read_text()

# app-level names read by the code: config.py via os.environ, static.py for WEB_DIR
APP_VARS = set(re.findall(r'(?:_pos_int|_bool|_hhmm|environ\.get)\(\s*"([A-Z_]+)"', CONFIG_TXT)) | {"WEB_DIR"}


def env_example():
    """(active, all_names) from analytics.env.example; commented `# NAME=value` lines count as documented."""
    active, allv = {}, {}
    for line in (ROOT / "analytics.env.example").read_text().splitlines():
        m = re.match(r"#?\s*([A-Z][A-Z_]+)=(\S*)", line)
        if m:
            allv[m[1]] = m[2]
            if not line.startswith("#"):
                active[m[1]] = m[2]
    return active, allv


def test_app_vars_detected():
    assert {"SERVER_URL", "DATA_DIR", "POLL_SECONDS", "PORT", "BACKUP_ENABLED", "BACKUP_TIME",
            "BACKUP_KEEP", "BACKUP_DIR", "WEB_DIR"} == APP_VARS
    assert "WEB_DIR" in STATIC_TXT


def test_env_example_app_level_names_parse_into_settings(monkeypatch):
    _, allv = env_example()
    app_level = {k: v for k, v in allv.items() if not k.startswith("ANALYTICS_")}
    assert set(app_level) == APP_VARS  # all documented, none invented
    for k, v in app_level.items():
        monkeypatch.setenv(k, v)
    s = Settings.from_env()
    assert (s.server_url, str(s.data_dir), s.poll_seconds, s.port) == (
        app_level["SERVER_URL"], app_level["DATA_DIR"], int(app_level["POLL_SECONDS"]), int(app_level["PORT"]))
    assert (s.backup_enabled, s.backup_time, s.backup_keep, str(s.backup_dir)) == (
        True, app_level["BACKUP_TIME"], int(app_level["BACKUP_KEEP"]), app_level["BACKUP_DIR"])


def test_env_example_compose_names_used_and_valid(monkeypatch):
    active, _ = env_example()
    assert set(active) == {"ANALYTICS_PORT", "ANALYTICS_POLL_SECONDS", "ANALYTICS_BACKUP_ENABLED",
                           "ANALYTICS_BACKUP_TIME", "ANALYTICS_BACKUP_KEEP", "ANALYTICS_BACKUP_DIR"}
    for k in active:
        assert f"${{{k}:-{active[k]}}}" in COMPOSE_TXT
    # the values, mapped to the app names compose uses, must pass the app's validation
    for k, v in active.items():
        if k != "ANALYTICS_PORT":
            monkeypatch.setenv(k.removeprefix("ANALYTICS_"), v)
    s = Settings.from_env()
    assert s.poll_seconds == 30 and s.backup_keep == 7 and s.backup_time == "03:00"


def services():
    """{name: block text} by splitting the `services:` mapping on 2-space-indented keys (yaml is not a dependency)."""
    body = COMPOSE_TXT.split("\nservices:\n", 1)[1].split("\nvolumes:\n", 1)[0]
    parts = re.split(r"^  ([\w-]+):\n", body, flags=re.M)
    return dict(zip(parts[1::2], parts[2::2]))


def test_compose_structure():
    svc = services()
    assert set(svc) == {"dublin-bus-server", "dublin-bus-analytics"}
    a = svc["dublin-bus-analytics"]
    assert re.search(r"depends_on:\n\s+- dublin-bus-server\n", a)
    assert "- analytics-data:/data\n" in a
    assert re.search(r"^volumes:\n(?:  .*\n)*  analytics-data:", COMPOSE_TXT, re.M)
    assert re.search(r"healthcheck:\n\s+test: .*api/health", a)
    for name, block in svc.items():
        ports = re.findall(r'^\s+- "([^"]+:\d+)"$', re.search(r"ports:\n((?:\s+- .*\n)+)", block)[1], re.M)
        assert ports and all(p.startswith("127.0.0.1:") for p in ports), name
    assert "0.0.0.0" not in COMPOSE_TXT


def test_compose_environment_keys_are_read_by_the_app():
    a = services()["dublin-bus-analytics"]
    env = re.search(r"environment:\n((?:      .*\n)+)", a)[1]
    keys = set(re.findall(r"^      ([A-Z_]+):", env, re.M))
    assert keys <= APP_VARS
    assert keys >= {"SERVER_URL", "POLL_SECONDS", "BACKUP_ENABLED", "BACKUP_TIME", "BACKUP_KEEP", "BACKUP_DIR"}


def test_readme_leads_with_dev_section_and_warns():
    h2 = re.findall(r"^## (.+)$", README, re.M)
    assert h2[0] == "Run the website without the container (dev environment)"
    assert "127.0.0.1" in README and "no authentication" in README.lower()


def test_readme_variables_table_matches_config():
    table = set(re.findall(r"^\| `([A-Z_]+)` \|", README, re.M))
    assert table == APP_VARS


def bash_commands():
    for block in re.findall(r"```bash\n(.*?)```", README, re.S):
        for line in block.splitlines():
            yield line.strip()


def test_readme_repo_paths_in_commands_exist():
    seen = 0
    for line in bash_commands():
        for tok in line.split():
            if re.match(r"^(scripts|web|tests|app|docs)/[\w./-]+$", tok):
                seen += 1
                assert (ROOT / tok).exists(), tok
    assert seen >= 4
    for name in ("docker-compose.example.yml", "analytics.env.example", "Dockerfile"):
        assert (ROOT / name).exists()


def test_readme_relative_links_exist():
    for target in re.findall(r"\]\((?!https?:|#)([^)#]+)", README):
        assert (ROOT / target).resolve().exists(), target


def test_dockerfile_serves_ui_and_has_healthcheck():
    d = (ROOT / "Dockerfile").read_text()
    assert re.search(r"^COPY web/index\.html ", d, re.M) and "COPY web/css" in d and "COPY web/js" in d
    assert "web/test" not in d and "ENV WEB_DIR=/srv/web" in d
    assert re.search(r"^HEALTHCHECK .*\n?.*api/health", d, re.M)


def test_server_compose_passes_backup_vars():
    f = ROOT.parent / "dublin-bus-server" / "docker-compose.yml"
    if not f.exists():
        pytest.skip("sibling server repo not checked out")
    t = f.read_text()
    for k in ("BACKUP_ENABLED", "BACKUP_TIME", "BACKUP_KEEP", "BACKUP_DIR"):
        assert re.search(rf'^\s+{k}: "\$\{{ANALYTICS_{k}:-', t, re.M), k
