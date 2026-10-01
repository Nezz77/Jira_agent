"""
ai_decomposer.py
────────────────
Stage 2 of the Jira Agile Agent pipeline:

  Given the list of modules from Stage 1, make a second Gemini call to produce
  a fully structured Agile backlog in JSON format:

      [
        {
          "epic": { "name": "...", "description": "..." },
          "stories": [
            {
              "title": "As a ... I want ... so that ...",
              "description": "...",
              "story_points": 5,
              "assignee": "Alice",
              "tasks": [
                { "title": "...", "description": "...", "assignee": "Bob" }
              ]
            }
          ]
        }
      ]

  Work is distributed evenly across team members in a round-robin fashion.

Uses the new `google.genai` SDK (replaces deprecated `google.generativeai`).
"""

from __future__ import annotations

import json
import logging
from itertools import cycle
from pathlib import Path
from typing import Any

from google import genai
from google.genai import types

from prompts import AGILE_DECOMPOSITION_PROMPT
from utils import (
    call_with_model_fallback,
    console,
    extract_json,
    is_overload_error,
    make_retry_decorator,
    print_banner,
)

logger = logging.getLogger("jira_agent.decomposer")

# ── Gemini retry for non-overload transient errors ────────────────────────────
# 503/429 overload errors are handled by call_with_model_fallback in
# decompose_to_agile_backlog; this decorator handles other transient failures.
_gemini_retry = make_retry_decorator(
    max_attempts=3,
    min_wait=5.0,
    max_wait=60.0,
    exceptions=(Exception,),
)

# ── Allowed Fibonacci story points ───────────────────────────────────────────
VALID_STORY_POINTS = {1, 2, 3, 5, 8, 13}


# ── Schema Validation ────────────────────────────────────────────────────────

def _validate_backlog_schema(backlog: Any) -> list[dict]:
    """
    Validate that the Gemini JSON output conforms to the expected backlog schema.

    Raises:
        ValueError: On any schema violation.

    Returns:
        The validated backlog list.
    """
    if not isinstance(backlog, list):
        raise ValueError(
            f"Top-level backlog must be a JSON array, got: {type(backlog).__name__}"
        )

    if len(backlog) == 0:
        raise ValueError("Backlog array is empty — Gemini returned no epics.")

    for epic_idx, item in enumerate(backlog):
        prefix = f"Backlog item [{epic_idx}]"

        # ── Epic ──────────────────────────────────────────────────────────
        if "epic" not in item or not isinstance(item["epic"], dict):
            raise ValueError(f"{prefix}: missing or invalid 'epic' dict.")

        epic = item["epic"]
        if "name" not in epic or not isinstance(epic["name"], str):
            raise ValueError(f"{prefix}.epic: missing 'name' string.")
        if "description" not in epic or not isinstance(epic["description"], str):
            raise ValueError(f"{prefix}.epic: missing 'description' string.")

        # ── Stories ───────────────────────────────────────────────────────
        if "stories" not in item or not isinstance(item["stories"], list):
            raise ValueError(f"{prefix}: missing or invalid 'stories' array.")

        for story_idx, story in enumerate(item["stories"]):
            s_prefix = f"{prefix}.stories[{story_idx}]"

            for required_key in ("title", "description", "story_points", "assignee", "tasks"):
                if required_key not in story:
                    raise ValueError(f"{s_prefix}: missing required field '{required_key}'.")

            if not isinstance(story["story_points"], (int, float)):
                raise ValueError(f"{s_prefix}: 'story_points' must be a number.")

            if not isinstance(story["tasks"], list):
                raise ValueError(f"{s_prefix}: 'tasks' must be a list.")

            # Sprint field is optional (defaults to 1 if absent)
            sprint = story.get("sprint", 1)
            if not isinstance(sprint, int):
                raise ValueError(f"{s_prefix}: 'sprint' must be an integer, got {type(sprint).__name__}.")

            # ── Tasks ─────────────────────────────────────────────────────
            for task_idx, task in enumerate(story["tasks"]):
                t_prefix = f"{s_prefix}.tasks[{task_idx}]"
                for req in ("title", "description", "assignee"):
                    if req not in task:
                        raise ValueError(f"{t_prefix}: missing required field '{req}'.")

    return backlog


