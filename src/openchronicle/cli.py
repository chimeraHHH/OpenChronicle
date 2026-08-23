"""OpenChronicle CLI — start / stop / pause / resume / status / mcp / writer."""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
import shutil
import subprocess
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import typer
from rich.console import Console
from rich.table import Table

from . import __version__, paths
from . import config as config_mod
from . import logger as logger_mod
from .capture import filenames as capture_filenames
from .capture import reconcile as capture_reconcile
from .capture import store_lock as capture_store
from .memory_candidates import store as candidate_store
from .privacy.egress import privacy_egress_fenced, privacy_egress_lock
from .store import entries as entries_mod
from .store import files as files_mod
from .store import fts, index_md

app = typer.Typer(
    add_completion=False,
    no_args_is_help=True,
    help="Local-first screen-context memory for LLM agents.",
)
console = Console()


def _init() -> config_mod.Config:
    paths.ensure_dirs()
    created = config_mod.write_default_if_missing()
    if created:
        console.print(f"[green]Created default config at {paths.config_file()}[/green]")
    logger_mod.setup(console=False)
    return config_mod.load()


def _is_pid_alive(pid: int) -> bool:
    if pid <= 1:
        return False
    try:
        os.kill(pid, 0)
    except (ProcessLookupError, OverflowError):
        return False
    except PermissionError:
        return True
    return True


def _read_pid() -> int | None:
    try:
        pid = int(paths.pid_file().read_text().strip())
    except (OSError, ValueError):
        return None
    # A PID alone is unsafe after SIGKILL because the OS can reuse it for an
    # unrelated process. Bind the public PID file to the PID recorded by the
    # process that currently holds OpenChronicle's lifetime lease. Malformed,
    # dangerous (process-group/init), stale, or mismatched values fail closed.
    if pid <= 1:
        return None
    lock_pid = _held_daemon_lock_pid()
    if lock_pid != pid:
        return None
    return pid if _is_pid_alive(pid) else None


def _held_daemon_lock_pid() -> int | None:
    """Return the valid PID recorded in a currently-held daemon lease."""
    try:
        fd = os.open(paths.daemon_lock_file(), os.O_RDWR)
    except OSError:
        return None
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            try:
                os.lseek(fd, 0, os.SEEK_SET)
                lock_pid = int(os.read(fd, 64).decode("ascii").strip())
            except (OSError, UnicodeDecodeError, ValueError):
                return None
            return lock_pid if lock_pid > 1 else None
        fcntl.flock(fd, fcntl.LOCK_UN)
        return None
    except OSError:
        # Stop must never send a signal when lease ownership is ambiguous.
        return None
    finally:
        os.close(fd)


def _daemon_uptime() -> str:
    """Return a human-readable uptime string for the running daemon.

    Reads the PID file's mtime as a proxy for daemon start time (the
    daemon overwrites it on each launch). Returns ``"stopped"`` when
    the daemon is not running.
    """
    pid = _read_pid()
    if not pid:
        return "stopped"
    try:
        mtime = paths.pid_file().stat().st_mtime
        now = datetime.now().astimezone()
        delta = now - datetime.fromtimestamp(mtime).astimezone()
        h, r = divmod(int(delta.total_seconds()), 3600)
        m = r // 60
        if h >= 24:
            return f"{h // 24}d {h % 24}h"
        if h:
            return f"{h}h {m}m"
        return f"{m}m"
    except OSError:
        return "unknown"


def _last_capture_info(
    cfg: config_mod.Config | None = None,
) -> tuple[str | None, str | None]:
    """Return ``(timestamp, app_name)`` of the most recent capture buffer file.

    Returns ``(None, None)`` when the buffer directory is empty or missing.
    """
    buf = paths.capture_buffer_dir()
    if not buf.exists():
        return None, None
    json_files = [p for p in buf.iterdir() if p.suffix == ".json"]
    if not json_files:
        return None, None
    from .privacy import policy as privacy_policy

    cfg = cfg or config_mod.Config()
    remaining = list(json_files)
    while remaining:
        latest = _latest_capture_path(remaining)
        remaining.remove(latest)
        if latest.is_symlink() or not latest.is_file():
            continue
        try:
            data = json.loads(latest.read_bytes())
        except (OSError, ValueError):
            continue
        if (
            not isinstance(data, dict)
            or not privacy_policy.evaluate_stored_observation(cfg.capture, observation=data).allowed
        ):
            continue
        meta = data.get("window_meta")
        if not isinstance(meta, dict):
            continue
        ts = data.get("timestamp")
        app = meta.get("app_name")
        return (
            ts if isinstance(ts, str) else None,
            app if isinstance(app, str) else None,
        )
    return None, None


def _latest_capture_path(files: list[Path]) -> Path:
    """Return the newest valid capture by absolute time, with a legacy fallback."""
    from .capture import filenames

    parsed = [
        (timestamp.timestamp(), path)
        for path in files
        if (timestamp := filenames.parse_capture_stem(path.stem)) is not None
    ]
    if parsed:
        return max(parsed, key=lambda item: item[0])[1]
    return max(files)


def _health_status(pid: int | None, last_ts: str | None) -> tuple[str, str]:
    """Return ``(label, style)`` for daemon health.

    ``style`` is a Rich-style string suitable for ``console.print``.
    """
    if not pid:
        return "stopped", "red"
    if not last_ts:
        return "running (no captures yet)", "yellow"
    try:
        last = datetime.fromisoformat(last_ts)
        age = (datetime.now(last.tzinfo) - last).total_seconds()
    except (ValueError, TypeError):
        return "running", "green"
    if age < 300:  # 5 minutes
        return "healthy", "green"
    return "stale (no captures in >5m)", "yellow"


# ─── commands ─────────────────────────────────────────────────────────────


@app.command()
def start(
    foreground: bool = typer.Option(False, "--foreground", "-f", help="Run in this terminal."),
    capture_only: bool = typer.Option(
        False,
        "--capture-only",
        help="Capture/session bookkeeping only; disable model processing and MCP.",
    ),
) -> None:
    """Start the OpenChronicle daemon."""
    cfg = _init()
    pid = _read_pid()
    if pid:
        console.print(f"[yellow]Already running (pid {pid})[/yellow]")
        raise typer.Exit(1)

    from . import daemon

    if foreground:
        console.print("[bold]OpenChronicle starting in foreground[/bold] — Ctrl+C to stop.")
        daemon.run(cfg, capture_only=capture_only)
        return

    # Background: double-fork
    if os.fork() != 0:
        console.print("[green]OpenChronicle started in background.[/green]")
        console.print(f"Logs: {paths.logs_dir()}")
        return
    os.setsid()
    if os.fork() != 0:
        os._exit(0)
    # Redirect stdio to /dev/null. After dup2 the original fd is no longer
    # needed; closing it avoids leaking one descriptor per daemon start.
    devnull = os.open(os.devnull, os.O_RDWR)
    for fd in (0, 1, 2):
        os.dup2(devnull, fd)
    if devnull > 2:
        os.close(devnull)
    daemon.run(cfg, capture_only=capture_only)
    os._exit(0)


@app.command()
def stop() -> None:
    """Stop the daemon."""
    _init()
    from . import daemon

    try:
        daemon.request_stop()
    except daemon.DaemonControlError as exc:
        console.print(f"[yellow]Daemon stop refused: {exc}[/yellow]")
        raise typer.Exit(1) from None
    console.print("[green]Authenticated daemon stop request accepted.[/green]")


@app.command()
def pause() -> None:
    """Pause capture (daemon stays up but skips captures)."""
    paths.ensure_dirs()
    paths.paused_flag().write_text(datetime.now().isoformat())
    console.print("[yellow]Capture paused.[/yellow]")


@app.command()
def resume() -> None:
    """Resume capture."""
    with contextlib.suppress(FileNotFoundError):
        paths.paused_flag().unlink()
    console.print("[green]Capture resumed.[/green]")


