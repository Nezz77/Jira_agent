"""
 ▄▄▄· ▄▄ • ▄▄▄ . ▐ ▄ ▄▄▄▄▄      ▄▄ • ▄▄▄   ▄▄▄·  ▌ ▐·▪  ▄▄▄▄▄ ▄· ▄▌
▐█ ▀█ ▐█ ▀ ▪▀▄.▀·•█▌▐█•██  ▪     ▐█ ▀ ▪▀▄ █·▐█ ▀█ ▪█·█▌██ •██  ▐█▪██▌
▄█▀▀█ ▄█ ▀█▄▐▀▀▪▄▐█▐▐▌ ▐█.▪ ▄█▀▄ ▄█ ▀█▄▐▀▀▄ ▄█▀▀█ ▐█▐█•▐█· ▐█.▪▐█▌▐█▪
▐█ ▪▐▌▐█▄▪▐█▐█▄▄▌██▐█▌ ▐█▌·▐█▌.▐▌▐█▄▪▐█▐█•█▌▐█ ▪▐▌ ███ ▐█▌ ▐█▌· ▐█▀·.
 ▀  ▀ ·▀▀▀▀  ▀▀▀ ▀▀ █▪ ▀▀▀  ▀█▄▀▪·▀▀▀▀ .▀  ▀ ▀  ▀ . ▀  ▀▀▀ ▀▀▀   ▀ •

agent.py  —  Jira Agile Agent  (Powered by Gemini + Antigravity 🚀)
────────────────────────────────────────────────────────────────────
This is the main orchestrator script that coordinates all three stages:

  Stage 1 ── PDF Ingestion    (pdf_ingestion.py)
  Stage 2 ── Agile Decompose  (ai_decomposer.py)
  Stage 3 ── Jira Board Build (jira_builder.py)

Usage:
  python agent.py --pdf proposal.pdf --members "Alice,Bob,Carol"
  python agent.py --pdf proposal.pdf --members "Alice,Bob" --dry-run
  python agent.py --pdf proposal.pdf --members "Alice" --project-key DEMO

Run `python agent.py --help` for the full option list.
"""

from __future__ import annotations

# ══════════════════════════════════════════════════════════════════════════════
#  (Easter egg removed for sanity)
# ══════════════════════════════════════════════════════════════════════════════

import argparse
import os
import sys
import time
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv
from rich.panel import Panel
from rich.rule import Rule

# Local modules
from ai_decomposer import decompose_to_agile_backlog
from jira_builder import build_jira_board
from pdf_ingestion import ingest_pdf
from utils import console, fatal, print_banner, print_summary_table, setup_logging

# ── Logging ───────────────────────────────────────────────────────────────────
logger = setup_logging(log_file="agent.log")

# ── Environment ───────────────────────────────────────────────────────────────
# Load .env from the directory where this script lives (not necessarily CWD).
_SCRIPT_DIR = Path(__file__).parent.resolve()
load_dotenv(dotenv_path=_SCRIPT_DIR / ".env", override=False)


# ══════════════════════════════════════════════════════════════════════════════
#  CLI Argument Parsing
# ══════════════════════════════════════════════════════════════════════════════