def _fix_sprint_numbers(backlog: list[dict], num_sprints: int) -> list[dict]:
    """
    Clamp any sprint values that are out of range [1, num_sprints].
    Also fills in a default of 1 if the field is missing.
    Distributes stories round-robin across sprints if num_sprints > 1 and
    all sprints ended up as 1 (Gemini ignored the instruction).
    """
    if num_sprints <= 1:
        # Single sprint — just ensure the field exists
        for item in backlog:
            for story in item.get("stories", []):
                story["sprint"] = 1
        return backlog

    sprint_counter = 1
    all_same = True
    first_sprint = None

    for item in backlog:
        for story in item.get("stories", []):
            raw = story.get("sprint", 1)
            try:
                val = int(raw)
            except (TypeError, ValueError):
                val = 1
            val = max(1, min(num_sprints, val))
            story["sprint"] = val
            if first_sprint is None:
                first_sprint = val
            elif val != first_sprint:
                all_same = False

    # If Gemini assigned every story to the same sprint, override with round-robin
    if all_same and num_sprints > 1:
        logger.warning(
            "All stories assigned to sprint %s — enforcing round-robin distribution.",
            first_sprint,
        )
        for item in backlog:
            for story in item.get("stories", []):
                story["sprint"] = sprint_counter
                sprint_counter = (sprint_counter % num_sprints) + 1

    return backlog


def _fix_story_points(backlog: list[dict]) -> list[dict]:
    """
    Clamp any story_points values to the nearest valid Fibonacci number.
    Defensive post-process step in case Gemini returns non-Fibonacci values.
    """
    fibonacci = sorted(VALID_STORY_POINTS)

    def nearest_fib(value: Any) -> int:
        try:
            v = int(float(value))
        except (TypeError, ValueError):
            return 3  # fallback default
        return min(fibonacci, key=lambda f: abs(f - v))

    for item in backlog:
        for story in item.get("stories", []):
            sp = story.get("story_points", 3)
            if sp not in VALID_STORY_POINTS:
                fixed = nearest_fib(sp)
                logger.debug(
                    "Fixed story_points %s → %s for story: %s",
                    sp, fixed, story.get("title", "?")[:50],
                )
                story["story_points"] = fixed

    return backlog


def _enforce_round_robin_assignees(
    backlog: list[dict],
    members: list[str],
) -> list[dict]:
    """
    Post-process the backlog to guarantee an even, round-robin work distribution.

    Even if Gemini followed the prompt instructions, this step ensures no
    single team member is overloaded due to hallucination or rounding.
    """
    if not members:
        return backlog

    member_cycle = cycle(members)

    for item in backlog:
        for story in item.get("stories", []):
            # ── Assign story ──────────────────────────────────────────────
            story["assignee"] = next(member_cycle)

            # ── Assign tasks (start from next member after story owner) ──
            task_cycle = cycle(members)
            story_assignee_idx = members.index(story["assignee"])
            for _ in range((story_assignee_idx + 1) % len(members)):
                next(task_cycle)

            for task in story.get("tasks", []):
                task["assignee"] = next(task_cycle)

    return backlog


@_gemini_retry
def _call_gemini_for_backlog(
    client: genai.Client,
    model_name: str,
    modules: list[str],
    members: list[str],
    num_sprints: int = 1,
) -> str:
    """
    Call Gemini to produce the full Agile backlog JSON string.

    Args:
        num_sprints: Number of sprints to distribute stories across.

    Returns:
        Raw text response from Gemini.
    """
    prompt = AGILE_DECOMPOSITION_PROMPT.format(
        modules_list=json.dumps(modules, ensure_ascii=False),
        members_list=", ".join(members) if members else "Team Member A, Team Member B",
        num_sprints=num_sprints,
    )

    logger.info(
        "Calling Gemini model '%s' for Agile decomposition (%d modules, %d members, %d sprints)…",
        model_name, len(modules), len(members), num_sprints,
    )

    response = client.models.generate_content(
        model=model_name,
        contents=prompt,
        config=types.GenerateContentConfig(
            temperature=0.3,
            max_output_tokens=65536,  # raised: complex backlogs can exceed 16k tokens
            response_mime_type="application/json",
        ),
    )

    raw = response.text
    logger.debug("Raw Gemini decomposition response (first 2000 chars):\n%s", raw[:2000])
    return raw


