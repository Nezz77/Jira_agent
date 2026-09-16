"""
prompts.py
──────────
Centralised Gemini prompt templates for the Jira Agile Agent.
Keeping prompts here makes them easy to tune without touching business logic.
"""

# ── Prompt 1: Module Extraction ─────────────────────────────────────────────
# Given the full PDF document, extract a clean list of high-level modules
# (functional areas / feature groups). This is the "first pass" — we want a
# bird's-eye view of the project before drilling into Agile artefacts.

MODULE_EXTRACTION_PROMPT = """
You are an expert software architect and business analyst.

You have been given a project proposal document (PDF). Your task is to read it
carefully and identify the **distinct functional modules** or **feature groups**
that make up the project.

Rules:
- Each module must represent a self-contained area of functionality (e.g.,
  "User Authentication", "Payment Processing", "Admin Dashboard").
- Do NOT list sub-features or implementation details here — only top-level modules.
- Return ONLY a valid JSON array of strings. No markdown fences, no explanation.
- Aim for 4–12 modules. If the project is simple, fewer is fine.

Example output:
["User Authentication", "Product Catalogue", "Shopping Cart", "Payment Processing", "Order Management", "Admin Dashboard"]

Now analyse the attached document and return the module list.
""".strip()


# ── Prompt 2: Agile Decomposition ───────────────────────────────────────────
# Given the PDF + the module list, produce a fully structured Agile backlog as
# a JSON array. This is consumed directly by the Jira builder.

AGILE_DECOMPOSITION_PROMPT = """
You are an expert Agile project manager and full-stack software architect.

You have been given a project proposal document and its list of modules:
{modules_list}

Team members who will work on this project (distribute work evenly — treat each
person as a full-stack developer capable of working on any part of the system):
{members_list}

This project will be delivered in {num_sprints} sprint(s).

Your task: produce a **complete Agile backlog** for the entire project.

━━━ OUTPUT SCHEMA ━━━
Return ONLY a valid JSON array. No markdown, no explanation, no code fences.
The array must follow this EXACT schema:

[
  {{
    "epic": {{
      "name": "<Epic name — should match a module>",
      "description": "<2-3 sentence summary of the epic's purpose>"
    }},
    "stories": [
      {{
        "title": "As a <role>, I want to <action> so that <benefit>",
        "description": "<Detailed acceptance criteria and context>",
        "story_points": <Fibonacci number: 1, 2, 3, 5, 8, or 13>,
        "assignee": "<One team member name from the list above>",
        "sprint": <integer between 1 and {num_sprints}>,
        "tasks": [
          {{
            "title": "<Concrete engineering sub-task>",
            "description": "<Technical implementation details>",
            "assignee": "<One team member name — may differ from story assignee>"
          }}
        ]
      }}
    ]
  }}
]

━━━ RULES ━━━
1. Create one Epic per module listed above.
2. Each Epic must have 2–3 User Stories.
3. Each Story must have 2–3 Sub-tasks covering frontend, backend, and testing
   concerns — this ensures every member works full-stack.
4. Distribute assignees EVENLY across stories and tasks using round-robin.
   No single team member should have significantly more work than others.
5. Story points must be realistic Fibonacci values (1, 2, 3, 5, 8, 13).
6. Use proper Agile "As a / I want / So that" format for story titles.
7. Sub-task titles must be concrete engineering actions
   (e.g. "Implement JWT refresh token endpoint", "Write Cypress E2E login tests").
8. Distribute stories across sprints evenly. Earlier epics belong to earlier
   sprints. Spread the workload so no sprint has significantly more story points
   than another. Sprint numbers must be integers from 1 to {num_sprints}.
9. The JSON must be valid and parseable — no trailing commas, no comments.

Now produce the full Agile backlog JSON.
""".strip()
