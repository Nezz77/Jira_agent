"""
jira_builder.py
───────────────
Stage 3 of the Jira Agile Agent pipeline:

  Takes the validated Agile backlog (list of Epics with Stories and Tasks)
  and creates the corresponding Jira issues in the correct dependency order:

    1. Ensure the Jira project exists (create it if not).
    2. Create each Epic.
    3. Create each Story, linked to its parent Epic.
    4. Create each Sub-task, linked to its parent Story.
    5. Optionally resolve Jira account IDs by member email/display name.
    6. Return a flat list of all created issue rows for the summary table.

  Authentication uses Basic Auth (email + API token) via the `jira` library,
  with all credentials loaded from environment variables.
"""

from __future__ import annotations

import logging
import time
from typing import Optional

import requests
from jira import JIRA, JIRAError

from utils import console, make_retry_decorator, print_banner, print_summary_table

logger = logging.getLogger("jira_agent.jira_builder")

# ── Retry decorator for Jira API calls ───────────────────────────────────────
# Jira Cloud has rate limits (~300 req/min). We retry with backoff on any error.
_jira_retry = make_retry_decorator(
    max_attempts=3,
    min_wait=3.0,
    max_wait=30.0,
    exceptions=(JIRAError, Exception),
)


# ── Jira Client Factory ───────────────────────────────────────────────────────

def create_jira_client(
    domain: str,
    email: str,
    api_token: str,
) -> JIRA:
    """
    Authenticate with Jira Cloud using Basic Auth (email + API token).

    Args:
        domain:    Atlassian domain, e.g. "mycompany.atlassian.net".
        email:     Atlassian account email.
        api_token: Jira API token (not your password).

    Returns:
        An authenticated JIRA client instance.

    Raises:
        JIRAError: If authentication fails.
        ValueError: If the domain is malformed.
    """
    if not domain.startswith("http"):
        server = f"https://{domain}"
    else:
        server = domain

    logger.info("Connecting to Jira: %s", server)

    jira = JIRA(
        server=server,
        basic_auth=(email, api_token),
        options={"verify": True},  # enforce SSL — never skip in production
    )

    # Verify connectivity by fetching the current user
    me = jira.myself()
    logger.info(
        "Jira authenticated as: %s (%s)", me.get("displayName"), me.get("emailAddress")
    )
    console.print(
        f"  [green]✓[/green] Jira authenticated as "
        f"[bold]{me.get('displayName')}[/bold] ({me.get('emailAddress')})"
    )

    return jira


# ── Project Management ────────────────────────────────────────────────────────

def ensure_project_exists(
    jira: JIRA,
    project_key: str,
    project_name: str,
) -> None:
    """
    Check if the Jira project exists; create a Scrum project if it doesn't.

    Args:
        jira:         Authenticated JIRA client.
        project_key:  Short project key, e.g. "PROJ".
        project_name: Human-readable project name.
    """
    try:
        project = jira.project(project_key)
        logger.info("Project '%s' already exists: %s", project_key, project.name)
        console.print(
            f"  [green]✓[/green] Jira project [bold]{project_key}[/bold] already exists."
        )
    except JIRAError as exc:
        if exc.status_code == 404:
            logger.info("Project '%s' not found — creating…", project_key)
            console.print(
                f"  [yellow]⚠[/yellow] Project [bold]{project_key}[/bold] not found. "
                "Creating a new Scrum project…"
            )
            try:
                jira.create_project(
                    key=project_key,
                    name=project_name,
                    ptype="software",          # software project type
                    template_name="Scrum",     # Scrum board template
                )
                console.print(
                    f"  [green]✓[/green] Project [bold]{project_key}[/bold] created."
                )
            except JIRAError as create_exc:
                # Some Jira instances don't allow programmatic project creation
                # (permission restriction). Surface a clear error message.
                raise RuntimeError(
                    f"Could not create Jira project '{project_key}': {create_exc.text}\n"
                    "Ensure your API token has 'Administer Jira' permission, or create "
                    "the project manually and re-run."
                ) from create_exc
        else:
            raise


# ── Account ID Resolution ─────────────────────────────────────────────────────

