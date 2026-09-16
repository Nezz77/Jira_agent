"""
utils.py
────────
Shared utilities for the Jira Agile Agent:
  - Logging setup (rich console + rotating file handler)
  - Exponential-backoff retry decorator (via tenacity)
  - Robust JSON extraction from Gemini responses
  - Pretty console output helpers (via rich)
"""

from __future__ import annotations

import json
import logging
import re
import sys
from logging.handlers import RotatingFileHandler
from typing import Any, Optional

from rich.console import Console
from rich.logging import RichHandler
from rich.panel import Panel
from rich.table import Table
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
    before_sleep_log,
)

# ── Module-level rich console (shared across the project) ────────────────────
console = Console()


# ── Logging Setup ────────────────────────────────────────────────────────────

def setup_logging(log_file: str = "agent.log", level: int = logging.INFO) -> logging.Logger:
    """
    Configure the root logger with:
      - A rich, colour-coded console handler.
      - A rotating file handler that caps at 5 MB per file (keeps last 3).

    Returns the root logger so callers can use it directly.
    """
    # Rich console handler — beautiful coloured output in the terminal
    rich_handler = RichHandler(
        console=console,
        rich_tracebacks=True,
        markup=True,
        show_time=True,
        show_path=False,
    )
    rich_handler.setLevel(level)

    # File handler — plain text, rotation so logs never bloat
    file_handler = RotatingFileHandler(
        log_file,
        maxBytes=5 * 1024 * 1024,  # 5 MB
        backupCount=3,
        encoding="utf-8",
    )
    file_handler.setLevel(logging.DEBUG)  # capture debug detail to file
    file_handler.setFormatter(
        logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    )

    logging.basicConfig(
        level=logging.DEBUG,
        handlers=[rich_handler, file_handler],
    )

    return logging.getLogger("jira_agent")


# ── Retry Decorator ──────────────────────────────────────────────────────────

def make_retry_decorator(
    max_attempts: int = 4,
    min_wait: float = 2.0,
    max_wait: float = 60.0,
    exceptions: tuple = (Exception,),
):
    """
    Factory that returns a tenacity @retry decorator with exponential backoff.

    Usage:
        @make_retry_decorator(max_attempts=3, exceptions=(ResourceExhausted,))
        def my_api_call():
            ...

    Args:
        max_attempts: Total number of attempts before giving up.
        min_wait:     Minimum seconds to wait between retries.
        max_wait:     Maximum seconds to wait between retries (caps backoff).
        exceptions:   Tuple of exception types that should trigger a retry.
    """
    logger = logging.getLogger("jira_agent.retry")
    return retry(
        reraise=True,
        stop=stop_after_attempt(max_attempts),
        wait=wait_exponential(multiplier=1, min=min_wait, max=max_wait),
        retry=retry_if_exception_type(exceptions),
        before_sleep=before_sleep_log(logger, logging.WARNING),
    )


# ── JSON Extraction ──────────────────────────────────────────────────────────

def extract_json(raw_text: str) -> Any:
    """
    Robustly extract and parse a JSON value from a Gemini response string.

    Gemini sometimes wraps JSON in markdown code fences (```json ... ```) or
    adds explanatory text before/after the JSON.  This function handles all of
    those cases by:
      1. Stripping leading/trailing whitespace.
      2. Removing markdown fences (```json, ```, ~~~).
      3. Finding the first '{' or '[' and last '}' or ']' as boundaries.
      4. Attempting json.loads() on the extracted substring.

    Raises:
        ValueError: If no valid JSON can be found in the text.
    """
    if not raw_text or not raw_text.strip():
        raise ValueError("Gemini returned an empty response.")

    text = raw_text.strip()

    # Step 1 — strip markdown code fences
    text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.MULTILINE)
    text = re.sub(r"```\s*$", "", text, flags=re.MULTILINE)
    text = re.sub(r"^~~~(?:json)?\s*", "", text, flags=re.MULTILINE)
    text = re.sub(r"~~~\s*$", "", text, flags=re.MULTILINE)
    text = text.strip()

    # Step 2 — try a direct parse first (fastest path)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    # Step 3 — find the outermost JSON array or object
    # We look for the first '[' or '{' and match the corresponding closing bracket.
    start_array = text.find("[")
    start_obj = text.find("{")

    # Pick whichever opening bracket appears first
    if start_array == -1 and start_obj == -1:
        raise ValueError(
            "No JSON array or object found in the Gemini response.\n"
            f"Raw text (first 500 chars):\n{raw_text[:500]}"
        )

    if start_array == -1:
        start = start_obj
        open_char, close_char = "{", "}"
    elif start_obj == -1:
        start = start_array
        open_char, close_char = "[", "]"
    else:
        # Both found — use whichever comes first
        if start_array < start_obj:
            start = start_array
            open_char, close_char = "[", "]"
        else:
            start = start_obj
            open_char, close_char = "{", "}"

    # Walk forward to find the matching closing bracket
    depth = 0
    end = -1
    for i, ch in enumerate(text[start:], start=start):
        if ch == open_char:
            depth += 1
        elif ch == close_char:
            depth -= 1
            if depth == 0:
                end = i + 1
                break

    if end == -1:
        raise ValueError(
            "Unbalanced brackets in Gemini JSON response.\n"
            f"Raw text (first 500 chars):\n{raw_text[:500]}"
        )

    json_str = text[start:end]

    try:
        return json.loads(json_str)
    except json.JSONDecodeError as exc:
        raise ValueError(
            f"Extracted text is not valid JSON: {exc}\n"
            f"Extracted substring:\n{json_str[:500]}"
        ) from exc


# ── Pretty Console Helpers ───────────────────────────────────────────────────

def print_banner(title: str, subtitle: str = "") -> None:
    """Print a rich banner panel at the start of a major stage."""
    content = f"[bold cyan]{title}[/bold cyan]"
    if subtitle:
        content += f"\n[dim]{subtitle}[/dim]"
    console.print(Panel(content, expand=False, border_style="bright_blue"))


def print_summary_table(rows: list[dict], title: str = "Jira Issues Created") -> None:
    """
    Print a formatted summary table of created Jira issues.

    Args:
        rows:  List of dicts, each with keys: type, key, summary, assignee.
        title: Table title string.
    """
    table = Table(title=title, show_header=True, header_style="bold magenta")
    table.add_column("Type", style="cyan", width=10)
    table.add_column("Jira Key", style="green", width=14)
    table.add_column("Summary", style="white", max_width=60)
    table.add_column("Assignee", style="yellow", width=20)

    for row in rows:
        table.add_row(
            row.get("type", ""),
            row.get("key", ""),
            row.get("summary", ""),
            row.get("assignee", "Unassigned"),
        )

    console.print(table)


def fatal(message: str, exc: Exception | None = None) -> None:
    """
    Print a fatal error message and exit with code 1.
    Optionally logs the exception traceback.
    """
    logger = logging.getLogger("jira_agent")
    if exc:
        logger.exception(message)
    else:
        logger.error(message)
    console.print(f"\n[bold red]✗ FATAL:[/bold red] {message}")
    sys.exit(1)
