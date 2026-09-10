"""`quercus` command line."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import sys
from pathlib import Path
from typing import Optional

import typer

from quercus_mcp import __version__
from quercus_mcp.config import Config, Paths, delete_token, get_token, set_token

app = typer.Typer(help="Sync your Quercus (Canvas) courses locally and serve them to Claude over MCP.", no_args_is_help=True)


def _paths() -> Paths:
    return Paths.default()


@app.callback()
def _main(verbose: bool = typer.Option(False, "--verbose", "-v", help="Log to stderr")) -> None:
    os.umask(0o077)  # everything we write under ~/.quercus-mcp is private
    logging.basicConfig(level=logging.INFO if verbose else logging.WARNING, stream=sys.stderr, format="%(levelname)s %(name)s: %(message)s")


@app.command()
def login(
    base_url: str = typer.Option(None, help="Canvas base URL (default https://q.utoronto.ca)"),
    expires: Optional[str] = typer.Option(None, help="Token expiry date (YYYY-MM-DD) to record for reminders"),
    token: Optional[str] = typer.Option(None, help="Token (prompted securely if omitted)"),
) -> None:
    """Store a Quercus personal access token in the OS keychain and verify it."""
    paths = _paths()
    config = Config.load(paths)
    if base_url:
        config.base_url = base_url.rstrip("/")
    typer.echo(f"Canvas host: {config.base_url}")
    typer.echo("Create a token at Account → Settings → Approved Integrations → “+ New Access Token”.")
    tok = token or typer.prompt("Paste your access token", hide_input=True)
    tok = tok.strip()

    from quercus_mcp.canvas.client import CanvasClient
    from quercus_mcp.canvas.errors import AuthError, CanvasError

    async def verify() -> dict:
        async with CanvasClient(config.base_url, tok) as c:
            return await c.get_self()

    try:
        me = asyncio.run(verify())
    except AuthError:
        typer.secho("Canvas rejected that token (401).", fg="red", err=True)
        raise typer.Exit(1)
    except CanvasError as exc:
        typer.secho(f"Could not verify token: HTTP {exc.status} {exc.body}", fg="red", err=True)
        raise typer.Exit(1)
    set_token(config.host, tok)
    config.save(paths)
    from quercus_mcp.store import Store

    with Store(paths.db) as store:
        store.meta_set("token_expires_at", expires)
        store.meta_set("last_auth_error", None)
    typer.secho(f"Logged in as {me.get('name') or me.get('id')} on {config.host}. Token stored in keychain.", fg="green")
    typer.echo("Next: `quercus sync` then `quercus config claude-desktop` (or claude-code).")


@app.command()
def logout() -> None:
    """Remove the stored token."""
    paths = _paths()
    delete_token(Config.load(paths).host)
    typer.echo("Token removed.")


@app.command()
def sync(
    course: Optional[list[int]] = typer.Option(None, "--course", help="Only these course ids (repeatable)"),
    full: bool = typer.Option(False, help="Re-download and re-extract everything"),
    all_terms: bool = typer.Option(False, help="Include completed/past courses"),
    retry_errors: bool = typer.Option(False, help="Retry documents whose extraction failed"),
) -> None:
    """Fetch courses from Quercus into the local cache."""
    paths = _paths()
    config = Config.load(paths)
    if all_terms:
        config.all_terms = True
    token = get_token(config.host)
    if not token:
        typer.secho("No token stored. Run `quercus login` first.", fg="red", err=True)
        raise typer.Exit(1)
    from quercus_mcp.canvas.client import CanvasClient
    from quercus_mcp.canvas.errors import AuthError
    from quercus_mcp.store import Store
    from quercus_mcp.sync.crawler import Syncer

    async def go():
        with Store(paths.db) as store:
            async with CanvasClient(config.base_url, token) as client:
                syncer = Syncer(client, store, config, paths, retry_errors=retry_errors)
                return await syncer.sync_all(full=full, course_ids=course or None)

    try:
        s = asyncio.run(go())
    except AuthError:
        typer.secho("Authentication failed: token invalid or expired. Run `quercus login`.", fg="red", err=True)
        raise typer.Exit(1)
    typer.echo(f"Sync {s.status}: {s.courses} course(s), {s.added} added, {s.updated} updated, {s.removed} removed, {s.downloaded} file(s) downloaded.")
    for e in s.errors:
        typer.secho(f"  ! {e}", fg="yellow", err=True)
    if s.errors:
        raise typer.Exit(2)


@app.command()
def status() -> None:
    """Show cache location, courses and recent sync runs."""
    paths = _paths()
    config = Config.load(paths)
    from quercus_mcp.store import Store

    typer.echo(f"quercus-mcp {__version__}")
    typer.echo(f"Home: {paths.home}")
    typer.echo(f"Canvas: {config.base_url}")
    typer.echo(f"Token: {'stored' if get_token(config.host) else 'MISSING (run `quercus login`)'}")
    if not paths.db.exists():
        typer.echo("No cache yet. Run `quercus sync`.")
        return
    with Store(paths.db) as store:
        if store.meta_get("last_auth_error"):
            typer.secho("Last sync failed authentication — run `quercus login`.", fg="red")
        exp = store.meta_get("token_expires_at")
        if exp:
            typer.echo(f"Token expiry (as entered at login): {exp}")
        typer.echo("\nCourses:")
        for c in store.courses():
            k = store.counts_by_kind(c.id)
            typer.echo(f"  {c.code:<12} id={c.id:<7} {sum(k.values()):>4} docs  last sync {c.last_synced_at or '-'}" + ("  [file listing blocked by Canvas; files found via modules/links]" if c.files_tab_hidden else ""))
        typer.echo("\nRecent syncs:")
        for r in store.sync_runs(5):
            typer.echo(f"  {r.started_at} [{r.status}] +{r.added} ~{r.updated} -{r.removed}" + (f" errors={len(r.errors)}" if r.errors else ""))


@app.command()
def serve() -> None:
    """Run the MCP server over stdio (used by Claude Desktop / Claude Code)."""
    from quercus_mcp.server import run_server

    run_server(_paths())


@app.command()
def config(target: str = typer.Argument("claude-desktop", help="claude-desktop | claude-code | show")) -> None:
    """Print the MCP client configuration for this server, or show current settings."""
    paths = _paths()
    cfg = Config.load(paths)
    exe = shutil.which("quercus") or sys.argv[0]
    exe = str(Path(exe).resolve())
    if target == "show":
        typer.echo(f"config file: {paths.config_path}")
        for k, v in vars(cfg).items():
            typer.echo(f"{k} = {v!r}")
        return
    env = {"QUERCUS_HOME": str(paths.home)} if paths.home != Path.home() / ".quercus-mcp" else {}
    if target == "claude-desktop":
        snippet = {"mcpServers": {"quercus": {"command": exe, "args": ["serve"], **({"env": env} if env else {})}}}
        typer.echo("Add to ~/Library/Application Support/Claude/claude_desktop_config.json:")
        typer.echo(json.dumps(snippet, indent=2))
    elif target == "claude-code":
        env_flags = " ".join(f"-e {k}={v}" for k, v in env.items())
        typer.echo("Run:")
        typer.echo(f"claude mcp add quercus {env_flags} -- {exe} serve".replace("  ", " "))
    else:
        typer.secho(f"Unknown target {target!r}. Use claude-desktop, claude-code or show.", fg="red", err=True)
        raise typer.Exit(1)


if __name__ == "__main__":  # pragma: no cover
    app()