def resolve_account_ids(
    jira: JIRA,
    members: list[str],
) -> dict[str, Optional[str]]:
    """
    Attempt to resolve team member names/emails to Jira account IDs.

    Jira Cloud requires account IDs (not display names) for issue assignment.
    We do a best-effort fuzzy search:
      - If the member string looks like an email, search by email.
      - Otherwise, search by display name.
      - If no match is found, we leave the assignee as None (unassigned).

    Args:
        jira:    Authenticated JIRA client.
        members: List of member names or emails.

    Returns:
        Dict mapping member name → Jira account ID (or None if not found).
    """
    account_map: dict[str, Optional[str]] = {}

    for member in members:
        try:
            # Jira Cloud user search supports both email and display name
            users = jira.search_users(query=member, maxResults=5)

            if not users:
                logger.warning("No Jira user found for: %s — will assign as Unassigned", member)
                account_map[member] = None
                continue

            # Pick the first result (most likely match)
            best = users[0]
            account_map[member] = best.accountId
            logger.info(
                "Resolved '%s' → account ID: %s (%s)",
                member, best.accountId, best.displayName,
            )

        except JIRAError as exc:
            logger.warning(
                "Could not search for user '%s' (JIRAError %s): %s",
                member, exc.status_code, exc.text,
            )
            account_map[member] = None

    return account_map


# ── Issue Creators ────────────────────────────────────────────────────────────

@_jira_retry
def _create_epic(
    jira: JIRA,
    project_key: str,
    name: str,
    description: str,
) -> str:
    """
    Create a Jira Epic and return its issue key (e.g. "PROJ-1").

    Epic name field detection:
      - Jira Cloud uses the "Epic Name" custom field (customfield_10011).
      - We also try "Epic Link" label as fallback.
    """
    fields: dict = {
        "project": {"key": project_key},
        "summary": name,
        "description": description,
        "issuetype": {"name": "Epic"},
        # Note: customfield_10011 (Epic Name) is omitted — it is only present on
        # classic/company-managed projects and raises HTTP 400 on next-gen projects.
        # The `summary` field already serves as the Epic name.
    }

    issue = jira.create_issue(fields=fields)
    logger.info("Created Epic: %s — %s", issue.key, name)
    return issue.key


@_jira_retry
def _create_story(
    jira: JIRA,
    project_key: str,
    title: str,
    description: str,
    story_points: int,
    epic_key: str,
    account_id: Optional[str],
) -> str:
    """
    Create a Jira Story linked to its parent Epic.

    Returns the new Story's issue key.

    Compatibility notes:
    - Next-gen (team-managed) projects use `parent` to link stories to epics.
      Classic projects use `customfield_10014` (Epic Link).
    - Story points use `customfield_10016`; if not on the screen we retry without it.
    - `story_points` is NOT a valid Jira field name and must never be sent.
    """
    fields: dict = {
        "project": {"key": project_key},
        "summary": title,
        "description": description,
        "issuetype": {"name": "Story"},
        # Next-gen: link story to epic via `parent`
        "parent": {"key": epic_key},
        # Story points (customfield_10016) — present on most but not all screens
        "customfield_10016": story_points,
    }

    if account_id:
        fields["assignee"] = {"accountId": account_id}

    try:
        issue = jira.create_issue(fields=fields)
    except JIRAError as exc:
        # If story points or parent field is rejected, retry without optional fields
        if exc.status_code == 400 and (
            "customfield_10016" in (exc.text or "")
            or "customfield_10014" in (exc.text or "")
            or "parent" in (exc.text or "")
        ):
            logger.warning(
                "Story creation failed with optional fields (%s) — retrying without them.",
                exc.text,
            )
            fields.pop("customfield_10016", None)
            fields.pop("parent", None)
            issue = jira.create_issue(fields=fields)
        else:
            raise

    logger.info(
        "Created Story: %s — %s (Epic: %s, Assignee ID: %s)",
        issue.key, title[:60], epic_key, account_id,
    )
    return issue.key


@_jira_retry
def _create_subtask(
    jira: JIRA,
    project_key: str,
    title: str,
    description: str,
    parent_story_key: str,
    account_id: Optional[str],
) -> str:
    """
    Create a Jira Sub-task linked to its parent Story.

    Returns the new Sub-task's issue key.
    """
    fields: dict = {
        "project": {"key": project_key},
        "summary": title,
        "description": description,
        "issuetype": {"name": "Subtask"},
        "parent": {"key": parent_story_key},
    }

    if account_id:
        fields["assignee"] = {"accountId": account_id}

    issue = jira.create_issue(fields=fields)
    logger.info(
        "Created Sub-task: %s — %s (Parent: %s)", issue.key, title[:60], parent_story_key
    )
    return issue.key


# ── Rate-limit-friendly sleep ─────────────────────────────────────────────────

_RATE_LIMIT_SLEEP = 0.35  # seconds between Jira API write calls (~170 calls/min)


# ── Sprint Management ─────────────────────────────────────────────────────────

