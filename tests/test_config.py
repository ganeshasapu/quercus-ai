from pathlib import Path

from quercus_mcp import config as cfg


def test_paths_honour_quercus_home(tmp_path, monkeypatch):
    monkeypatch.setenv("QUERCUS_HOME", str(tmp_path / "home"))
    paths = cfg.Paths.default()
    assert paths.home == tmp_path / "home"
    assert paths.db == tmp_path / "home" / "quercus.db"
    assert paths.files_dir == tmp_path / "home" / "files"
    assert paths.text_dir == tmp_path / "home" / "text"
    assert paths.config_path == tmp_path / "home" / "config.toml"


def test_paths_ensure_creates_dirs(tmp_path):
    paths = cfg.Paths(tmp_path / "h")
    paths.ensure()
    assert paths.files_dir.is_dir() and paths.text_dir.is_dir()


def test_config_defaults_when_missing(tmp_path):
    paths = cfg.Paths(tmp_path)
    c = cfg.Config.load(paths)
    assert c.base_url == "https://q.utoronto.ca"
    assert c.sync_interval_minutes == 30
    assert c.volatile_ttl_minutes == 10
    assert c.max_file_mb == 50
    assert c.include_courses == [] and c.exclude_courses == []
    assert c.all_terms is False


def test_config_round_trip(tmp_path):
    paths = cfg.Paths(tmp_path)
    c = cfg.Config(base_url="https://canvas.example.edu/", sync_interval_minutes=5,
                   include_courses=[1, 2], exclude_courses=[3])
    c.save(paths)
    loaded = cfg.Config.load(paths)
    assert loaded.base_url == "https://canvas.example.edu"  # trailing slash stripped
    assert loaded.sync_interval_minutes == 5
    assert loaded.include_courses == [1, 2]
    assert loaded.exclude_courses == [3]


def test_host_derived_from_base_url():
    c = cfg.Config(base_url="https://q.utoronto.ca")
    assert c.host == "q.utoronto.ca"


def test_env_token_wins_over_keyring(monkeypatch):
    monkeypatch.setenv("QUERCUS_TOKEN", "env-token")
    monkeypatch.setattr(cfg, "_keyring_get", lambda host: "kr-token")
    assert cfg.get_token("q.utoronto.ca") == "env-token"


def test_keyring_token_used_when_no_env(monkeypatch):
    monkeypatch.delenv("QUERCUS_TOKEN", raising=False)
    monkeypatch.setattr(cfg, "_keyring_get", lambda host: "kr-token")
    assert cfg.get_token("q.utoronto.ca") == "kr-token"


def test_no_token(monkeypatch):
    monkeypatch.delenv("QUERCUS_TOKEN", raising=False)
    monkeypatch.setattr(cfg, "_keyring_get", lambda host: None)
    assert cfg.get_token("q.utoronto.ca") is None


def test_set_token_calls_keyring(monkeypatch):
    calls = {}
    monkeypatch.setattr(cfg, "_keyring_set", lambda host, tok: calls.update(host=host, tok=tok))
    cfg.set_token("q.utoronto.ca", "abc")
    assert calls == {"host": "q.utoronto.ca", "tok": "abc"}
