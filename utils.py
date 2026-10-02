"""
utils.py
────────
Shared utilities for the Jira Agile Agent:
  - Logging setup (rich console + rotating file handler)
  - Exponential-backoff retry decorator (via tenacity)
  - Robust JSON extraction from Gemini responses
  - Pretty console output helpers (via rich)
  - Model-fallback helper for Gemini 503/429 overload errors
"""

from __future__ import annotations

import json
import logging
import re
import sys
import time
from logging.handlers import RotatingFileHandler
from typing import Any, Callable, Optional, TypeVar

import openai
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

# ── Model fallback chain ─────────────────────────────────────────────────────
# When the primary model is overloaded (503/429) or deprecated (404),
# the agent tries each model in this list in order before raising a fatal error.
_FALLBACK_MODELS: list[str] = [
    "gemini-3.8-flash",
    "gemini-3.6-flash",
    "gemini-2.5-flash-preview-04-17",
    "gemini-3.5-flash-lite",
    "deepseek-chat",
]

# Hardcoded backup API keys (lowest priority — prefer GEMINI_API_KEYS_EXTRA in .env).
_FALLBACK_API_KEYS: list[str] = []

# Seconds to wait before retrying the *same* model/key on a transient overload.
_OVERLOAD_WAIT_SECONDS = 30
_OVERLOAD_MAX_RETRIES = 2   # retries per model/key before switching


def is_overload_error(exc: BaseException) -> bool:
    """
    Return True if the exception represents a transient service overload
    (HTTP 503 UNAVAILABLE or 429 RESOURCE_EXHAUSTED / RATE_LIMIT_EXCEEDED).
    This signals: wait and retry or switch key, but the model itself is valid.
    """
    msg = str(exc).upper()
    codes = ("503", "429", "UNAVAILABLE", "RESOURCE_EXHAUSTED",
             "RATE_LIMIT", "QUOTA", "HIGH DEMAND")
    return any(c in msg for c in codes)


def is_skip_model_error(exc: BaseException) -> bool:
    """
    Return True if the exception means this model should be skipped entirely
    (e.g. 404 NOT_FOUND because the model is deprecated or unavailable).
    In this case we move straight to the next model without waiting.
    """
    msg = str(exc).upper()
    return "404" in msg or "NOT_FOUND" in msg or "NO LONGER AVAILABLE" in msg


F = TypeVar("F")


def call_with_model_fallback(
    fn: Callable[..., F],
    primary_model: str,
    *args: Any,
    model_arg_index: int = 1,
    extra_api_keys: list[str] | None = None,
    deepseek_api_key: str | None = None,
    **kwargs: Any,
) -> F:
    """
    Call *fn* with *primary_model*. Handles three error classes:

    - **503 / 429 overload**: wait ``_OVERLOAD_WAIT_SECONDS``, retry same
      model+key up to ``_OVERLOAD_MAX_RETRIES`` times, then rotate to next key
      for the same model, then next model.
    - **404 deprecated model**: skip immediately to next model (no wait).
    - **other errors**: re-raise immediately (not a quota/availability issue).

    Rotation order (per model, try every key first)::

      gemini-3.6-flash + key1 → gemini-3.6-flash + key2
        → gemini-3.8-flash + key1 → gemini-3.8-flash + key2 → …
    """
    _log = logging.getLogger("jira_agent.fallback")

    models_to_try = [primary_model] + [
        m for m in _FALLBACK_MODELS if m != primary_model
    ]

    # Build client list: primary first, then extras from .env, then hardcoded.
    primary_client = args[0]
    all_keys: list[str] = []
    seen: set[str] = set()
    for key in (extra_api_keys or []) + _FALLBACK_API_KEYS:
        if key and key not in seen:
            all_keys.append(key)
            seen.add(key)

    extra_clients = []
    for key in all_keys:
        try:
            extra_clients.append(type(primary_client)(api_key=key))
        except Exception:
            pass

    clients_to_try = [primary_client] + extra_clients

    last_exc: BaseException | None = None

    deepseek_client = None
    if deepseek_api_key:
        try:
            deepseek_client = openai.OpenAI(api_key=deepseek_api_key, base_url="https://api.deepseek.com/v1")
        except Exception:
            pass

    for model in models_to_try:
        model_skipped = False
        
        # Determine which clients to try for this model
        if model.startswith("deepseek"):
            if not deepseek_client:
                continue # Skip deepseek if no key
            current_clients = [deepseek_client]
        else:
            current_clients = clients_to_try
            
        for client_idx, client in enumerate(current_clients):
            if model_skipped:
                break  # 404 on this model — don't try other keys, skip model

            args_list = list(args)
            args_list[0] = client
            if model_arg_index < len(args_list):
                args_list[model_arg_index] = model
            else:
                args_list.append(model)
            current_args = tuple(args_list)
            key_label = f"key{client_idx + 1}"

            for attempt in range(1, _OVERLOAD_MAX_RETRIES + 1):
                try:
                    return fn(*current_args, **kwargs)
                except BaseException as exc:
                    last_exc = exc

                    if is_skip_model_error(exc):
                        # Model deprecated/unavailable — skip to next model
                        _log.warning(
                            "Model '%s' unavailable (404). Skipping to next model.", model
                        )
                        console.print(
                            f"  [red]✗[/red]  Model [bold]{model}[/bold] is deprecated/unavailable. "
                            "Skipping to next model…"
                        )
                        model_skipped = True
                        break  # break attempt loop; outer loop will break key loop

                    if not is_overload_error(exc):
                        # Unknown error — re-raise immediately
                        raise

                    if attempt < _OVERLOAD_MAX_RETRIES:
                        wait = _OVERLOAD_WAIT_SECONDS * attempt
                        _log.warning(
                            "[%s/%s] overloaded (attempt %d/%d). Waiting %ds…",
                            model, key_label, attempt, _OVERLOAD_MAX_RETRIES, wait,
                        )
                        console.print(
                            f"  [yellow]⚠[/yellow]  [bold]{model}[/bold] ({key_label}) "
                            f"high demand. Retrying in [bold]{wait}s[/bold]…"
                        )
                        time.sleep(wait)
                    else:
                        _log.warning(
                            "[%s/%s] quota exhausted after %d retries. Trying next key/model…",
                            model, key_label, _OVERLOAD_MAX_RETRIES,
                        )
                        console.print(
                            f"  [yellow]⚠[/yellow]  [bold]{model}[/bold] ({key_label}) quota exhausted. "
                            "Trying next key/model…"
                        )

    # All models + keys failed
    assert last_exc is not None
    raise last_exc


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

    Overload/rate-limit errors (503, 429) are handled separately by
    `call_with_model_fallback`; this decorator covers other transient failures
    (network timeouts, 5xx errors that are *not* overloads, etc.).

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
    _log = logging.getLogger("jira_agent.retry")
    return retry(
        reraise=True,
        stop=stop_after_attempt(max_attempts),
        wait=wait_exponential(multiplier=1, min=min_wait, max=max_wait),
        retry=retry_if_exception_type(exceptions),
        before_sleep=before_sleep_log(_log, logging.WARNING),
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