def create_sprints(
    jira: JIRA,
    jira_domain: str,
    jira_email: str,
    jira_api_token: str,
    project_key: str,
    num_sprints: int,
) -> dict[int, int]:
    """
    Create `num_sprints` sprints on the project's Scrum board via the Agile REST API.

    Returns:
        Dict mapping sprint_number (1-based) -> Jira sprint ID.
        Returns an empty dict if no board is found or sprint creation fails.
    """
    if num_sprints <= 0:
        return {}

    base_url = f"https://{jira_domain}" if not jira_domain.startswith("http") else jira_domain
    auth = (jira_email, jira_api_token)
    headers = {"Content-Type": "application/json", "Accept": "application/json"}

    # 1. Find the board ID for this project
    boards_url = f"{base_url}/rest/agile/1.0/board"
    try:
        resp = requests.get(
            boards_url,
            params={"projectKeyOrId": project_key, "type": "scrum"},
            auth=auth,
            headers=headers,
            timeout=15,
        )
        resp.raise_for_status()
        boards = resp.json().get("values", [])
    except Exception as exc:
        logger.warning("Could not fetch boards for project %s: %s", project_key, exc)
        return {}

    if not boards:
        logger.warning(
            "No Scrum board found for project '%s' — sprints will not be created. "
            "Ensure the project has a Scrum board.",
            project_key,
        )
        return {}

    board_id = boards[0]["id"]
    logger.info("Found board ID %s for project %s", board_id, project_key)

    # 2. Create each sprint
    sprint_map: dict[int, int] = {}
    sprints_url = f"{base_url}/rest/agile/1.0/sprint"

    for sprint_num in range(1, num_sprints + 1):
        try:
            resp = requests.post(
                sprints_url,
                json={"name": f"Sprint {sprint_num}", "originBoardId": board_id},
                auth=auth,
                headers=headers,
                timeout=15,
            )
            resp.raise_for_status()
            sprint_id = resp.json()["id"]
            sprint_map[sprint_num] = sprint_id
            logger.info("Created Sprint %d (ID: %s)", sprint_num, sprint_id)
            console.print(
                f"  [green]✓[/green] Sprint [bold]{sprint_num}[/bold] created (ID: {sprint_id})"
            )
            time.sleep(_RATE_LIMIT_SLEEP)
        except Exception as exc:
            logger.warning("Failed to create Sprint %d: %s", sprint_num, exc)

    return sprint_map


def _move_issue_to_sprint(
    jira_domain: str,
    jira_email: str,
    jira_api_token: str,
    sprint_id: int,
    issue_key: str,
) -> None:
    """
    Move a Jira issue into a sprint using the Agile REST API.
    Silently logs a warning on failure (sprint assignment is non-critical).
    """
    base_url = f"https://{jira_domain}" if not jira_domain.startswith("http") else jira_domain
    url = f"{base_url}/rest/agile/1.0/sprint/{sprint_id}/issue"
    try:
        resp = requests.post(
            url,
            json={"issues": [issue_key]},
            auth=(jira_email, jira_api_token),
            headers={"Content-Type": "application/json"},
            timeout=15,
        )
        resp.raise_for_status()
        logger.debug("Moved %s to sprint %s", issue_key, sprint_id)
    except Exception as exc:
        logger.warning("Could not move %s to sprint %s: %s", issue_key, sprint_id, exc)


# ── Main Builder ──────────────────────────────────────────────────────────────

