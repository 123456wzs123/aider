"""aider-agent CLI: an agent shell around aider's edit engine."""

from __future__ import annotations

import argparse
import os
import sys
import traceback
from pathlib import Path

from aider import __version__, models
from aider.main import (
    get_git_root,
    load_dotenv_files,
    register_litellm_models,
    register_models,
)
from aider.repo import GitRepo

from .llm import ContextWindowError
from .mcp import MCPManager, load_server_configs
from .permissions import PermissionPolicy
from .prompt import build_system_prompt
from .repo_context import ConsoleIO, RepoContext, current_branch
from .runtime import AgentRuntime
from .session import SessionStore
from .tools import ToolContext, build_registry
from .ui import AgentUI


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="aider-agent",
        description="Tool-calling coding agent built on aider's edit engine.",
    )
    parser.add_argument("--model", metavar="MODEL", default=None)
    parser.add_argument("-m", "--message", default=None, help="run one task and exit")
    parser.add_argument("--yes", action="store_true", help="approve all shell commands")
    parser.add_argument("--dry-run", action="store_true", help="show actions without doing them")
    parser.add_argument("--max-steps", type=int, default=40, help="tool steps per user turn")
    parser.add_argument("--map-tokens", type=int, default=None, help="repo map budget (0 disables)")
    parser.add_argument("--no-auto-commits", action="store_true")
    parser.add_argument("--no-stream", action="store_true")
    parser.add_argument(
        "--mcp-config",
        action="append",
        default=[],
        metavar="PATH",
        help="extra MCP config file (repeatable)",
    )
    parser.add_argument("--no-mcp", action="store_true", help="do not load MCP servers")
    parser.add_argument("--ask-mcp", action="store_true", help="ask before each MCP tool call")
    parser.add_argument(
        "--trust-project-mcp",
        action="store_true",
        help="start MCP servers from project configs without asking",
    )
    parser.add_argument("--continue", dest="continue_session", action="store_true")
    parser.add_argument("--plain", action="store_true", help="no colors, boxes or glyphs")
    parser.add_argument("--verbose", action="store_true")
    parser.add_argument("--version", action="version", version=f"aider-agent {__version__}")
    parser.add_argument("paths", nargs="*", help="optional files to focus on")
    return parser


def parse_args(argv=None):
    return build_parser().parse_args(argv)


def with_paths(text: str, paths) -> str:
    if not paths:
        return text
    return f"{text}\n\nFocus files: {', '.join(paths)}"


def _try_repo(io, root):
    try:
        return GitRepo(io, None, str(root))
    except Exception:
        return None


def _setup_mcp(registry, root, ui, args):
    if args.no_mcp:
        return None

    configs, warnings = load_server_configs(root, args.mcp_config)
    for warning in warnings:
        ui.warning(warning)
    if args.ask_mcp:
        for cfg in configs:
            cfg.approval = "ask"
    if not configs:
        if args.mcp_config:
            ui.warning("no MCP servers found in the given config")
        return None

    manager = MCPManager(configs, root=root, ui=ui, verbose=args.verbose)
    if any(cfg.project for cfg in configs if not cfg.disabled):
        ui.info("project MCP config found; each server needs approval")

    def approve_project(cfg):
        if args.yes or args.trust_project_mcp:
            return True
        return ui.confirm_mcp_server(cfg)

    try:
        statuses = manager.connect_all(project_approver=approve_project)
    except BaseException:
        manager.close()
        raise
    connected = [status for status in statuses if status.connected]
    for status in statuses:
        if status.error:
            ui.warning(f"mcp {status.name}: {status.error}")
    if connected:
        manager.register_tools(registry)
        summary = ", ".join(f"{status.name} ({status.tools})" for status in connected)
        ui.info(f"mcp connected: {summary}")
        if args.verbose:
            for name in registry.names():
                if name.startswith("mcp__"):
                    ui.info(f"  {name}")
    return manager