@app.command()
def status() -> None:
    """Show daemon status + memory stats."""
    cfg = _init()
    stages = ("timeline", "reducer", "classifier", "daily_wrap", "compact")
    # Provider probes are diagnostic network I/O.  They must finish before the
    # short capture-store fence below so a slow or hung provider cannot pause
    # ordinary capture persistence.
    ping_results = _ping_stages(cfg, stages)
    from .services.snapshot import build_snapshot

    # Rebuild, authorize, and serialize one final canonical status response
    # under the cleanup fence.  This section is local-only and intentionally
    # contains no provider calls.
    with privacy_egress_lock():
        pid = _read_pid()
        paused = paths.paused_flag().exists()
        uptime = _daemon_uptime()
        with fts.cursor() as conn:
            visible = build_snapshot(
                conn,
                cfg,
                timeline_limit=0,
                candidate_limit=0,
                wrap_limit=1,
            )
        capture_snapshot = visible["capture"]
        last_capture = capture_snapshot.get("last")
        last_ts = str(last_capture.get("timestamp") or "") if last_capture else None
        last_app = str(last_capture.get("app_name") or "") if last_capture else None
        health_label, health_style = _health_status(pid, last_ts)

        table = Table(show_header=False, box=None, padding=(0, 2))
        table.add_row("Version", __version__)
        table.add_row("Root", str(paths.root()))
        table.add_row(
            "Daemon",
            f"[green]running pid {pid}[/green]" if pid else "[red]stopped[/red]",
        )
        table.add_row("Uptime", uptime)
        table.add_row("Health", f"[{health_style}]{health_label}[/{health_style}]")
        table.add_row("Capture", "[yellow]paused[/yellow]" if paused else "active")

        if last_ts:
            try:
                last_dt = datetime.fromisoformat(last_ts)
                age = (datetime.now(last_dt.tzinfo) - last_dt).total_seconds()
                if age < 60:
                    ago = "just now"
                elif age < 3600:
                    ago = f"{int(age // 60)}m ago"
                else:
                    ago = f"{int(age // 3600)}h ago"
                table.add_row("Last Capture", f"{ago} ({last_app})" if last_app else ago)
            except (ValueError, TypeError):
                table.add_row("Last Capture", last_ts)
        else:
            table.add_row("Last Capture", "(none)")

        table.add_row(
            "Buffer",
            f"{int(capture_snapshot.get('indexed_count') or 0)} policy-visible capture(s)",
        )
        counts = visible["counts"]
        sessions = counts["sessions"]
        if sessions["total"]:
            table.add_row(
                "Sessions",
                f"{sessions['total']} policy-visible "
                f"({sessions['reduced']} reduced, {sessions['ended']} ended, "
                f"{sessions['failed']} failed)",
            )
        else:
            table.add_row("Sessions", "(none policy-visible)")
        table.add_row("Classifier Delivery", "policy-filtered; use memory candidates")
        memory = counts["memory"]
        table.add_row(
            "Memory",
            f"{memory['active_files']} active files, {memory['dormant_files']} dormant, "
            f"{memory['entries']} policy-visible entries",
        )
        if cfg.search.semantic_enabled:
            table.add_row(
                "Memory Search",
                f"hybrid_rrf — {cfg.search.embedding_backend}:{cfg.search.embedding_model}",
            )
        else:
            table.add_row("Memory Search", "bm25")
        table.add_row("Timeline", f"{counts['timeline_blocks']} policy-visible blocks")
        review = counts["candidates"]
        review_total = sum(int(value) for value in review.values())
        table.add_row(
            "Review Inbox",
            f"{review_total} policy-visible "
            f"({review.get('pending', 0)} pending, "
            f"{review.get('conflict', 0)} conflict)",
        )
        wraps = visible["daily_wrap"]["wraps"]
        if wraps:
            wrap = wraps[0]
            table.add_row(
                "Daily Wrap",
                f"{wrap['local_date']} {wrap['timezone']} — "
                f"{wrap['status']}/{wrap['coverage_status']} r{wrap['revision']}",
            )
        else:
            table.add_row("Daily Wrap", "(none policy-visible)")

        for stage in stages:
            m = cfg.model_for(stage)
            ping = _format_ping(ping_results.get(stage))
            table.add_row(f"Model ({stage})", f"{m.provider}:{m.model}   {ping}")

        console.print(table)


def _ping_stages(cfg: config_mod.Config, stages: tuple[str, ...]) -> dict:
    """Probe each stage's configured model, deduping identical configs.

    Returns a dict keyed by stage name -> PingResult. Pings run in parallel
    so a single hung provider can't stretch the wait past the per-call
    timeout.
    """
    from concurrent.futures import ThreadPoolExecutor
    from dataclasses import replace

    from .writer.llm import PingResult, ping_stage

    # Dedup identical provider configurations so the common single-model case
    # performs one probe, while a Codex CLI model never aliases a LiteLLM one.
    dedup: dict[tuple[str, str, str, str, str], list[str]] = {}
    for stage in stages:
        m = cfg.model_for(stage)
        key = (
            m.provider,
            m.model,
            m.reasoning_effort,
            m.base_url,
            config_mod.resolve_api_key(m) or "",
        )
        dedup.setdefault(key, []).append(stage)

    results: dict = {}
    if not dedup:
        return results
    with ThreadPoolExecutor(max_workers=min(4, len(dedup))) as pool:
        future_to_stages = {
            pool.submit(ping_stage, cfg, members[0]): members for members in dedup.values()
        }
        for future, members in future_to_stages.items():
            try:
                res = future.result(timeout=12.0)
            except Exception as exc:  # noqa: BLE001
                err_label = type(exc).__name__
                for stage in members:
                    m = cfg.model_for(stage)
                    results[stage] = PingResult(
                        stage=stage,
                        model=m.model,
                        ok=False,
                        latency_ms=None,
                        error=err_label,
                    )
                continue
            for stage in members:
                # Reuse the same PingResult across stages that share a config,
                # but tag each with its own stage name so callers can map back.
                results[stage] = replace(res, stage=stage)
    return results


def _format_ping(res) -> str:
    """Render a PingResult as a short Rich-styled cell."""
    if res is None:
        return "[dim]?[/dim]"
    if res.mocked:
        return "[dim]✓ mocked[/dim]"
    if res.ok:
        latency = f"{res.latency_ms} ms" if res.latency_ms is not None else "ok"
        return f"[green]✓[/green] {latency}"
    err = res.error or "failed"
    return f"[red]✗[/red] {err}"


@app.command()
def mcp() -> None:
    """Run the MCP server (stdio). For LLM client config."""
    _init()
    from .mcp import server as mcp_server

    mcp_server.run_stdio()


install_app = typer.Typer(help="Register the MCP server with common LLM clients.")
app.add_typer(install_app, name="install")

uninstall_app = typer.Typer(help="Remove OpenChronicle's MCP entry from LLM clients.")
app.add_typer(uninstall_app, name="uninstall")