def parse_args() -> argparse.Namespace:
    """Define and parse all command-line arguments."""
    parser = argparse.ArgumentParser(
        prog="agent.py",
        description=(
            "Jira Agile Agent — Upload a project proposal PDF, have Gemini AI "
            "decompose it into Agile Epics/Stories/Tasks, and push them to Jira automatically."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python agent.py --pdf proposal.pdf --members "Alice,Bob,Carol"
  python agent.py --pdf proposal.pdf --members "alice@co.com,bob@co.com" --dry-run
  python agent.py --pdf proposal.pdf --members "Alice" --project-key DEMO --skip-stage 3
        """,
    )

    # ── Required ──────────────────────────────────────────────────────────────
    parser.add_argument(
        "--pdf",
        required=True,
        metavar="PATH",
        help="Path to the project proposal PDF file.",
    )

    # ── Optional ──────────────────────────────────────────────────────────────
    parser.add_argument(
        "--members",
        default="",
        metavar="\"Name1,Name2,...\"",
        help=(
            "Comma-separated list of team member names or emails. "
            "Work will be distributed evenly across all members. "
            "Names/emails must match Jira user accounts for assignment to work."
        ),
    )

    parser.add_argument(
        "--project-key",
        default=None,
        metavar="KEY",
        help=(
            "Jira project key (e.g. PROJ). "
            "Overrides JIRA_PROJECT_KEY from .env."
        ),
    )

    parser.add_argument(
        "--project-name",
        default=None,
        metavar="NAME",
        help=(
            "Human-readable project name (used when creating a new Jira project). "
            "Overrides JIRA_PROJECT_NAME from .env."
        ),
    )

    parser.add_argument(
        "--model",
        default=None,
        metavar="MODEL",
        help="Gemini model name (e.g. gemini-3.6-flash, gemini-2.5-pro). "
             "Overrides GEMINI_MODEL from .env.",
    )

    parser.add_argument(
        "--sprints",
        type=int,
        default=1,
        metavar="N",
        help=(
            "Number of sprints to create on the Jira board (default: 1). "
            "Stories will be distributed evenly across all sprints."
        ),
    )

    parser.add_argument(
        "--modules-out",
        default="modules.txt",
        metavar="PATH",
        help="Output path for the modules text file (default: modules.txt).",
    )

    parser.add_argument(
        "--backlog-out",
        default="backlog.json",
        metavar="PATH",
        help="Output path for the Agile backlog JSON file (default: backlog.json).",
    )

    parser.add_argument(
        "--dry-run",
        action="store_true",
        help=(
            "Run all Gemini stages but do NOT create any Jira issues. "
            "Useful for previewing the backlog before committing to Jira."
        ),
    )

    parser.add_argument(
        "--skip-stage",
        type=int,
        choices=[1, 2, 3],
        default=None,
        metavar="N",
        help=(
            "Skip stage N and start from a previously saved intermediate file. "
            "  --skip-stage 1 → use existing modules.txt (skip PDF ingestion). "
            "  --skip-stage 2 → use existing backlog.json (skip AI decomposition)."
            "  (skip-stage 3 is equivalent to --dry-run)"
        ),
    )

    return parser.parse_args()


# ══════════════════════════════════════════════════════════════════════════════
#  Environment Validation
# ══════════════════════════════════════════════════════════════════════════════

def load_and_validate_env(args: argparse.Namespace) -> dict:
    """
    Load all required environment variables and CLI overrides into a single
    config dict. Raises SystemExit if any required variable is missing.

    Returns:
        Dict with keys: gemini_api_key, gemini_model, jira_domain,
                        jira_email, jira_api_token, jira_project_key,
                        jira_project_name.
    """
    def require(var: str, override: str | None = None) -> str:
        value = override or os.getenv(var, "").strip()
        if not value:
            fatal(
                f"Missing required config: '{var}'.\n"
                f"  Set it in your .env file or pass the corresponding CLI flag.\n"
                f"  See .env.example for all required variables."
            )
        return value

    config = {
        "gemini_api_key":    require("GEMINI_API_KEY"),
        "gemini_model":      args.model or os.getenv("GEMINI_MODEL", "gemini-3.6-flash").strip(),
        "jira_domain":       require("JIRA_DOMAIN"),
        "jira_email":        require("JIRA_EMAIL"),
        "jira_api_token":    require("JIRA_API_TOKEN"),
        "jira_project_key":  require("JIRA_PROJECT_KEY", args.project_key),
        "jira_project_name": require("JIRA_PROJECT_NAME", args.project_name),
    }

    return config


# ══════════════════════════════════════════════════════════════════════════════
#  Interactive Helpers
# ══════════════════════════════════════════════════════════════════════════════

def collect_members_interactively() -> list[str]:
    """
    Prompt the user to enter team member names one by one in the terminal.
    Called when --members is not provided and stdin is a TTY (interactive mode).

    Returns an empty list if the user skips (presses Enter with no input first).
    """
    from rich.prompt import Prompt

    console.print(
        "\n  [bold yellow]No team members specified.[/bold yellow]\n"
        "  Enter team member names one per line.\n"
        "  Press [bold]Enter[/bold] on a blank line when done, "
        "or type [bold]skip[/bold] to leave issues unassigned.\n"
    )

    members: list[str] = []
    index = 1
    while True:
        name = Prompt.ask(f"  Member {index}", default="").strip()
        if name.lower() in ("", "skip", "done", "q"):
            break
        members.append(name)
        index += 1

    if members:
        console.print(
            f"\n  [green]✓[/green] Team members: [bold]{', '.join(members)}[/bold] "
            f"({len(members)} members)\n"
        )
    else:
        console.print(
            "  [yellow]⚠[/yellow] No members entered — issues will be unassigned.\n"
        )
    return members


# ══════════════════════════════════════════════════════════════════════════════
#  Stage Skipping — Load from cached intermediate files
# ══════════════════════════════════════════════════════════════════════════════

def load_modules_from_file(path: str) -> list[str]:
    """
    Load a previously saved modules.txt file to skip Stage 1.

    Each non-blank, non-comment line is treated as one module name.
    Lines matching `N. Module Name` (numbered list) are also handled.
    """
    import re

    modules_path = Path(path).resolve()
    if not modules_path.exists():
        fatal(f"modules.txt not found at: {modules_path}\nCannot skip Stage 1.")

    modules = []
    with modules_path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            # Strip leading numbering e.g. "1. Module Name"
            line = re.sub(r"^\d+\.\s*", "", line)
            if line:
                modules.append(line)

    if not modules:
        fatal(f"modules.txt exists but contains no modules: {modules_path}")

    logger.info("Loaded %d modules from %s (Stage 1 skipped)", len(modules), modules_path)
    return modules


def load_backlog_from_file(path: str) -> list[dict]:
    """
    Load a previously saved backlog.json file to skip Stage 2.
    """
    import json

    backlog_path = Path(path).resolve()
    if not backlog_path.exists():
        fatal(f"backlog.json not found at: {backlog_path}\nCannot skip Stage 2.")

    with backlog_path.open("r", encoding="utf-8") as f:
        backlog = json.load(f)

    if not isinstance(backlog, list) or not backlog:
        fatal(f"backlog.json is empty or malformed: {backlog_path}")

    logger.info("Loaded %d epics from %s (Stage 2 skipped)", len(backlog), backlog_path)
    return backlog


# ══════════════════════════════════════════════════════════════════════════════
#  Main Orchestrator
# ══════════════════════════════════════════════════════════════════════════════

def main() -> None:
    args = parse_args()

    # ── Welcome banner ────────────────────────────────────────────────────────
    console.print(
        Panel.fit(
            "[bold bright_blue]🚀 Jira Agile Agent[/bold bright_blue]\n"
            "[dim]Powered by [green]Gemini AI[/green] + [cyan]Antigravity[/cyan][/dim]\n"
            f"[dim]Started: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}[/dim]",
            border_style="bright_blue",
        )
    )

    # ── Load & validate config ────────────────────────────────────────────────
    config = load_and_validate_env(args)

    # ── Parse team members ────────────────────────────────────────────────────
    members: list[str] = []
    if args.members.strip():
        members = [m.strip() for m in args.members.split(",") if m.strip()]

    # If no members provided and we're in an interactive terminal, prompt for them
    if not members and sys.stdin.isatty():
        members = collect_members_interactively()

    if not members:
        console.print(
            "  [yellow]⚠[/yellow] No team members specified (--members). "
            "Issues will be created without assignees."
        )
    else:
        console.print(
            f"  [green]✓[/green] Team members: [bold]{', '.join(members)}[/bold] "
            f"({len(members)} members)"
        )

    skip_stage = args.skip_stage or 0
    num_sprints = max(1, args.sprints)
    start_time = time.time()

    # ══════════════════════════════════════════════════════════════════════════
    #  Stage 1 — PDF Ingestion & Module Extraction
    # ══════════════════════════════════════════════════════════════════════════

    if skip_stage >= 1:
        console.print(Rule("[dim]Stage 1 skipped — loading from modules.txt[/dim]"))
        modules = load_modules_from_file(args.modules_out)
        console.print(
            f"  [green]✓[/green] Loaded [bold]{len(modules)}[/bold] modules "
            f"from [bold]{args.modules_out}[/bold]"
        )
    else:
        try:
            modules = ingest_pdf(
                pdf_path=args.pdf,
                gemini_api_key=config["gemini_api_key"],
                gemini_model_name=config["gemini_model"],
                modules_output_path=args.modules_out,
            )
        except FileNotFoundError as exc:
            fatal(f"PDF file not found: {exc}")
        except ValueError as exc:
            fatal(f"PDF validation error: {exc}")
        except Exception as exc:
            fatal("PDF ingestion failed unexpectedly.", exc=exc)

    # ══════════════════════════════════════════════════════════════════════════
    #  Stage 2 — Agile Decomposition via Gemini
    # ══════════════════════════════════════════════════════════════════════════

    if skip_stage >= 2:
        console.print(Rule("[dim]Stage 2 skipped — loading from backlog.json[/dim]"))
        backlog = load_backlog_from_file(args.backlog_out)
        console.print(
            f"  [green]✓[/green] Loaded [bold]{len(backlog)}[/bold] epics "
            f"from [bold]{args.backlog_out}[/bold]"
        )
    else:
        try:
            backlog = decompose_to_agile_backlog(
                modules=modules,
                members=members,
                gemini_api_key=config["gemini_api_key"],
                gemini_model_name=config["gemini_model"],
                backlog_output_path=args.backlog_out,
                num_sprints=num_sprints,
            )
        except ValueError as exc:
            fatal(f"Agile decomposition failed (JSON parse error): {exc}")
        except Exception as exc:
            fatal("Agile decomposition failed unexpectedly.", exc=exc)

    # ══════════════════════════════════════════════════════════════════════════
    #  Stage 3 — Build Jira Board
    # ══════════════════════════════════════════════════════════════════════════

    if skip_stage == 3:
        # Treat skip-stage 3 as an implicit dry run
        args.dry_run = True

    try:
        created_issues = build_jira_board(
            backlog=backlog,
            jira_domain=config["jira_domain"],
            jira_email=config["jira_email"],
            jira_api_token=config["jira_api_token"],
            jira_project_key=config["jira_project_key"],
            jira_project_name=config["jira_project_name"],
            members=members,
            dry_run=args.dry_run,
            num_sprints=num_sprints,
        )
    except RuntimeError as exc:
        fatal(f"Jira board creation failed: {exc}")
    except Exception as exc:
        fatal("Jira board creation failed unexpectedly.", exc=exc)

    # ══════════════════════════════════════════════════════════════════════════
    #  Summary
    # ══════════════════════════════════════════════════════════════════════════

    elapsed = time.time() - start_time
    console.print(Rule("[bold bright_blue]Run Complete[/bold bright_blue]"))

    if not args.dry_run and created_issues:
        print_summary_table(created_issues)

        # Counts by type
        epics  = sum(1 for r in created_issues if r["type"] == "Epic")
        stories = sum(1 for r in created_issues if r["type"] == "Story")
        tasks  = sum(1 for r in created_issues if r["type"] == "Sub-task")

        jira_url = f"https://{config['jira_domain']}/jira/software/projects/{config['jira_project_key']}/boards"

        console.print(
            Panel(
                f"[bold green]✅ Jira board populated successfully![/bold green]\n\n"
                f"  • [cyan]{epics}[/cyan] Epics created\n"
                f"  • [cyan]{stories}[/cyan] Stories created\n"
                f"  • [cyan]{tasks}[/cyan] Sub-tasks created\n\n"
                f"  🔗 [underline]{jira_url}[/underline]\n\n"
                f"  ⏱  Completed in [bold]{elapsed:.1f}s[/bold]",
                title="🚀 Mission Accomplished",
                border_style="green",
                expand=False,
            )
        )
    elif args.dry_run:
        console.print(
            Panel(
                "[bold yellow]Dry run complete — no Jira issues were created.[/bold yellow]\n"
                "Review the preview above, then re-run without [bold]--dry-run[/bold] "
                "to push to Jira.\n\n"
                f"  ⏱  Completed in [bold]{elapsed:.1f}s[/bold]",
                title="📋 Dry Run Summary",
                border_style="yellow",
                expand=False,
            )
        )

    logger.info("Agent completed in %.1f seconds.", elapsed)


# ══════════════════════════════════════════════════════════════════════════════
#  Entry Point
# ══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    main()
