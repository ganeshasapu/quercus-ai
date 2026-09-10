from typer.testing import CliRunner

from quercus_mcp.cli import app

runner = CliRunner()


def test_status_without_cache(tmp_path, monkeypatch):
    monkeypatch.setenv("QUERCUS_HOME", str(tmp_path))
    monkeypatch.delenv("QUERCUS_TOKEN", raising=False)
    monkeypatch.setattr("quercus_mcp.config._keyring_get", lambda host: None)
    r = runner.invoke(app, ["status"])
    assert r.exit_code == 0, r.output
    assert "MISSING" in r.output and "No cache yet" in r.output


def test_config_snippets(tmp_path, monkeypatch):
    monkeypatch.setenv("QUERCUS_HOME", str(tmp_path))
    r = runner.invoke(app, ["config", "claude-desktop"])
    assert r.exit_code == 0 and '"mcpServers"' in r.output and '"serve"' in r.output and "QUERCUS_HOME" in r.output
    r = runner.invoke(app, ["config", "claude-code"])
    assert r.exit_code == 0 and r.output.strip().splitlines()[-1].startswith("claude mcp add quercus")
    assert runner.invoke(app, ["config", "bogus"]).exit_code == 1


def test_sync_without_token(tmp_path, monkeypatch):
    monkeypatch.setenv("QUERCUS_HOME", str(tmp_path))
    monkeypatch.delenv("QUERCUS_TOKEN", raising=False)
    monkeypatch.setattr("quercus_mcp.config._keyring_get", lambda host: None)
    r = runner.invoke(app, ["sync"])
    assert r.exit_code == 1 and "quercus login" in r.output