def main(argv=None) -> int:
    args = parse_args(argv)
    root = Path(get_git_root() or os.getcwd()).resolve()
    os.chdir(root)

    history_path = Path.home() / ".aider-agent" / "history"
    ui = AgentUI(plain=args.plain, verbose=args.verbose, history_path=history_path)
    console_io = ConsoleIO(ui)

    load_dotenv_files(str(root), None)
    register_models(str(root), None, console_io, verbose=args.verbose)
    register_litellm_models(str(root), None, console_io, verbose=args.verbose)

    model_name = args.model or models.DEFAULT_MODEL_NAME
    try:
        model = models.Model(model_name, weak_model=False, editor_model=False, verbose=args.verbose)
    except Exception as exc:
        ui.error(f"cannot load model {model_name}: {exc}")
        return 1

    repo = _try_repo(console_io, root)
    if repo is None:
        ui.warning("not a git repository; auto-commit is disabled")

    context = RepoContext(root, model, ui=ui, map_tokens=args.map_tokens)
    registry = build_registry()
    mcp_manager = _setup_mcp(registry, root, ui, args)
    mcp_servers = mcp_manager.connected_names() if mcp_manager is not None else None
    permissions = PermissionPolicy(yes=args.yes, dry_run=args.dry_run)
    tool_context = ToolContext(
        root=root,
        valid_fnames=context.tracked_files(),
        permissions=permissions,
        ui=ui,
        dry_run=args.dry_run,
        repo=repo,
    )
    runtime = AgentRuntime(
        model,
        registry,
        build_system_prompt(root, mcp_servers=mcp_servers),
        ui=ui,
        root=root,
        max_steps=args.max_steps,
        repo=repo,
        auto_commits=not args.no_auto_commits and repo is not None,
        dry_run=args.dry_run,
        tool_context=tool_context,
        stream=not args.no_stream,
    )

    session = SessionStore(root)
    if args.continue_session:
        payload = session.load()
        if payload:
            restored = runtime.load_blocks(payload["blocks"])
            ui.info(f"resumed {restored} block(s) from {payload['_path']}")
        else:
            ui.warning("no previous session found")

    ui.header(
        root.name or str(root),
        current_branch(root),
        model.name,
        "search/replace",
        runtime.auto_commits,
        "yolo" if args.yes else "ask",
        mcp=mcp_manager.connected_count() if mcp_manager is not None else 0,
    )

    def ensure_map():
        if runtime.repo_map is None and not runtime.blocks and context.map_tokens:
            ui.info("building repo map…")
            runtime.repo_map = context.repo_map()

    try:
        if args.message:
            ensure_map()
            task = with_paths(args.message, args.paths)
            ui.user(task)
            try:
                runtime.run_turn(task)
            finally:
                ui.assistant_end()
            session.save(runtime.blocks, model=model.name)
            return 0 if not runtime.stopped_at_limit else 2
        return _repl(runtime, context, session, ui, model, args, ensure_map, mcp_manager)
    except KeyboardInterrupt:
        ui.info("cancelled")
        return 130
    except ContextWindowError as exc:
        ui.error(f"context window exceeded: {exc}")
        return 1
    finally:
        if mcp_manager is not None:
            mcp_manager.close()


def _repl(runtime, context, session, ui, model, args, ensure_map, mcp_manager=None) -> int:
    while True:
        try:
            line = ui.read_input()
        except EOFError:
            ui.info("")
            return 0
        except KeyboardInterrupt:
            ui.info("")
            continue

        text = line.strip()
        if not text:
            continue

        if text.startswith("/"):
            command = text.split()[0].lower()
            if command in ("/exit", "/quit"):
                return 0
            if command == "/clear":
                runtime.blocks.clear()
                ui.info("history cleared")
                continue
            if command == "/map":
                ui.info("building repo map…")
                runtime.repo_map = context.repo_map()
                ui.info("repo map updated" if runtime.repo_map else "repo map unavailable")
                continue
            if command == "/mcp":
                lines = (
                    mcp_manager.status_lines()
                    if mcp_manager is not None
                    else ["no MCP servers configured"]
                )
                for line in lines:
                    ui.info(line)
                continue
            if command == "/help":
                ui.info("commands: /exit /clear /map /mcp /help")
                ui.info("tools: " + ", ".join(runtime.registry.names()))
                continue
            ui.warning(f"unknown command: {command}")
            continue

        ensure_map()
        ui.user(text)
        try:
            runtime.run_turn(with_paths(text, args.paths))
        except ContextWindowError as exc:
            ui.error(f"context window exceeded: {exc}")
        except KeyboardInterrupt:
            ui.info("cancelled")
        except Exception as exc:
            detail = traceback.format_exc() if args.verbose else None
            ui.error(f"{type(exc).__name__}: {exc}", detail=detail)
        finally:
            ui.assistant_end()

        try:
            session.save(runtime.blocks, model=model.name)
        except Exception as exc:
            ui.warning(f"could not save session: {exc}")


if __name__ == "__main__":
    sys.exit(main())