def build_jira_board(
    backlog: list[dict],
    jira_domain: str,
    jira_email: str,
    jira_api_token: str,
    jira_project_key: str,
    jira_project_name: str,
    members: list[str],
    dry_run: bool = False,
    num_sprints: int = 1,
) -> list[dict]:
    """
    Full Stage 3 pipeline: authenticate → ensure project → create sprints → create issues.

    Args:
        backlog:           Validated backlog from Stage 2.
        jira_domain:       Atlassian domain (e.g. mycompany.atlassian.net).
        jira_email:        Atlassian account email.
        jira_api_token:    Jira API token.
        jira_project_key:  Jira project key (e.g. PROJ).
        jira_project_name: Human-readable project name.
        members:           Team member names (used for account ID resolution).
        dry_run:           If True, print what would be created without calling Jira.
        num_sprints:       Number of sprints to create on the board.

    Returns:
        List of row dicts for the summary table (type, key, summary, assignee, sprint).
    """
    print_banner(
        "Stage 3 — Building Jira Board",
        f"Project: {jira_project_key} | Dry run: {dry_run}",
    )

    created_issues: list[dict] = []

    if dry_run:
        console.print(
            "  [bold yellow]⚠ DRY RUN MODE — no Jira issues will be created.[/bold yellow]\n"
        )
        _dry_run_preview(backlog)
        return []

    # ── Authenticate ──────────────────────────────────────────────────────────
    with console.status("[bold green]Authenticating with Jira…"):
        jira = create_jira_client(jira_domain, jira_email, jira_api_token)

    # ── Ensure project exists ─────────────────────────────────────────────────
    with console.status("[bold green]Checking Jira project…"):
        ensure_project_exists(jira, jira_project_key, jira_project_name)

    # ── Create sprints ────────────────────────────────────────────────────────
    sprint_map: dict[int, int] = {}
    if num_sprints >= 1:
        console.print(f"\n  Creating [bold]{num_sprints}[/bold] sprint(s) on the board…")
        sprint_map = create_sprints(
            jira, jira_domain, jira_email, jira_api_token, jira_project_key, num_sprints
        )

    # ── Resolve account IDs ───────────────────────────────────────────────────
    account_map: dict[str, Optional[str]] = {}
    if members:
        with console.status("[bold green]Resolving team member account IDs…"):
            account_map = resolve_account_ids(jira, members)
        console.print(
            f"  [green]✓[/green] Resolved account IDs for "
            f"{sum(1 for v in account_map.values() if v)} / {len(members)} members."
        )

    # ── Create issues ─────────────────────────────────────────────────────────
    total_epics = len(backlog)
    for epic_idx, item in enumerate(backlog, start=1):
        epic_data = item["epic"]
        epic_name = epic_data["name"]
        epic_desc = epic_data["description"]
        stories = item.get("stories", [])

        # Create the Epic
        console.print(
            f"\n  [bold cyan]Epic {epic_idx}/{total_epics}:[/bold cyan] {epic_name}"
        )

        with console.status(f"Creating Epic: {epic_name[:50]}…"):
            epic_key = _create_epic(
                jira, jira_project_key, epic_name, epic_desc
            )

        time.sleep(_RATE_LIMIT_SLEEP)

        created_issues.append({
            "type": "Epic",
            "key": epic_key,
            "summary": epic_name,
            "assignee": "",
        })

        # Create Stories under this Epic
        for story in stories:
            story_title = story["title"]
            story_desc = story["description"]
            story_points = story.get("story_points", 3)
            story_assignee_name = story.get("assignee", "")
            story_account_id = account_map.get(story_assignee_name)

            with console.status(f"  Creating Story: {story_title[:60]}…"):
                story_key = _create_story(
                    jira,
                    jira_project_key,
                    story_title,
                    story_desc,
                    story_points,
                    epic_key,
                    story_account_id,
                )

            time.sleep(_RATE_LIMIT_SLEEP)

            # ── Assign story to its sprint ──────────────────────────────────────
            story_sprint_num = story.get("sprint", 1)
            sprint_id = sprint_map.get(story_sprint_num)
            if sprint_id:
                _move_issue_to_sprint(jira_domain, jira_email, jira_api_token, sprint_id, story_key)
                time.sleep(_RATE_LIMIT_SLEEP)

            created_issues.append({
                "type": "Story",
                "key": story_key,
                "summary": story_title[:60],
                "assignee": story_assignee_name,
                "sprint": story_sprint_num,
            })

            console.print(
                f"    [green]↳[/green] Story [bold]{story_key}[/bold] "
                f"({story_points} pts, Sprint {story_sprint_num}) → {story_assignee_name}"
            )

            # Create Sub-tasks under this Story
            for task in story.get("tasks", []):
                task_title = task["title"]
                task_desc = task["description"]
                task_assignee_name = task.get("assignee", "")
                task_account_id = account_map.get(task_assignee_name)

                with console.status(f"    Creating Sub-task: {task_title[:60]}…"):
                    task_key = _create_subtask(
                        jira,
                        jira_project_key,
                        task_title,
                        task_desc,
                        story_key,
                        task_account_id,
                    )

                time.sleep(_RATE_LIMIT_SLEEP)

                created_issues.append({
                    "type": "Sub-task",
                    "key": task_key,
                    "summary": task_title[:60],
                    "assignee": task_assignee_name,
                })

                console.print(
                    f"      [dim]• Sub-task [bold]{task_key}[/bold] → {task_assignee_name}[/dim]"
                )

    return created_issues


def _dry_run_preview(backlog: list[dict]) -> None:
    """Print a structured dry-run preview of all issues that would be created."""
    for item in backlog:
        epic = item["epic"]
        console.print(f"\n  [bold cyan]🔷 EPIC:[/bold cyan] {epic['name']}")
        console.print(f"     [dim]{epic['description'][:100]}[/dim]")

        for story in item.get("stories", []):
            console.print(
                f"\n    [bold green]📗 STORY:[/bold green] {story['title'][:70]}"
            )
            console.print(
                f"       Points: [yellow]{story['story_points']}[/yellow] | "
                f"Assignee: [magenta]{story['assignee']}[/magenta]"
            )

            for task in story.get("tasks", []):
                console.print(
                    f"         [dim]• Task: {task['title'][:70]} "
                    f"→ [magenta]{task['assignee']}[/magenta][/dim]"
                )