def decompose_to_agile_backlog(
    modules: list[str],
    members: list[str],
    gemini_api_key: str,
    gemini_model_name: str = "gemini-3.6-flash",
    backlog_output_path: str = "backlog.json",
    num_sprints: int = 1,
    extra_api_keys: list[str] | None = None,
) -> list[dict]:
    """
    Full Stage 2 pipeline: call Gemini → parse JSON → validate → fix → enforce distribution.

    Args:
        modules:             List of module names from Stage 1.
        members:             List of team member names (for work distribution).
        gemini_api_key:      Google AI Studio API key.
        gemini_model_name:   Gemini model name.
        backlog_output_path: Optional path to save the raw backlog JSON.
        num_sprints:         Number of sprints to distribute stories across.

    Returns:
        Validated, distribution-corrected Agile backlog as a list of dicts.
    """
    print_banner(
        "Stage 2 — Agile Decomposition via Gemini",
        f"{len(modules)} modules → Epics / Stories / Tasks",
    )

    # ── Create Gemini client ─────────────────────────────────────────────────
    client = genai.Client(api_key=gemini_api_key)

    # ── Call Gemini (with automatic model fallback on 503/429) ───────────────────
    console.print(
        f"  [dim]Primary model: [bold]{gemini_model_name}[/bold] "
        "(auto-fallback enabled on 503/429)…[/dim]"
    )
    with console.status("[bold green]Generating Agile backlog (this may take 30–60 s)…"):
        raw_text = call_with_model_fallback(
            _call_gemini_for_backlog,
            gemini_model_name,
            client,
            gemini_model_name,
            modules,
            members,
            num_sprints,
            model_arg_index=1,
            extra_api_keys=extra_api_keys,
        )

    console.print("  [green]✓[/green] Gemini returned a response.")

    # ── Parse JSON ───────────────────────────────────────────────────────────
    with console.status("[bold green]Parsing and validating JSON…"):
        try:
            backlog = extract_json(raw_text)
        except ValueError:
            logger.error("JSON extraction failed.\nFull raw response:\n%s", raw_text)
            raise

        backlog = _validate_backlog_schema(backlog)

    console.print(
        f"  [green]✓[/green] Schema validated — [bold]{len(backlog)}[/bold] epics found."
    )

    # ── Post-processing ──────────────────────────────────────────────────────
    backlog = _fix_story_points(backlog)
    console.print("  [green]✓[/green] Story points normalised to Fibonacci scale.")

    backlog = _fix_sprint_numbers(backlog, num_sprints)
    console.print(
        f"  [green]✓[/green] Stories distributed across "
        f"[bold]{num_sprints}[/bold] sprint(s)."
    )

    if members:
        backlog = _enforce_round_robin_assignees(backlog, members)
        console.print(
            f"  [green]✓[/green] Work distributed evenly across "
            f"[bold]{len(members)}[/bold] team members."
        )

    # ── Save backlog JSON ─────────────────────────────────────────────────────
    output_path = Path(backlog_output_path).resolve()
    with output_path.open("w", encoding="utf-8") as f:
        json.dump(backlog, f, indent=2, ensure_ascii=False)

    console.print(
        f"  [green]✓[/green] Backlog JSON saved → [bold]{output_path}[/bold]\n"
    )

    # ── Print summary stats ──────────────────────────────────────────────────
    total_stories = sum(len(item.get("stories", [])) for item in backlog)
    total_tasks = sum(
        len(story.get("tasks", []))
        for item in backlog
        for story in item.get("stories", [])
    )
    total_points = sum(
        story.get("story_points", 0)
        for item in backlog
        for story in item.get("stories", [])
    )

    console.print(
        f"  📊 [bold]Backlog summary:[/bold] "
        f"{len(backlog)} Epics · {total_stories} Stories · "
        f"{total_tasks} Tasks · {total_points} story points total"
    )

    return backlog
