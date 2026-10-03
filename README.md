<div align="center">

```
 ▄▄▄· ▄▄ • ▄▄▄ . ▐ ▄ ▄▄▄▄▄      ▄▄ • ▄▄▄   ▄▄▄·  ▌ ▐·▪  ▄▄▄▄▄ ▄· ▄▌
▐█ ▀█ ▐█ ▀ ▪▀▄.▀·•█▌▐█•██  ▪     ▐█ ▀ ▪▀▄ █·▐█ ▀█ ▪█·█▌██ •██  ▐█▪██▌
▄█▀▀█ ▄█ ▀█▄▐▀▀▪▄▐█▐▐▌ ▐█.▪ ▄█▀▄ ▄█ ▀█▄▐▀▀▄ ▄█▀▀█ ▐█▐█•▐█· ▐█.▪▐█▌▐█▪
▐█ ▪▐▌▐█▄▪▐█▐█▄▄▌██▐█▌ ▐█▌·▐█▌.▐▌▐█▄▪▐█▐█•█▌▐█ ▪▐▌ ███ ▐█▌ ▐█▌· ▐█▀·.
 ▀  ▀ ·▀▀▀▀  ▀▀▀ ▀▀ █▪ ▀▀▀  ▀█▄▀▪·▀▀▀▀ .▀  ▀ ▀  ▀ . ▀  ▀▀▀ ▀▀▀   ▀ •
```

# 🚀 Agent Jira — AI-Powered Agile Board Generator

**Upload a project proposal PDF → Gemini AI decomposes it → Your Jira board is built automatically.**