@install_app.command("claude-code")
def install_claude_code(
    name: str = typer.Option("openchronicle", help="MCP server name shown to the client."),
    scope: str = typer.Option("user", help="Claude Code scope: user | local | project."),
) -> None:
    """Add (or refresh) OpenChronicle's entry in Claude Code's MCP config.

    Always installs the current URL/transport — if an entry named ``name`` already
    exists at the given scope, it is removed and re-registered.
    """
    cfg = _init()
    from .mcp import server as mcp_server

    if cfg.mcp.transport not in ("sse", "streamable-http"):
        console.print(
            f"[red]MCP transport is {cfg.mcp.transport!r}; install requires sse or streamable-http.[/red]"
        )
        raise typer.Exit(1)
    if not cfg.mcp.auto_start:
        console.print(
            "[yellow]Warning: mcp.auto_start is false — the daemon won't host the server.[/yellow]"
        )

    claude_bin = shutil.which("claude")
    if not claude_bin:
        console.print(
            "[red]`claude` CLI not found on PATH.[/red] "
            "Install Claude Code first, or edit ~/.claude.json manually."
        )
        raise typer.Exit(1)

    url = mcp_server.endpoint_url(cfg)
    transport_flag = "sse" if cfg.mcp.transport == "sse" else "http"

    remove = subprocess.run(
        [claude_bin, "mcp", "remove", "-s", scope, name],
        capture_output=True,
        text=True,
        check=False,
    )
    replaced = remove.returncode == 0

    cmd = [
        claude_bin,
        "mcp",
        "add",
        "-s",
        scope,
        "--transport",
        transport_flag,
        name,
        url,
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        console.print(f"[red]claude mcp add failed:[/red]\n{result.stderr or result.stdout}")
        raise typer.Exit(result.returncode)

    verb = "Updated" if replaced else "Registered"
    console.print(f"[green]{verb} {name!r} in Claude Code ({scope} scope).[/green]")
    console.print(f"  URL: {url}")
    console.print(
        "  Make sure the daemon is running (`openchronicle start`) so the server is reachable."
    )


def _claude_desktop_config_path() -> Path:
    return Path.home() / "Library" / "Application Support" / "Claude" / "claude_desktop_config.json"


def _load_claude_desktop_config(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text())
    except json.JSONDecodeError as exc:
        console.print(
            f"[red]Could not parse {path}:[/red] {exc}\n"
            "Fix the JSON or move the file aside and rerun."
        )
        raise typer.Exit(1) from exc
    if not isinstance(data, dict):
        console.print(f"[red]Unexpected top-level shape in {path} (expected object).[/red]")
        raise typer.Exit(1)
    return data


def _restart_reminder(action: str) -> None:
    console.print(
        f"[yellow]Claude Desktop must be fully quit (Cmd+Q) and reopened to {action}.[/yellow]"
    )
    console.print(
        "[dim]The app only reads claude_desktop_config.json at launch. You won't need to "
        "re-login — restart is enough, your session persists.[/dim]"
    )


@install_app.command("claude-desktop")
def install_claude_desktop(
    name: str = typer.Option("openchronicle", help="MCP server name shown to the client."),
) -> None:
    """Add (or refresh) OpenChronicle's entry in Claude Desktop's MCP config.

    Claude Desktop's JSON config only accepts stdio servers (remote SSE/HTTP
    must be added via Settings → Integrations UI), so we register
    ``openchronicle mcp`` as a subprocess command.

    Every invocation is idempotent — existing entries with the same name are
    overwritten with the current absolute path.
    """
    openchronicle_bin = shutil.which("openchronicle")
    if not openchronicle_bin:
        console.print(
            "[red]`openchronicle` not found on PATH.[/red]\n"
            "Install it globally first with [cyan]uv tool install .[/cyan] "
            "(from the repo), then rerun this command."
        )
        raise typer.Exit(1)

    cfg_path = _claude_desktop_config_path()
    cfg_path.parent.mkdir(parents=True, exist_ok=True)

    data = _load_claude_desktop_config(cfg_path)
    servers = data.setdefault("mcpServers", {})
    if not isinstance(servers, dict):
        console.print(f"[red]`mcpServers` in {cfg_path} is not an object.[/red]")
        raise typer.Exit(1)

    replaced = name in servers
    servers[name] = {
        "command": openchronicle_bin,
        "args": ["mcp"],
    }

    cfg_path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n")

    verb = "Updated" if replaced else "Registered"
    console.print(f"[green]{verb} {name!r} in Claude Desktop config.[/green]")
    console.print(f"  file: {cfg_path}")
    console.print(f"  command: {openchronicle_bin} mcp")
    _restart_reminder("pick up the new entry")


@install_app.command("codex")
def install_codex(
    name: str = typer.Option("openchronicle", help="MCP server name shown to the client."),
) -> None:
    """Add (or refresh) OpenChronicle's entry in Codex CLI's MCP config.

    Codex CLI supports streamable-HTTP MCP servers via ``codex mcp add <name> --url <URL>``,
    so we register the daemon's always-on endpoint. The CLI and the IDE extension
    share this config, so a single install covers both clients.

    Every invocation is idempotent — if an entry named ``name`` already exists,
    it is removed and re-registered with the current URL.
    """
    cfg = _init()
    from .mcp import server as mcp_server

    if cfg.mcp.transport not in ("sse", "streamable-http"):
        console.print(
            f"[red]MCP transport is {cfg.mcp.transport!r}; install requires sse or streamable-http.[/red]"
        )
        raise typer.Exit(1)
    if not cfg.mcp.auto_start:
        console.print(
            "[yellow]Warning: mcp.auto_start is false — the daemon won't host the server.[/yellow]"
        )

    codex_bin = shutil.which("codex")
    if not codex_bin:
        console.print(
            "[red]`codex` CLI not found on PATH.[/red] "
            "Install Codex first (https://github.com/openai/codex), "
            "or edit ~/.codex/config.toml manually."
        )
        raise typer.Exit(1)

    url = mcp_server.endpoint_url(cfg)

    remove = subprocess.run(
        [codex_bin, "mcp", "remove", name],
        capture_output=True,
        text=True,
        check=False,
    )
    replaced = remove.returncode == 0

    cmd = [codex_bin, "mcp", "add", name, "--url", url]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        console.print(f"[red]codex mcp add failed:[/red]\n{result.stderr or result.stdout}")
        raise typer.Exit(result.returncode)

    verb = "Updated" if replaced else "Registered"
    console.print(f"[green]{verb} {name!r} in Codex CLI.[/green]")
    console.print(f"  URL: {url}")
    console.print(
        "  Make sure the daemon is running (`openchronicle start`) so the server is reachable."
    )


def _opencode_config_path() -> Path:
    return Path.home() / ".config" / "opencode" / "opencode.json"


def _load_opencode_config(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text())
    except json.JSONDecodeError as exc:
        console.print(
            f"[red]Could not parse {path}:[/red] {exc}\n"
            "If your config is JSONC (with comments), edit the `mcp` section manually."
        )
        raise typer.Exit(1) from exc
    if not isinstance(data, dict):
        console.print(f"[red]Unexpected top-level shape in {path} (expected object).[/red]")
        raise typer.Exit(1)
    return data


@install_app.command("opencode")
def install_opencode(
    name: str = typer.Option("openchronicle", help="MCP server name shown to the client."),
) -> None:
    """Add (or refresh) OpenChronicle's entry in opencode's MCP config.

    opencode (https://opencode.ai) reads ``~/.config/opencode/opencode.json``
    and supports remote streamable-HTTP MCP servers natively, so we register
    the daemon's always-on endpoint.

    Every invocation is idempotent — an existing entry named ``name`` is
    overwritten with the current URL; other `mcp` entries are preserved.
    """
    cfg = _init()
    from .mcp import server as mcp_server

    if cfg.mcp.transport not in ("sse", "streamable-http"):
        console.print(
            f"[red]MCP transport is {cfg.mcp.transport!r}; install requires sse or streamable-http.[/red]"
        )
        raise typer.Exit(1)
    if not cfg.mcp.auto_start:
        console.print(
            "[yellow]Warning: mcp.auto_start is false — the daemon won't host the server.[/yellow]"
        )

    cfg_path = _opencode_config_path()
    jsonc_path = cfg_path.with_suffix(".jsonc")
    if jsonc_path.exists():
        url = mcp_server.endpoint_url(cfg)
        console.print(
            f"[red]Found {jsonc_path} — can't safely edit JSONC (comments would be lost).[/red]\n"
            "Add this entry under the `mcp` key manually:\n"
            f'  "{name}": {{"type": "remote", "url": "{url}", "enabled": true}}'
        )
        raise typer.Exit(1)

    existed = cfg_path.exists()
    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    data = _load_opencode_config(cfg_path)
    if not existed:
        data["$schema"] = "https://opencode.ai/config.json"

    servers = data.setdefault("mcp", {})
    if not isinstance(servers, dict):
        console.print(f"[red]`mcp` in {cfg_path} is not an object.[/red]")
        raise typer.Exit(1)

    url = mcp_server.endpoint_url(cfg)
    replaced = name in servers
    servers[name] = {
        "type": "remote",
        "url": url,
        "enabled": True,
    }

    cfg_path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n")

    verb = "Updated" if replaced else "Registered"
    console.print(f"[green]{verb} {name!r} in opencode config.[/green]")
    console.print(f"  URL: {url}")
    console.print(
        "  Make sure the daemon is running (`openchronicle start`) so the server is reachable."
    )


@install_app.command("mcp-json")
def install_mcp_json(
    name: str = typer.Option("openchronicle", help="MCP server name written into the config."),
    filename: str = typer.Option("mcp.json", help="Output filename (written to CWD)."),
    http: bool = typer.Option(
        False,
        "--http",
        help="Emit a URL-based entry using the configured HTTP endpoint instead of stdio.",
    ),
    force: bool = typer.Option(False, "--force", "-f", help="Overwrite if the file exists."),
) -> None:
    """Generate a generic MCP config in the current directory.

    Shape matches the ``mcpServers`` object used by most local agent
    frameworks (Cursor, Cline, Continue, Zed, Windsurf, custom tools). Drop
    the emitted file next to your agent's config or merge its contents into
    an existing one.
    """
    cfg = _init()
    out_path = Path.cwd() / filename
    if out_path.exists() and not force:
        console.print(f"[red]{out_path} already exists.[/red] Use --force to overwrite.")
        raise typer.Exit(1)

    if http:
        from .mcp import server as mcp_server

        if cfg.mcp.transport not in ("sse", "streamable-http"):
            console.print(
                f"[red]--http requires mcp.transport to be sse or streamable-http, "
                f"got {cfg.mcp.transport!r}.[/red]"
            )
            raise typer.Exit(1)
        url = mcp_server.endpoint_url(cfg)
        transport_label = "sse" if cfg.mcp.transport == "sse" else "http"
        entry: dict[str, object] = {"url": url, "transport": transport_label}
        summary = f"{transport_label} → {url}"
    else:
        openchronicle_bin = shutil.which("openchronicle") or "openchronicle"
        entry = {"command": openchronicle_bin, "args": ["mcp"]}
        summary = f"stdio → {openchronicle_bin} mcp"

    payload = {"mcpServers": {name: entry}}
    out_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n")

    console.print(f"[green]Wrote {out_path}[/green]")
    console.print(f"  server: {name} ({summary})")
    console.print(
        "[dim]Point your agent framework at this file, or merge `mcpServers` "
        "into its existing MCP config.[/dim]"
    )


@uninstall_app.command("claude-code")
def uninstall_claude_code(
    name: str = typer.Option("openchronicle", help="MCP server name to remove."),
    scope: str = typer.Option("user", help="Claude Code scope the entry was installed at."),
) -> None:
    """Remove OpenChronicle's entry from Claude Code's MCP config.

    Scope must match whatever ``install claude-code`` used (default ``user``).
    Missing entries are treated as success — the command is idempotent.
    """
    claude_bin = shutil.which("claude")
    if not claude_bin:
        console.print(
            "[red]`claude` CLI not found on PATH.[/red] "
            "Install Claude Code first, or edit ~/.claude.json manually."
        )
        raise typer.Exit(1)

    result = subprocess.run(
        [claude_bin, "mcp", "remove", "-s", scope, name],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode == 0:
        console.print(f"[green]Removed {name!r} from Claude Code ({scope} scope).[/green]")
        return

    combined = (result.stderr + result.stdout).lower()
    if "no mcp server" in combined or "not found" in combined:
        console.print(f"[yellow]No {name!r} entry at {scope} scope — nothing to remove.[/yellow]")
        return

    console.print(f"[red]claude mcp remove failed:[/red]\n{result.stderr or result.stdout}")
    raise typer.Exit(result.returncode)


@uninstall_app.command("codex")
def uninstall_codex(
    name: str = typer.Option("openchronicle", help="MCP server name to remove."),
) -> None:
    """Remove OpenChronicle's entry from Codex CLI's MCP config.

    Missing entries are treated as success — the command is idempotent.
    """
    codex_bin = shutil.which("codex")
    if not codex_bin:
        console.print(
            "[red]`codex` CLI not found on PATH.[/red] "
            "Install Codex first, or edit ~/.codex/config.toml manually."
        )
        raise typer.Exit(1)

    result = subprocess.run(
        [codex_bin, "mcp", "remove", name],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode == 0:
        console.print(f"[green]Removed {name!r} from Codex CLI.[/green]")
        return

    combined = (result.stderr + result.stdout).lower()
    if "no mcp server" in combined or "not found" in combined or "does not exist" in combined:
        console.print(f"[yellow]No {name!r} entry in Codex config — nothing to remove.[/yellow]")
        return

    console.print(f"[red]codex mcp remove failed:[/red]\n{result.stderr or result.stdout}")
    raise typer.Exit(result.returncode)


@uninstall_app.command("opencode")
def uninstall_opencode(
    name: str = typer.Option("openchronicle", help="MCP server name to remove."),
) -> None:
    """Remove OpenChronicle's entry from opencode's MCP config.

    Missing config / missing entry are treated as success — the command is
    idempotent.
    """
    cfg_path = _opencode_config_path()
    if not cfg_path.exists():
        console.print(f"[yellow]No opencode config at {cfg_path} — nothing to remove.[/yellow]")
        return

    data = _load_opencode_config(cfg_path)
    servers = data.get("mcp")
    if not isinstance(servers, dict) or name not in servers:
        console.print(f"[yellow]No {name!r} entry in opencode config — nothing to remove.[/yellow]")
        return

    del servers[name]
    cfg_path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n")

    console.print(f"[green]Removed {name!r} from opencode config.[/green]")


@uninstall_app.command("claude-desktop")
def uninstall_claude_desktop(
    name: str = typer.Option("openchronicle", help="MCP server name to remove."),
) -> None:
    """Remove OpenChronicle's entry from Claude Desktop's MCP config.

    Missing config / missing entry are treated as success — the command is
    idempotent.
    """
    cfg_path = _claude_desktop_config_path()
    if not cfg_path.exists():
        console.print(
            f"[yellow]No Claude Desktop config at {cfg_path} — nothing to remove.[/yellow]"
        )
        return

    data = _load_claude_desktop_config(cfg_path)
    servers = data.get("mcpServers")
    if not isinstance(servers, dict) or name not in servers:
        console.print(
            f"[yellow]No {name!r} entry in Claude Desktop config — nothing to remove.[/yellow]"
        )
        return

    del servers[name]
    cfg_path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n")

    console.print(f"[green]Removed {name!r} from Claude Desktop config.[/green]")
    _restart_reminder("finalize the removal")


timeline_app = typer.Typer(help="Timeline (short-window activity blocks) subcommands.")
app.add_typer(timeline_app, name="timeline")


@timeline_app.command("tick")
def timeline_tick_cmd() -> None:
    """Build any closed timeline windows right now (synchronous)."""
    cfg = _init()
    from .timeline import tick as tick_mod

    produced = tick_mod.tick_now(cfg)
    console.print(f"[green]Produced {produced} block(s).[/green]")


@timeline_app.command("list")
@privacy_egress_fenced
def timeline_list(
    limit: int = typer.Option(12, "--limit", "-n", help="How many recent blocks to show."),
) -> None:
    """Show the most recent timeline blocks (oldest → newest)."""
    cfg = _init()
    from .provenance.models import EvidenceRef
    from .services.context import ContextService
    from .timeline import store as tls

    requested = max(limit, 0)
    with fts.cursor() as conn:
        context = ContextService(conn, cfg)
        blocks = (
            [
                block
                for block in tls.query_recent(conn, limit=1_000)
                if context.evidence_allowed(EvidenceRef(kind="timeline_block", id=block.id))
            ][-requested:]
            if requested
            else []
        )
    if not blocks:
        console.print("[yellow]No timeline blocks yet.[/yellow]")
        return
    for b in blocks:
        apps = ", ".join(b.apps_used) or "—"
        console.print(
            f"[bold]{b.start_time.strftime('%Y-%m-%d %H:%M')}"
            f"–{b.end_time.strftime('%H:%M')}[/bold] "
            f"({b.capture_count} captures, apps: {apps})"
        )
        for e in b.entries:
            console.print(f"  - {e}")


writer_app = typer.Typer(help="Writer subcommands.")
app.add_typer(writer_app, name="writer")


@writer_app.command("run")
def writer_run() -> None:
    """Reduce any pending sessions and run the classifier on each result."""
    cfg = _init()
    from .writer import agent

    agent.run(cfg)
    # Crash-recovered classifier receipts can predate the current privacy
    # policy.  Do not echo model-controlled summaries, artifact IDs, or raw
    # counts here; the policy-aware review/list commands expose what remains
    # visible after catch-up.
    console.print("[green]Writer catch-up completed.[/green]")


memory_app = typer.Typer(help="Review and manage proposed durable memories.")
app.add_typer(memory_app, name="memory")


@memory_app.command("explain-recall")
@privacy_egress_fenced
def memory_explain_recall(
    query: str = typer.Argument(..., help="Transient local recall query."),
    top_k: int = typer.Option(5, "--top-k", "-k", min=1, max=20),
    json_output: bool = typer.Option(False, "--json", help="Emit deterministic JSON."),
) -> None:
    """Explain local durable-memory ranking without returning memory content."""
    cfg = _init()
    from .services.memory_recall_explain import explain_memory_recall

    with fts.cursor() as conn:
        report = explain_memory_recall(conn, cfg, query=query, top_k=top_k)
    if json_output:
        typer.echo(json.dumps(report, ensure_ascii=False, sort_keys=True))
        return
    if error := report.get("error"):
        console.print(f"[yellow]{error}[/yellow]")
        return
    table = Table("Rank", "Memory", "BM25", "Vector", "Similarity", "RRF")
    for row in report["results"]:
        table.add_row(
            str(row["position"]),
            f"{row['path']}#{row['id']}",
            str(row["bm25_rank"] or "—"),
            str(row["vector_rank"] or "—"),
            "—" if row["vector_similarity"] is None else f"{row['vector_similarity']:.4f}",
            "—" if row["rrf_score"] is None else f"{row['rrf_score']:.6f}",
        )
    console.print(table)
    console.print(
        f"Mode: {report['retrieval_mode']}; candidates inspected: "
        f"{report['candidate_count']}; returned: {report['returned_count']}."
    )


@memory_app.command("adoptions")
@privacy_egress_fenced
def memory_adoptions(
    limit: int = typer.Option(50, "--limit", "-n", min=1, max=1_000),
) -> None:
    """List exact Prompt/Reply Rescue outputs the user marked as used."""
    _init()
    from .artifact_adoptions import store as adoption_store

    with fts.cursor() as conn:
        adoptions = adoption_store.list_recent(conn, limit=limit)
    table = Table("ID", "Artifact", "Source", "Version", "Edited", "Adopted")
    for adoption in adoptions:
        table.add_row(
            adoption.id,
            adoption.artifact_kind,
            adoption.artifact_id,
            str(adoption.artifact_version),
            "yes" if adoption.output_edited else "no",
            adoption.adopted_at,
        )
    console.print(table)


@memory_app.command("usefulness")
@privacy_egress_fenced
def memory_usefulness(
    json_output: bool = typer.Option(False, "--json", help="Emit deterministic JSON."),
) -> None:
    """Report exact memory revisions and Prompt Rescue adoption outcomes."""
    cfg = _init()
    from .services.memory_usefulness import memory_usefulness_report

    with fts.cursor() as conn:
        report = memory_usefulness_report(conn, cfg)
    if json_output:
        typer.echo(json.dumps(report, ensure_ascii=False, sort_keys=True))
        return

    summary = report["summary"]
    table = Table("Memory revision", "Status", "Outputs", "Unedited", "Edited")
    for row in report["memory_revisions"]:
        revision = row["memory_revision"]
        table.add_row(
            f"{revision['path']}#{revision['id']}",
            str(row["current_status"]),
            str(row["conditioned_output_count"]),
            str(row["unedited_adoption_count"]),
            str(row["edited_adoption_count"]),
        )
    console.print(table)
    console.print(
        "Tracked exact revisions: "
        f"{summary['tracked_exact_revision_binding_count']}/"
        f"{summary['exact_revision_binding_count']}; "
        "conditioned output adoption rate: "
        f"{summary['conditioned_output_unedited_adoption_rate']:.3f} "
        "(descriptive, unedited artifacts only)."
    )


@memory_app.command("screen-adoption")
def memory_screen_adoption(adoption_id: str) -> None:
    """Use the configured classifier to stage, never approve, a procedure candidate."""
    cfg = _init()
    from .artifact_adoptions.procedure_screen import stage_adoption

    model = cfg.model_for("classifier")
    console.print(
        "Screening the exact adopted text with "
        f"[bold]{model.provider}:{model.model}[/bold]. "
        "This explicit command may send that text to the configured provider."
    )
    with fts.cursor() as conn:
        try:
            result = stage_adoption(conn, cfg, adoption_id)
        except ValueError as exc:
            raise typer.BadParameter(str(exc)) from exc
    if result.candidate is None:
        console.print(f"[yellow]Not staged:[/yellow] {result.decision.rationale}")
        return
    candidate = result.candidate
    console.print(
        f"[green]Staged review-only candidate {candidate.id} "
        f"({candidate.status}).[/green] Review it with "
        f"`openchronicle memory show {candidate.id}`; approval remains explicit."
    )


@memory_app.command("candidates")
@privacy_egress_fenced
def memory_candidates(
    status: str = typer.Option(
        "pending,conflict", "--status", help="Comma-separated candidate statuses."
    ),
    limit: int = typer.Option(100, "--limit", "-n"),
) -> None:
    """List the local review inbox without exposing it over MCP."""
    cfg = _init()
    from .provenance.models import EvidenceRef
    from .services.evidence import EvidenceResolver
    from .services.memory import MemoryService

    statuses = [value.strip() for value in status.split(",") if value.strip()]
    with fts.cursor() as conn:
        service = MemoryService(conn, soft_limit_tokens=cfg.writer.soft_limit_tokens, cfg=cfg)
        service.resume_pending_purges()
        resolver = EvidenceResolver(conn, cfg)
        candidates = [
            candidate
            for candidate in service.list_candidates(statuses=statuses or None, limit=1000)
            if resolver.resolve(EvidenceRef(kind="memory_candidate", id=candidate.id))["status"]
            == "current"
        ][: max(limit, 0)]
    table = Table("ID", "Status", "Kind", "Target", "Version", "Content")
    for candidate in candidates:
        table.add_row(
            candidate.id,
            candidate.status,
            candidate.kind,
            candidate.target_path,
            str(candidate.version),
            candidate.content.replace("\n", " ")[:80],
        )
    console.print(table)


@memory_app.command("show")
@privacy_egress_fenced
def memory_candidate_show(candidate_id: str) -> None:
    """Show one proposal and its direct evidence."""
    cfg = _init()
    from .provenance import store as provenance_store
    from .provenance.models import EvidenceRef
    from .services.evidence import EvidenceResolver
    from .services.memory import MemoryService

    with fts.cursor() as conn:
        service = MemoryService(conn, soft_limit_tokens=cfg.writer.soft_limit_tokens, cfg=cfg)
        service.resume_pending_purges()
        candidate = service.get_candidate(candidate_id)
        if candidate is None:
            console.print(f"[red]Candidate not found: {candidate_id}[/red]")
            raise typer.Exit(1)
        if (
            EvidenceResolver(conn, cfg).resolve(
                EvidenceRef(kind="memory_candidate", id=candidate_id)
            )["status"]
            != "current"
        ):
            console.print(f"[red]Candidate not found: {candidate_id}[/red]")
            raise typer.Exit(1)
        payload = candidate.to_dict()
        payload["evidence"] = [
            ref.to_dict()
            for ref in provenance_store.direct_sources(
                conn, EvidenceRef(kind="memory_candidate", id=candidate_id)
            )
        ]
    console.print_json(data=payload)


@memory_app.command("edit")
@privacy_egress_fenced
def memory_candidate_edit(
    candidate_id: str,
    content: str = typer.Option(..., "--content"),
    tags: str = typer.Option("", "--tags", help="Comma-separated tags."),
    version: int | None = typer.Option(None, "--version"),
    conflict_key: str | None = typer.Option(None, "--conflict-key"),
) -> None:
    """Edit a pending proposal with optimistic version checking."""
    cfg = _init()
    from .provenance.models import EvidenceRef
    from .services.evidence import EvidenceResolver
    from .services.memory import MemoryService

    with fts.cursor() as conn:
        service = MemoryService(conn, soft_limit_tokens=cfg.writer.soft_limit_tokens, cfg=cfg)
        service.resume_pending_purges()
        current = service.get_candidate(candidate_id)
        if (
            current is None
            or EvidenceResolver(conn, cfg).resolve(
                EvidenceRef(kind="memory_candidate", id=candidate_id)
            )["status"]
            != "current"
        ):
            raise typer.BadParameter(f"candidate not found: {candidate_id}")
        updated = service.edit_candidate(
            candidate_id,
            expected_version=current.version if version is None else version,
            content=content,
            tags=[tag.strip() for tag in tags.split(",") if tag.strip()],
            conflict_key=conflict_key,
        )
    console.print(f"[green]Updated {updated.id} to version {updated.version}.[/green]")


@memory_app.command("approve")
def memory_candidate_approve(
    candidate_id: str,
    version: int | None = typer.Option(None, "--version"),
) -> None:
    """Approve and idempotently materialize one reviewed proposal."""
    cfg = _init()
    from .services.memory import MemoryService

    with fts.cursor() as conn:
        service = MemoryService(conn, soft_limit_tokens=cfg.writer.soft_limit_tokens, cfg=cfg)
        service.resume_pending_purges()
        current = service.get_candidate(candidate_id)
        if current is None:
            raise typer.BadParameter(f"candidate not found: {candidate_id}")
        approved = service.approve_candidate(
            candidate_id,
            expected_version=current.version if version is None else version,
        )
    console.print(
        f"[green]Accepted {approved.id} as {approved.target_path}#"
        f"{approved.applied_entry_id}.[/green]"
    )


@memory_app.command("reject")
@privacy_egress_fenced
def memory_candidate_reject(
    candidate_id: str,
    reason: str = typer.Option("", "--reason"),
    version: int | None = typer.Option(None, "--version"),
) -> None:
    """Reject a proposal while retaining its review history."""
    cfg = _init()
    from .provenance.models import EvidenceRef
    from .services.evidence import EvidenceResolver
    from .services.memory import MemoryService

    with fts.cursor() as conn:
        service = MemoryService(conn, soft_limit_tokens=cfg.writer.soft_limit_tokens, cfg=cfg)
        service.resume_pending_purges()
        current = service.get_candidate(candidate_id)
        if (
            current is None
            or EvidenceResolver(conn, cfg).resolve(
                EvidenceRef(kind="memory_candidate", id=candidate_id)
            )["status"]
            != "current"
        ):
            raise typer.BadParameter(f"candidate not found: {candidate_id}")
        rejected = service.reject_candidate(
            candidate_id,
            expected_version=current.version if version is None else version,
            reason=reason,
        )
    console.print(f"[green]Rejected {rejected.id}.[/green]")


@memory_app.command("forget")
def memory_candidate_forget(
    candidate_id: str,
    yes: bool = typer.Option(False, "--yes", help="Confirm permanent cascading purge."),
) -> None:
    """Permanently purge a proposal, accepted entry, and derived wraps."""
    if not yes:
        confirmed = typer.confirm("Permanently delete this proposal and all accepted/derived data?")
        if not confirmed:
            raise typer.Abort()
    cfg = _init()
    from .services.memory import MemoryService

    with fts.cursor() as conn:
        result = MemoryService(
            conn, soft_limit_tokens=cfg.writer.soft_limit_tokens, cfg=cfg
        ).purge_candidate(candidate_id)
    console.print(
        f"[green]Purged {candidate_id}; entry_removed={result.removed_entry}; "
        f"files_removed={len(result.removed_files)}; "
        f"wraps_invalidated={len(result.invalidated_wraps)}.[/green]"
    )


provenance_app = typer.Typer(help="Inspect the local evidence graph.")
app.add_typer(provenance_app, name="provenance")


@provenance_app.command("trace")
@privacy_egress_fenced
def provenance_trace(
    kind: str,
    artifact_id: str,
    path: str = typer.Option("", "--path"),
    depth: int = typer.Option(4, "--depth"),
) -> None:
    """Trace direct and transitive sources for a local artifact."""
    cfg = _init()
    from .daily_wrap import store as daily_wrap_store
    from .memory_candidates import store as candidate_store
    from .provenance import store as provenance_store
    from .provenance.models import EvidenceRef
    from .services.context import ContextService
    from .services.evidence import EvidenceResolver

    with fts.cursor() as conn:
        ref = EvidenceRef(kind=kind, id=artifact_id, path=path)
        allowed = False
        if kind == "daily_wrap_revision":
            allowed = bool(
                path
                and not candidate_store.is_tombstoned(conn, kind="daily_wrap", artifact_id=path)
                and daily_wrap_store.get_by_id(conn, path) is not None
                and provenance_store.availability(conn, ref) == "available"
                and ContextService(conn, cfg).daily_wrap_allowed(
                    path,
                    expected_row=daily_wrap_store.get_by_id(conn, path),
                )
                and ContextService(conn, cfg).evidence_allowed(ref)
            )
        else:
            canonical = ref
            if kind in {"observation", "timeline_block", "memory_entry"}:
                current_hash = provenance_store.current_content_hash(conn, ref)
                if current_hash:
                    canonical = EvidenceRef(
                        kind=ref.kind,
                        id=ref.id,
                        path=ref.path,
                        content_hash=current_hash,
                    )
            allowed = EvidenceResolver(conn, cfg).resolve(canonical)["status"] == "current"
        if not allowed:
            console.print("[yellow]Provenance subject not found.[/yellow]")
            raise typer.Exit(1)
        trace = provenance_store.trace_sources(conn, ref, max_depth=depth)
    console.print_json(data={"count": len(trace), "sources": trace})


daily_wrap_app = typer.Typer(help="Generate and read evidence-backed Daily Wraps.")
app.add_typer(daily_wrap_app, name="daily-wrap")


@daily_wrap_app.command("run")
def daily_wrap_run(
    day: str | None = typer.Option(None, "--date", help="Local date (YYYY-MM-DD)."),
    timezone: str | None = typer.Option(None, "--timezone", help="IANA timezone."),
) -> None:
    """Generate or refresh one canonical Daily Wrap."""
    cfg = _init()
    from .daily_wrap import worker as daily_wrap_worker
    from .daily_wrap.service import DailyWrapService
    from .services.context import ContextService

    try:
        zone_name = timezone or daily_wrap_worker.local_timezone_name(cfg)
    except ValueError as exc:
        raise typer.BadParameter(str(exc)) from exc
    target_day = _daily_wrap_day(day, zone_name)
    with fts.cursor() as conn:
        service = DailyWrapService(conn, cfg)
        generated = service.run(target_day, zone_name)
        # Provider latency must not hold the capture-store fence.  Reacquire
        # both cleanup locks only for the final canonical read, authorization,
        # and detached response copy.
        with privacy_egress_lock():
            row = service.get(target_day, zone_name)
            if (
                row is None
                or row.id != generated.id
                or not ContextService(conn, cfg).daily_wrap_allowed(
                    row.id,
                    expected_row=row,
                )
            ):
                console.print("[yellow]Daily Wrap failed publication validation.[/yellow]")
                raise typer.Exit(1)
            payload = row.to_dict()
    console.print_json(data=payload)


@daily_wrap_app.command("show")
@privacy_egress_fenced
def daily_wrap_show(
    day: str | None = typer.Option(None, "--date", help="Local date (YYYY-MM-DD)."),
    timezone: str | None = typer.Option(None, "--timezone", help="IANA timezone."),
) -> None:
    """Show the canonical wrap for one local day."""
    cfg = _init()
    from .daily_wrap import worker as daily_wrap_worker
    from .daily_wrap.service import DailyWrapService
    from .services.context import ContextService

    try:
        zone_name = timezone or daily_wrap_worker.local_timezone_name(cfg)
    except ValueError as exc:
        raise typer.BadParameter(str(exc)) from exc
    target_day = _daily_wrap_day(day, zone_name)
    with fts.cursor() as conn:
        row = DailyWrapService(conn, cfg).get(target_day, zone_name)
        if row is not None and not ContextService(conn, cfg).daily_wrap_allowed(
            row.id, expected_row=row
        ):
            row = None
    if row is None:
        console.print(f"[yellow]No Daily Wrap for {target_day} ({zone_name}).[/yellow]")
        raise typer.Exit(1)
    console.print_json(data=row.to_dict())


@daily_wrap_app.command("list")
@privacy_egress_fenced
def daily_wrap_list(
    limit: int = typer.Option(30, "--limit", "-n"),
) -> None:
    """List recent canonical wraps across timezones."""
    cfg = _init()
    from .daily_wrap.service import DailyWrapService
    from .services.context import ContextService

    with fts.cursor() as conn:
        context = ContextService(conn, cfg)
        rows = [
            row
            for row in DailyWrapService(conn, cfg).list(limit=365)
            if context.daily_wrap_allowed(row.id, expected_row=row)
        ][: max(limit, 0)]
    console.print_json(data={"count": len(rows), "wraps": [row.to_dict() for row in rows]})


def _local_timezone_name() -> str:
    configured = os.environ.get("TZ", "").strip()
    if configured:
        return configured
    tzinfo = datetime.now().astimezone().tzinfo
    key = getattr(tzinfo, "key", "")
    if key:
        return str(key)
    with contextlib.suppress(OSError):
        resolved = Path("/etc/localtime").resolve()
        marker = "zoneinfo/"
        if marker in str(resolved):
            return str(resolved).split(marker, 1)[1]
    raise typer.BadParameter("cannot infer IANA timezone; pass --timezone")


def _daily_wrap_day(value: str | None, timezone: str) -> date:
    try:
        zone = ZoneInfo(timezone)
    except ZoneInfoNotFoundError as exc:
        raise typer.BadParameter(f"unknown IANA timezone: {timezone}") from exc
    if value is None:
        return datetime.now(zone).date() - timedelta(days=1)
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise typer.BadParameter("date must be YYYY-MM-DD") from exc


@app.command("capture-once")
def capture_once() -> None:
    """Perform one capture immediately (useful for testing)."""
    cfg = _init()
    from .capture import ax_capture, scheduler

    provider = ax_capture.create_provider(
        depth=cfg.capture.ax_depth, timeout=cfg.capture.ax_timeout_seconds
    )
    path = scheduler.capture_once(
        cfg.capture,
        provider,
        trigger={"event_type": "manual"},
    )
    if path:
        console.print(f"[green]Wrote {path}[/green]")
    else:
        console.print("[red]Capture skipped or failed (check logs).[/red]")
        raise typer.Exit(1)


@app.command("rebuild-index")
def rebuild_index() -> None:
    """Rebuild SQLite FTS index from Markdown files on disk."""
    _init()
    with fts.cursor() as conn:
        files_count, entry_count = entries_mod.rebuild_index(conn)
        index_md.rebuild(conn)
    console.print(f"[green]Rebuilt: {files_count} files, {entry_count} entries.[/green]")


@app.command("rebuild-captures-index")
def rebuild_captures_index() -> None:
    """Backfill captures_fts from capture-buffer/*.json on disk.

    Re-runnable: existing rows are upserted via INSERT OR REPLACE, so this
    is safe to invoke any time the captures index has fallen out of sync
    (e.g. fresh upgrade onto a populated buffer, or an FTS write the
    capture worker logged but didn't commit).
    """
    _init()
    stats = capture_reconcile.reconcile_capture_index()
    console.print(
        f"[green]Captures index rebuilt: {stats.indexed} indexed, "
        f"{stats.removed} stale rows removed, {stats.skipped} invalid and "
        f"{stats.hidden} tombstoned files skipped (of {stats.scanned}).[/green]"
    )


@app.command()
def config() -> None:
    """Print the resolved config path and contents."""
    _init()
    p = paths.config_file()
    console.print(f"[bold]{p}[/bold]")
    console.print(p.read_text())


clean_app = typer.Typer(help="Delete past data. Destructive — use with care.")
app.add_typer(clean_app, name="clean")


def _confirm(prompt: str, yes: bool) -> bool:
    if yes:
        return True
    return typer.confirm(prompt, default=False)


def _warn_if_running() -> None:
    pid = _read_pid()
    if pid:
        console.print(
            f"[yellow]Warning: daemon is running (pid {pid}). "
            "Consider `openchronicle stop` first — new data may arrive mid-clean.[/yellow]"
        )


def _capture_clean_targets() -> list[Path]:
    buf = paths.capture_buffer_dir()
    if not buf.exists():
        return []
    return [
        path
        for path in buf.iterdir()
        if path.is_file()
        and (path.suffix == ".json" or capture_filenames.is_capture_temp_name(path.name))
    ]


def _memory_clean_targets() -> list[Path]:
    memory = paths.memory_dir()
    if not memory.exists():
        return []
    return [
        path
        for path in memory.rglob("*")
        if path.is_file() and (path.suffix == ".md" or files_mod.is_memory_temp_name(path.name))
    ]


def _clean_captures() -> int:
    # Provider calls hold the review fence but release the collection lock
    # during network I/O so ordinary capture writes continue. Explicit deletion
    # takes both in canonical order and therefore waits for any in-flight
    # provider before committing deny markers.
    with files_mod.review_operation_lock(), capture_store.capture_store_lock():
        from .capture import scheduler as capture_scheduler
        from .timeline import store as timeline_store

        captures = _capture_clean_targets()
        canonical = [path for path in captures if path.suffix == ".json"]
        canonical_names = {path.name for path in canonical}
        # Commit deny-read markers and clear every searchable projection before
        # touching authoritative files. A failed unlink remains hidden from raw
        # reads and rebuilds, and the command reports failure instead of claiming
        # the plaintext was removed.
        with fts.cursor() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                for path in canonical:
                    candidate_store.put_tombstone(conn, kind="capture_file", artifact_id=path.name)
                conn.execute("DELETE FROM captures")
                for receipt in timeline_store.window_receipts_in_raw_states(
                    conn,
                    "live",
                ):
                    manifest_names = {
                        binding[0]
                        for binding in timeline_store.capture_bindings_for_window(
                            conn,
                            receipt,
                        )
                    }
                    if (
                        manifest_names
                        and manifest_names.issubset(canonical_names)
                        and timeline_store.window_receipt_is_current(conn, receipt)
                    ):
                        timeline_store.transition_window_receipt_raw_state(
                            conn,
                            receipt,
                            "retiring",
                        )
                conn.execute("COMMIT")
            except BaseException:
                if conn.in_transaction:
                    conn.execute("ROLLBACK")
                raise
        removed = 0
        removed_paths: list[Path] = []
        failures: list[tuple[Path, OSError]] = []
        for path in captures:
            try:
                path.unlink()
                removed += 1
                removed_paths.append(path)
            except OSError as exc:
                failures.append((path, exc))
        capture_scheduler._finish_capture_unlinks(
            removed=[path for path in removed_paths if path.suffix == ".json"],
            failures=[(path, exc) for path, exc in failures if path.suffix == ".json"],
            operation="explicit capture cleanup",
        )
        with fts.cursor() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                invalidated_live_proof = False
                if canonical_names:
                    remaining_windows = conn.execute(
                        """
                        SELECT DISTINCT window_start, window_end
                          FROM timeline_capture_receipts
                         WHERE capture_path IN ({})
                        """.format(",".join("?" for _ in canonical_names)),
                        tuple(sorted(canonical_names)),
                    ).fetchall()
                    for start_raw, end_raw in remaining_windows:
                        start = end = None
                        try:
                            start = datetime.fromisoformat(start_raw)
                            end = datetime.fromisoformat(end_raw)
                        except (TypeError, ValueError):
                            receipt = None
                        else:
                            receipt = timeline_store.window_receipt_for(
                                conn,
                                start,
                                end,
                            )
                        if receipt is not None and receipt.raw_state == "retiring":
                            continue
                        if start is not None and end is not None:
                            timeline_store.delete_window_receipt_for(conn, start, end)
                        else:
                            conn.execute(
                                "DELETE FROM timeline_window_receipts "
                                "WHERE window_start=? AND window_end=?",
                                (start_raw, end_raw),
                            )
                        conn.execute(
                            "DELETE FROM timeline_capture_receipts "
                            "WHERE window_start=? AND window_end=?",
                            (start_raw, end_raw),
                        )
                        invalidated_live_proof = True
                if invalidated_live_proof:
                    # Explicit raw deletion is allowed to create a historical
                    # evidence gap, but must never leave a live receipt or
                    # coverage proof claiming the removed bytes still exist.
                    conn.execute("DELETE FROM timeline_state")
                    conn.execute("DELETE FROM timeline_replay_state")
                    fts.bump_content_generation(conn, "timeline")
                    fts.bump_content_generation(conn, "reducer")
                for path in removed_paths:
                    if path.suffix == ".json" and (
                        conn.execute(
                            "SELECT 1 FROM timeline_capture_receipts WHERE capture_path=? LIMIT 1",
                            (path.name,),
                        ).fetchone()
                        is None
                    ):
                        candidate_store.delete_tombstone(
                            conn,
                            kind="capture_file",
                            artifact_id=path.name,
                        )
                conn.execute("COMMIT")
            except BaseException:
                if conn.in_transaction:
                    conn.execute("ROLLBACK")
                raise
        if failures:
            raise RuntimeError(
                f"capture cleanup incomplete: {len(failures)} file(s) could not be removed"
            ) from failures[0][1]
        return removed


_TIMELINE_AFFECTED_CANDIDATES_SQL = """
WITH RECURSIVE descendants(kind, id, path) AS (
    SELECT subject_kind, subject_id, subject_path
      FROM provenance_edges
     WHERE source_kind='timeline_block'
    UNION
    SELECT edge.subject_kind, edge.subject_id, edge.subject_path
      FROM provenance_edges AS edge
      JOIN descendants AS prior
        ON edge.source_kind=prior.kind
       AND edge.source_id=prior.id
       AND edge.source_path=prior.path
)
SELECT DISTINCT candidate.id AS subject_id
  FROM descendants
  JOIN memory_candidates AS candidate
    ON descendants.kind='memory_candidate' AND candidate.id=descendants.id
 WHERE candidate.status IN ('pending', 'conflict')
"""


def _timeline_clean_counts(conn) -> dict[str, int]:
    return {
        "blocks": int(conn.execute("SELECT COUNT(*) FROM timeline_blocks").fetchone()[0]),
        "wraps": int(conn.execute("SELECT COUNT(*) FROM daily_wrap_jobs").fetchone()[0]),
        "revisions": int(conn.execute("SELECT COUNT(*) FROM daily_wrap_revisions").fetchone()[0]),
        "candidates": len(conn.execute(_TIMELINE_AFFECTED_CANDIDATES_SQL).fetchall()),
    }


def _clean_timeline() -> int:
    with files_mod.review_operation_lock(), fts.cursor() as conn:
        n = conn.execute("SELECT COUNT(*) FROM timeline_blocks").fetchone()[0]
        conn.execute("BEGIN IMMEDIATE")
        try:
            if (
                conn.execute(
                    "SELECT 1 FROM timeline_window_receipts WHERE raw_state='retiring' LIMIT 1"
                ).fetchone()
                is not None
            ):
                raise RuntimeError("timeline cleanup blocked by in-progress capture retirement")
            fts.bump_content_generation(conn, "reducer")
            fts.bump_content_generation(conn, "timeline")
            affected_candidates = conn.execute(_TIMELINE_AFFECTED_CANDIDATES_SQL).fetchall()
            conn.executemany(
                """
                UPDATE memory_candidates
                   SET status='conflict',
                       last_error='source timeline was explicitly deleted'
                 WHERE id=? AND status IN ('pending', 'conflict')
                """,
                ((row["subject_id"],) for row in affected_candidates),
            )
            conn.execute("DELETE FROM daily_wrap_revisions")
            conn.execute("DELETE FROM daily_wrap_jobs")
            conn.execute("DELETE FROM classifier_jobs")
            conn.execute(
                """
                UPDATE sessions
                   SET classified_end=COALESCE(
                           end_time, flush_end, classified_end, start_time
                       ),
                       classifier_terminal_pending=0,
                       classifier_terminal_entry_id='',
                       classifier_terminal_path='',
                       classifier_terminal_noop=0,
                       updated_at=?
                """,
                (datetime.now().astimezone().isoformat(),),
            )
            conn.execute(
                """
                DELETE FROM provenance_edges
                 WHERE subject_kind IN (
                           'timeline_block', 'daily_wrap',
                           'daily_wrap_item', 'daily_wrap_revision'
                       )
                    OR source_kind='timeline_block'
                """
            )
            conn.execute("DELETE FROM timeline_blocks")
            conn.execute("DELETE FROM timeline_state")
            conn.execute("DELETE FROM timeline_replay_state")
            conn.execute("DELETE FROM timeline_window_receipts")
            conn.execute("DELETE FROM timeline_capture_receipts")
            conn.execute("DELETE FROM timeline_capture_receipt_state")
            conn.execute("DELETE FROM timeline_window_receipt_epoch")
            conn.execute("DELETE FROM timeline_receipt_audit_state")
            conn.execute("COMMIT")
        except Exception:  # noqa: BLE001
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise
    return n


def _clean_memory() -> tuple[int, int]:
    """Delete memory files/temp copies and reset indexes. Returns (files, entries)."""
    # Fence classifier proposal transactions as well as Markdown writers. A
    # worker holding an old classifier lease cannot recreate a candidate after
    # this explicit local reset because its job row is deleted atomically.
    with files_mod.review_operation_lock(), files_mod.store_write_lock():
        targets = _memory_clean_targets()
        canonical = [
            path for path in targets if path.suffix == ".md" and path.parent == paths.memory_dir()
        ]
        # File-level deny markers close direct-read and rebuild paths if an
        # unlink fails after projections have been cleared.
        with fts.cursor() as conn:
            from .activity import store as activity_store

            entries = conn.execute("SELECT COUNT(*) FROM entries").fetchone()[0]
            conn.execute("BEGIN IMMEDIATE")
            try:
                fts.bump_content_generation(conn, "reducer")
                for path in canonical:
                    candidate_store.put_tombstone(conn, kind="memory_file", artifact_id=path.name)
                conn.execute("DELETE FROM entries")
                conn.execute("DELETE FROM files")
                activity_store.clear(conn)
                conn.execute("DELETE FROM memory_candidates")
                conn.execute("DELETE FROM classifier_jobs")
                conn.execute(
                    """
                    UPDATE sessions
                       SET classified_end=COALESCE(
                               end_time, flush_end, classified_end, start_time
                           ),
                           classifier_terminal_pending=0,
                           classifier_terminal_entry_id='',
                           classifier_terminal_path='',
                           classifier_terminal_noop=0,
                           updated_at=?
                    """,
                    (datetime.now().astimezone().isoformat(),),
                )
                conn.execute("DELETE FROM daily_wrap_revisions")
                conn.execute("DELETE FROM daily_wrap_jobs")
                conn.execute(
                    """
                    DELETE FROM provenance_edges
                     WHERE subject_kind IN (
                           'memory_entry', 'memory_candidate',
                           'daily_wrap', 'daily_wrap_item', 'daily_wrap_revision'
                     )
                        OR source_kind IN ('memory_entry', 'memory_candidate')
                    """
                )
                conn.execute("COMMIT")
            except Exception:  # noqa: BLE001
                if conn.in_transaction:
                    conn.execute("ROLLBACK")
                raise
        removed = 0
        removed_paths: list[Path] = []
        failures: list[OSError] = []
        for path in targets:
            try:
                path.unlink()
                removed += 1
                removed_paths.append(path)
            except OSError as exc:
                failures.append(exc)
        with fts.cursor() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                for path in removed_paths:
                    if path in canonical:
                        candidate_store.delete_tombstone(
                            conn, kind="memory_file", artifact_id=path.name
                        )
                if not failures:
                    conn.execute(
                        """
                        DELETE FROM purge_tombstones
                         WHERE kind IN (
                               'memory_candidate', 'memory_entry',
                               'memory_file', 'daily_wrap'
                         )
                        """
                    )
                conn.execute("COMMIT")
            except BaseException:
                if conn.in_transaction:
                    conn.execute("ROLLBACK")
                raise
        if failures:
            raise RuntimeError(
                f"memory cleanup incomplete: {len(failures)} file(s) could not be removed"
            ) from failures[0]
        return removed, entries


def _clean_writer_state() -> bool:
    p = paths.writer_state()
    if p.exists():
        p.unlink()
        return True
    return False


@clean_app.command("captures")
def clean_captures(
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip confirmation."),
) -> None:
    """Delete all files in the capture buffer."""
    _init()
    buf = paths.capture_buffer_dir()
    count = len(_capture_clean_targets())
    console.print(f"About to delete {count} capture file(s) under {buf}")
    _warn_if_running()
    if not _confirm("Proceed?", yes):
        console.print("[yellow]Aborted.[/yellow]")
        raise typer.Exit(1)
    n = _clean_captures()
    console.print(f"[green]Deleted {n} capture file(s).[/green]")


@clean_app.command("timeline")
def clean_timeline(
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip confirmation."),
) -> None:
    """Delete timeline blocks, derived wraps, and dependent provenance."""
    _init()
    with fts.cursor() as conn:
        counts = _timeline_clean_counts(conn)
    console.print(
        f"About to delete {counts['blocks']} timeline block(s).\n"
        f"This also permanently deletes {counts['wraps']} Daily Wrap(s), "
        f"{counts['revisions']} wrap revision(s), their provenance, and marks "
        f"{counts['candidates']} dependent pending/conflict candidate(s) as conflict."
    )
    _warn_if_running()
    if not _confirm("Proceed?", yes):
        console.print("[yellow]Aborted.[/yellow]")
        raise typer.Exit(1)
    n = _clean_timeline()
    console.print(f"[green]Deleted {n} timeline block(s).[/green]")


@clean_app.command("memory")
def clean_memory(
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip confirmation."),
) -> None:
    """Delete Markdown, review candidates, wraps, and memory provenance."""
    _init()
    mem = paths.memory_dir()
    memory_count = len(_memory_clean_targets())
    with fts.cursor() as conn:
        entry_count = conn.execute("SELECT COUNT(*) FROM entries").fetchone()[0]
        file_count = conn.execute("SELECT COUNT(*) FROM files").fetchone()[0]
        candidate_count = conn.execute("SELECT COUNT(*) FROM memory_candidates").fetchone()[0]
        wrap_count = conn.execute("SELECT COUNT(*) FROM daily_wrap_jobs").fetchone()[0]
        revision_count = conn.execute("SELECT COUNT(*) FROM daily_wrap_revisions").fetchone()[0]
    console.print(
        f"About to delete {memory_count} memory file(s) under {mem} "
        f"and reset {entry_count} entries / {file_count} files in the index.\n"
        f"This also permanently deletes {candidate_count} review candidate(s), "
        f"{wrap_count} Daily Wrap(s), {revision_count} wrap revision(s), "
        "purge intents, and their memory provenance."
    )
    _warn_if_running()
    if not _confirm("Proceed?", yes):
        console.print("[yellow]Aborted.[/yellow]")
        raise typer.Exit(1)
    files, entries = _clean_memory()
    console.print(
        f"[green]Deleted {files} Markdown file(s); cleared {entries} index entries, "
        "review candidates, Daily Wraps, and memory provenance.[/green]"
    )


@clean_app.command("all")
def clean_all(
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip confirmation."),
) -> None:
    """Delete captures, timeline blocks, memory, and writer state. Config is kept."""
    _init()
    capture_count = len(_capture_clean_targets())
    memory_count = len(_memory_clean_targets())
    with fts.cursor() as conn:
        entry_count = conn.execute("SELECT COUNT(*) FROM entries").fetchone()[0]
        tlb_count = conn.execute("SELECT COUNT(*) FROM timeline_blocks").fetchone()[0]
        candidate_count = conn.execute("SELECT COUNT(*) FROM memory_candidates").fetchone()[0]
        wrap_count = conn.execute("SELECT COUNT(*) FROM daily_wrap_jobs").fetchone()[0]

    console.print(
        "[bold red]This will delete:[/bold red]\n"
        f"  - {capture_count} capture file(s)\n"
        f"  - {tlb_count} timeline block(s)\n"
        f"  - {memory_count} memory file(s) and {entry_count} index entries\n"
        f"  - {candidate_count} review candidate(s) and {wrap_count} Daily Wrap(s)\n"
        f"  - writer state\n"
        "[bold]Config ({}) is kept.[/bold]".format(paths.config_file())
    )
    _warn_if_running()
    if not _confirm("Proceed with full wipe?", yes):
        console.print("[yellow]Aborted.[/yellow]")
        raise typer.Exit(1)

    c = _clean_captures()
    t = _clean_timeline()
    f, e = _clean_memory()
    s = _clean_writer_state()
    console.print(
        f"[green]Done. Removed {c} captures, {t} timeline blocks, "
        f"{f} memory files, {e} index entries, writer_state={s}.[/green]"
    )


if __name__ == "__main__":
    app()