[![Python](https://img.shields.io/badge/Python-3.9%2B-blue?logo=python)](https://python.org)
[![Gemini](https://img.shields.io/badge/Powered%20by-Gemini%20AI-purple?logo=google)](https://ai.google.dev)
[![Jira](https://img.shields.io/badge/Integrates%20with-Jira%20Cloud-blue?logo=jira)](https://atlassian.com/jira)
[![License](https://img.shields.io/badge/License-MIT-green)](LICENSE)

</div>

---

## ✨ What It Does

Agent Jira is a fully autonomous, three-stage CLI pipeline that transforms an unstructured project proposal document into a populated, ready-to-use Jira Agile board — with zero manual ticket creation.

```
📄 PDF Proposal
      │
      ▼  Stage 1 — PDF Ingestion (Gemini Vision)
📋 Module List
      │
      ▼  Stage 2 — Agile Decomposition (Gemini AI)
📦 Backlog JSON  (Epics → Stories → Sub-tasks, with story points & assignees)
      │
      ▼  Stage 3 — Jira Board Builder
🎯 Live Jira Board
```

### Stage 1 — PDF Ingestion
Uses **Gemini's multimodal capabilities** to read and understand your proposal PDF, extracting a structured list of software modules and functional areas.

### Stage 2 — Agile Decomposition
Feeds the module list back to **Gemini** with an Agile prompt to generate a full backlog:
- **Epics** — one per module
- **Stories** — user-story formatted (`As a ... I want ... so that ...`)
- **Sub-tasks** — granular technical tasks per story
- **Story points** — Fibonacci scale (1, 2, 3, 5, 8, 13)
- **Assignees** — distributed evenly across team members via round-robin

### Stage 3 — Jira Board Builder
Authenticates with Jira Cloud via the REST API and creates every issue in the correct hierarchy:
- Ensures the project exists (creates it if not)
- Creates Epics, then Stories (as children of Epics), then Sub-tasks
- Resolves team member names/emails to Jira account IDs for assignment
- Rate-limit-aware with automatic retries

---

## 📋 Prerequisites

| Requirement | Details |
|---|---|
| **Python** | 3.9 or newer |
| **Google Gemini API Key** | [Get one free at AI Studio](https://aistudio.google.com/app/apikey) |
| **Jira Cloud account** | Any tier (Free works) |
| **Jira API Token** | [Generate here](https://id.atlassian.com/manage-profile/security/api-tokens) |

---

## ⚙️ Installation

### 1. Clone the repository

```bash
git clone https://github.com/Nezz77/Jira_agent.git
cd Jira_agent
```

### 2. Create and activate a virtual environment

```bash
python3 -m venv .venv
source .venv/bin/activate      # macOS / Linux
# .venv\Scripts\activate       # Windows
```

### 3. Install dependencies

```bash
pip install -r requirements.txt
```

### 4. Configure environment variables

```bash
cp .env.example .env
```

Open `.env` and fill in your credentials:

```dotenv
# Google Gemini
GEMINI_API_KEY=your_google_ai_studio_key
GEMINI_MODEL=gemini-3.6-flash         # or gemini-2.5-pro for higher quality

# Jira / Atlassian
JIRA_DOMAIN=yourcompany.atlassian.net
JIRA_EMAIL=you@example.com
JIRA_API_TOKEN=your_jira_api_token
JIRA_PROJECT_KEY=PROJ
JIRA_PROJECT_NAME=My Project
```

> ⚠️ **Never commit your `.env` file.** It is listed in `.gitignore` and excluded from all commits.

---

## 🚀 Usage

### 🖥️ Web GUI (Recommended)

A full-featured web interface is included for an easier experience with live log streaming.

```bash
python gui.py
```
*Then open http://localhost:7860 in your browser.*

### 💻 CLI Usage

#### Basic run (all three stages)

```bash
python agent.py \
  --pdf path/to/proposal.pdf \
  --members "Alice,Bob,Carol,Diana"
```

#### Full options

```bash
python agent.py \
  --pdf path/to/proposal.pdf \
  --members "Alice,Bob,Carol" \
  --model gemini-3.6-flash \
  --project-key MYPROJ \
  --project-name "My Awesome Project" \
  --dry-run
```

### Skip already-completed stages (useful for re-runs)

```bash
# Skip Stage 1 — reuse existing modules.txt
python agent.py --pdf proposal.pdf --members "Alice,Bob" --skip-stage 1

# Skip Stages 1 & 2 — reuse existing backlog.json
python agent.py --pdf proposal.pdf --members "Alice,Bob" --skip-stage 2
```

### All CLI flags

| Flag | Default | Description |
|---|---|---|
| `--pdf PATH` | *(required)* | Path to the project proposal PDF |
| `--members "A,B,C"` | *(none)* | Comma-separated team member names or emails |
| `--model MODEL` | `gemini-3.6-flash` | Gemini model name (overrides `GEMINI_MODEL` in `.env`) |
| `--project-key KEY` | from `.env` | Jira project key, e.g. `PROJ` |
| `--project-name NAME` | from `.env` | Human-readable project name (for new projects) |
| `--modules-out PATH` | `modules.txt` | Output path for the extracted modules file |
| `--backlog-out PATH` | `backlog.json` | Output path for the Agile backlog JSON |
| `--dry-run` | `false` | Run AI stages but **skip Jira creation** |
| `--skip-stage N` | *(none)* | Skip stage N and load from cached file |

---

## 👥 Assigning Teammates

For the agent to successfully assign issues to your teammates in Jira, they **must have active Atlassian accounts in your workspace** before you run the script.

1. Go to your Jira User Management page (`https://<yourdomain>.atlassian.net/admin/users`)
2. Click **Invite users** and enter their email addresses.
3. **Wait for them to accept the email invitation** and create/log into their accounts.
4. Once they are "Active" in your workspace, you can enter their names or emails in the agent. The script will automatically resolve their names to Jira `accountIds` and assign the issues.

*(If you run the script before they accept their invites, Jira will default to assigning all issues to the Project Lead).*

---

## 📁 Project Structure

```
Agent_Jira/
├── agent.py            # Main orchestrator — CLI entry point
├── pdf_ingestion.py    # Stage 1: PDF → module list via Gemini
├── ai_decomposer.py    # Stage 2: module list → Agile backlog via Gemini
├── jira_builder.py     # Stage 3: backlog → Jira issues via REST API
├── prompts.py          # Gemini prompt templates
├── utils.py            # Shared utilities (logging, retry, rich console)
├── requirements.txt    # Python dependencies
├── .env.example        # Environment variable template (safe to commit)
└── .env                # Your actual credentials (NEVER commit this)
```

---

## 🔄 Pipeline Outputs

After a successful run, two intermediate files are produced:

| File | Contents | Auto-generated? |
|---|---|---|
| `modules.txt` | Extracted module names from the PDF | ✅ Yes |
| `backlog.json` | Full structured Agile backlog (Epics/Stories/Tasks) | ✅ Yes |

Both files can be reused via `--skip-stage` to avoid re-running expensive AI calls.

---

## 🛡️ Resilience & Reliability

- **Multi-Model Fallback** — Automatically rotates through multiple Gemini API keys and falls back to Gemini 3.8 Flash, 3.5 Flash Lite, and finally DeepSeek if quotas are exhausted.
- **Automatic retries** with exponential back-off on all AI and Jira API calls
- **Rate-limit awareness** — sleeps between Jira write calls to stay within API quotas
- **Schema validation** — Gemini output is validated against the expected backlog schema before any Jira calls are made
- **Story point normalisation** — any non-Fibonacci values are clamped to the nearest valid point
- **Round-robin enforcement** — work is always evenly distributed regardless of what Gemini assigns
- **Graceful fallbacks** — optional Jira fields (e.g. story points) are retried without them if rejected by the project screen configuration

---

## 🔧 Troubleshooting

| Error | Likely Cause | Fix |
|---|---|---|
| `404 NOT_FOUND` on Gemini | Model name is deprecated | Use `--model gemini-3.6-flash` |
| `customfield_10011` 400 error | Classic-only Epic Name field | Already fixed — field is omitted |
| `Specify a valid issue type` | Wrong sub-task type name | Already fixed — uses `Sub-task` (with hyphen) |
| `JSON parse error` | Gemini returned malformed JSON | Try `--dry-run` to inspect output; use a better model |
| `JIRAError 401` | Wrong API token or email | Regenerate token at [Atlassian security settings](https://id.atlassian.com/manage-profile/security/api-tokens) |
| `JIRAError 403` | Insufficient permissions | Ensure your account has project-admin rights |
| PDF not found | Wrong path | Use an absolute path or run from the correct directory |

---

## 🤝 Contributing

Contributions are welcome! Please:

1. Fork the repository
2. Create a feature branch (`git checkout -b feat/my-feature`)
3. Commit your changes (`git commit -m 'feat: add my feature'`)
4. Push to the branch (`git push origin feat/my-feature`)
5. Open a Pull Request

---

## 📄 License

MIT License — see [LICENSE](LICENSE) for details.

---

<div align="center">
Built by FutureTech <a href="https://ai.google.dev">Gemini AI</a> + <a href="https://www.atlassian.com/software/jira">Jira</a>
</div>
