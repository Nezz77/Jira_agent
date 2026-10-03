"""
gui.py
──────
Web-based GUI for the Jira Agile Agent.

Serves a single-page app at http://localhost:7860 that allows users to:
  • Upload a project proposal PDF
  • Configure team members, Jira credentials, and model settings
  • Run the agent with live streaming log output (via Server-Sent Events)
  • View results and click through to the Jira board

Usage:
    python gui.py
    # then open http://localhost:7860 in your browser
"""

from __future__ import annotations

import json
import os
import queue
import subprocess
import sys
import threading
from pathlib import Path

from flask import Flask, Response, jsonify, render_template_string, request, stream_with_context
from dotenv import load_dotenv

# ── Load .env ─────────────────────────────────────────────────────────────────
_SCRIPT_DIR = Path(__file__).parent.resolve()
load_dotenv(dotenv_path=_SCRIPT_DIR / ".env", override=False)

app = Flask(__name__, static_folder=None)

# ── Active run state ──────────────────────────────────────────────────────────
_active_proc: subprocess.Popen | None = None
_log_queue: queue.Queue[str | None] = queue.Queue()
_run_lock = threading.Lock()


# ══════════════════════════════════════════════════════════════════════════════
#  HTML Template
# ══════════════════════════════════════════════════════════════════════════════

HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8"/>
<meta name="viewport" content="width=device-width,initial-scale=1"/>
<title>Jira Agile Agent</title>
<link rel="preconnect" href="https://fonts.googleapis.com"/>
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin/>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700&family=JetBrains+Mono:wght@400;500&display=swap" rel="stylesheet"/>
<style>
  :root {
    --bg: #0d0f14;
    --surface: #13161e;
    --surface2: #1a1e2a;
    --border: #252a38;
    --accent: #6366f1;
    --accent2: #818cf8;
    --accent-glow: rgba(99,102,241,0.25);
    --green: #22c55e;
    --yellow: #f59e0b;
    --red: #ef4444;
    --text: #e2e8f0;
    --muted: #64748b;
    --radius: 14px;
  }
  *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
  html, body { height: 100%; font-family: 'Inter', sans-serif; background: var(--bg); color: var(--text); }

  /* ── Scrollbar ── */
  ::-webkit-scrollbar { width: 6px; height: 6px; }
  ::-webkit-scrollbar-track { background: transparent; }
  ::-webkit-scrollbar-thumb { background: var(--border); border-radius: 99px; }

  /* ── Layout ── */
  .app { display: grid; grid-template-columns: 380px 1fr; min-height: 100vh; }

  /* ── Sidebar ── */
  .sidebar {
    background: var(--surface);
    border-right: 1px solid var(--border);
    display: flex; flex-direction: column;
    padding: 28px 24px;
    gap: 24px;
    overflow-y: auto;
  }

  .logo {
    display: flex; align-items: center; gap: 12px;
    padding-bottom: 24px;
    border-bottom: 1px solid var(--border);
  }
  .logo-icon {
    width: 42px; height: 42px; border-radius: 12px;
    background: linear-gradient(135deg, var(--accent), #a78bfa);
    display: flex; align-items: center; justify-content: center;
    font-size: 20px;
    box-shadow: 0 0 20px var(--accent-glow);
  }
  .logo-text h1 { font-size: 16px; font-weight: 700; color: var(--text); }
  .logo-text p  { font-size: 11px; color: var(--muted); margin-top: 2px; }

  /* ── Section ── */
  .section { display: flex; flex-direction: column; gap: 14px; }
  .section-title {
    font-size: 11px; font-weight: 600; letter-spacing: 0.08em;
    text-transform: uppercase; color: var(--muted);
    display: flex; align-items: center; gap: 8px;
  }
  .section-title::after {
    content: ''; flex: 1; height: 1px; background: var(--border);
  }

  /* ── Form Controls ── */
  label { font-size: 12px; font-weight: 500; color: var(--muted); margin-bottom: 4px; display: block; }
  input[type=text], input[type=email], input[type=password], textarea, select {
    width: 100%; background: var(--surface2);
    border: 1px solid var(--border);
    border-radius: 8px; padding: 9px 12px;
    color: var(--text); font-size: 13px; font-family: 'Inter', sans-serif;
    outline: none; transition: border-color 0.2s, box-shadow 0.2s;
    resize: none;
  }
  input:focus, textarea:focus, select:focus {
    border-color: var(--accent);
    box-shadow: 0 0 0 3px var(--accent-glow);
  }
  textarea { min-height: 70px; }
  select { cursor: pointer; }
  option { background: var(--surface2); }

  /* ── File Upload ── */
  .upload-zone {
    border: 2px dashed var(--border);
    border-radius: var(--radius);
    padding: 20px;
    text-align: center;
    cursor: pointer;
    transition: border-color 0.2s, background 0.2s;
    position: relative;
  }
  .upload-zone:hover, .upload-zone.dragover {
    border-color: var(--accent);
    background: var(--accent-glow);
  }
  .upload-zone input[type=file] {
    position: absolute; inset: 0; opacity: 0; cursor: pointer; width: 100%; height: 100%;
  }
  .upload-icon { font-size: 28px; margin-bottom: 8px; }
  .upload-text { font-size: 13px; color: var(--muted); }
  .upload-text strong { color: var(--accent2); }
  .file-name {
    margin-top: 8px; font-size: 12px; color: var(--green);
    font-family: 'JetBrains Mono', monospace;
    word-break: break-all;
    display: none;
  }

  /* ── Toggle ── */
  .toggle-row { display: flex; align-items: center; justify-content: space-between; }
  .toggle-row label { font-size: 13px; color: var(--text); margin: 0; }
  .toggle {
    width: 36px; height: 20px; background: var(--border);
    border-radius: 99px; position: relative; cursor: pointer;
    transition: background 0.2s;
  }
  .toggle::after {
    content: ''; position: absolute; top: 2px; left: 2px;
    width: 16px; height: 16px; border-radius: 50%;
    background: white; transition: transform 0.2s;
  }
  input[type=checkbox]:checked + .toggle { background: var(--accent); }
  input[type=checkbox]:checked + .toggle::after { transform: translateX(16px); }
  input[type=checkbox] { display: none; }

  /* ── Run Button ── */
  .btn-run {
    padding: 12px; border-radius: 10px;
    background: linear-gradient(135deg, var(--accent), #7c3aed);
    color: white; font-size: 14px; font-weight: 600;
    border: none; cursor: pointer; width: 100%;
    transition: opacity 0.2s, transform 0.15s, box-shadow 0.2s;
    box-shadow: 0 4px 20px var(--accent-glow);
    display: flex; align-items: center; justify-content: center; gap: 8px;
  }
  .btn-run:hover { opacity: 0.9; transform: translateY(-1px); box-shadow: 0 6px 24px var(--accent-glow); }
  .btn-run:active { transform: translateY(0); }
  .btn-run:disabled { opacity: 0.4; cursor: not-allowed; transform: none; }

  .btn-stop {
    padding: 10px; border-radius: 10px;
    background: transparent; border: 1px solid var(--red);
    color: var(--red); font-size: 13px; font-weight: 500;
    cursor: pointer; width: 100%; transition: background 0.2s;
    display: none;
  }
  .btn-stop:hover { background: rgba(239,68,68,0.1); }

  /* ── Main Panel ── */
  .main {
    display: flex; flex-direction: column;
    background: var(--bg);
    overflow: hidden;
  }

  /* ── Top Bar ── */
  .topbar {
    padding: 20px 28px;
    border-bottom: 1px solid var(--border);
    display: flex; align-items: center; justify-content: space-between;
    background: var(--surface);
  }
  .topbar-left { display: flex; flex-direction: column; gap: 4px; }
  .topbar-title { font-size: 16px; font-weight: 600; }
  .topbar-sub { font-size: 12px; color: var(--muted); }

  /* ── Status Badge ── */
  .badge {
    padding: 4px 12px; border-radius: 99px; font-size: 11px; font-weight: 600;
    letter-spacing: 0.05em; text-transform: uppercase;
  }
  .badge-idle    { background: rgba(100,116,139,0.15); color: var(--muted); }
  .badge-running { background: rgba(99,102,241,0.15);  color: var(--accent2); animation: pulse 1.5s infinite; }
  .badge-done    { background: rgba(34,197,94,0.15);   color: var(--green); }
  .badge-error   { background: rgba(239,68,68,0.15);   color: var(--red); }
  @keyframes pulse { 0%,100% { opacity:1; } 50% { opacity:0.6; } }

  /* ── Stages ── */
  .stages {
    display: flex; gap: 0;
    padding: 16px 28px;
    border-bottom: 1px solid var(--border);
    background: var(--surface);
  }
  .stage {
    flex: 1; display: flex; align-items: center; gap: 10px;
    padding: 10px 14px;
    border-radius: 10px;
    transition: background 0.2s;
    position: relative;
  }
  .stage-icon {
    width: 32px; height: 32px; border-radius: 8px;
    background: var(--surface2); border: 1px solid var(--border);
    display: flex; align-items: center; justify-content: center;
    font-size: 14px; transition: all 0.3s; flex-shrink: 0;
  }
  .stage-info { flex: 1; }
  .stage-label { font-size: 11px; font-weight: 600; color: var(--muted); text-transform: uppercase; letter-spacing: 0.05em; }
  .stage-name  { font-size: 13px; font-weight: 500; color: var(--text); margin-top: 1px; }
  .stage.active .stage-icon { background: rgba(99,102,241,0.2); border-color: var(--accent); box-shadow: 0 0 12px var(--accent-glow); }
  .stage.done   .stage-icon { background: rgba(34,197,94,0.15); border-color: var(--green); }
  .stage.error  .stage-icon { background: rgba(239,68,68,0.1); border-color: var(--red); }
  .stage-arrow { color: var(--border); font-size: 18px; padding: 0 4px; align-self: center; }

  /* ── Log Output ── */
  .log-area {
    flex: 1;
    overflow-y: auto;
    padding: 20px 28px;
    font-family: 'JetBrains Mono', monospace;
    font-size: 12.5px;
    line-height: 1.7;
  }
  .log-area .line { white-space: pre-wrap; word-break: break-word; }
  .log-area .line.info    { color: #94a3b8; }
  .log-area .line.success { color: var(--green); }
  .log-area .line.warn    { color: var(--yellow); }
  .log-area .line.error   { color: var(--red); }
  .log-area .line.stage   { color: var(--accent2); font-weight: 600; margin: 8px 0 2px; }
  .log-area .line.plain   { color: #cbd5e1; }
  .log-placeholder {
    height: 100%; display: flex; flex-direction: column;
    align-items: center; justify-content: center; gap: 12px;
    color: var(--muted); text-align: center;
  }
  .log-placeholder .ph-icon { font-size: 48px; opacity: 0.3; }
  .log-placeholder p { font-size: 13px; }

  /* ── Result Banner ── */
  .result-banner {
    margin: 0 28px 20px;
    padding: 18px 22px;
    border-radius: var(--radius);
    display: none;
    animation: slideUp 0.4s ease;
  }
  @keyframes slideUp { from { opacity:0; transform:translateY(10px); } to { opacity:1; transform:translateY(0); } }
  .result-banner.success { background: rgba(34,197,94,0.1); border: 1px solid rgba(34,197,94,0.3); }
  .result-banner.error   { background: rgba(239,68,68,0.08); border: 1px solid rgba(239,68,68,0.3); }
  .result-banner h3 { font-size: 14px; font-weight: 600; margin-bottom: 6px; }
  .result-banner.success h3 { color: var(--green); }
  .result-banner.error   h3 { color: var(--red); }
  .result-banner p  { font-size: 12px; color: var(--muted); }
  .result-banner a  { color: var(--accent2); text-decoration: none; font-weight: 500; }
  .result-banner a:hover { text-decoration: underline; }
  .result-stats { display: flex; gap: 16px; margin-top: 10px; }
  .stat { font-size: 12px; }
  .stat strong { font-size: 20px; font-weight: 700; display: block; }
  .stat.epics   strong { color: #a78bfa; }
  .stat.stories strong { color: var(--accent2); }
  .stat.tasks   strong { color: #38bdf8; }

  /* ── Spinner ── */
  .spinner {
    width: 14px; height: 14px; border-radius: 50%;
    border: 2px solid rgba(255,255,255,0.3);
    border-top-color: white;
    animation: spin 0.7s linear infinite;
    display: none;
  }
  @keyframes spin { to { transform: rotate(360deg); } }
</style>
</head>
<body>
<div class="app">

  <!-- ── Sidebar ─────────────────────────────────────────── -->
  <aside class="sidebar">
    <div class="logo">
      <div class="logo-icon">🚀</div>
      <div class="logo-text">
        <h1>Jira Agile Agent</h1>
        <p>Powered by Gemini AI</p>
      </div>
    </div>

    <!-- PDF Upload -->
    <div class="section">
      <div class="section-title">📄 PDF Proposal</div>
      <div class="upload-zone" id="uploadZone">
        <input type="file" id="pdfFile" accept=".pdf" onchange="onFileChange(this)"/>
        <div class="upload-icon">📁</div>
        <div class="upload-text"><strong>Click to upload</strong> or drag & drop</div>
        <div class="upload-text" style="font-size:11px;margin-top:4px;">PDF files only</div>
        <div class="file-name" id="fileName"></div>
      </div>
    </div>

    <!-- Team -->
    <div class="section">
      <div class="section-title">👥 Team Members</div>
      <div>
        <label>Names or emails (comma-separated)</label>
        <textarea id="members" placeholder="Alice, Bob, Carol&#10;or alice@co.com, bob@co.com"></textarea>
      </div>
    </div>

    <!-- Jira -->
    <div class="section">
      <div class="section-title">🔧 Jira Settings</div>
      <div>
        <label>Jira Domain</label>
        <input type="text" id="jiraDomain" placeholder="yourcompany.atlassian.net" value="{{ jira_domain }}"/>
      </div>
      <div>
        <label>Jira Email</label>
        <input type="email" id="jiraEmail" placeholder="you@example.com" value="{{ jira_email }}"/>
      </div>
      <div>
        <label>Jira API Token</label>
        <input type="password" id="jiraToken" placeholder="ATATT3x..." value="{{ jira_token }}"/>
      </div>
      <div>
        <label>Project Key</label>
        <input type="text" id="projectKey" placeholder="PROJ" value="{{ project_key }}" style="text-transform:uppercase"/>
      </div>
      <div>
        <label>Project Name</label>
        <input type="text" id="projectName" placeholder="My Project" value="{{ project_name }}"/>
      </div>
    </div>

    <!-- AI Settings -->
    <div class="section">
      <div class="section-title">🤖 AI Settings</div>
      <div>
        <label>Gemini API Key</label>
        <input type="password" id="geminiKey" placeholder="AIza..." value="{{ gemini_key }}"/>
      </div>
      <div>
        <label>Model</label>
        <select id="geminiModel">
          <option value="gemini-3.6-flash" {% if gemini_model == 'gemini-3.6-flash' %}selected{% endif %}>gemini-3.6-flash (Fast)</option>
          <option value="gemini-3.8-flash" {% if gemini_model == 'gemini-3.8-flash' %}selected{% endif %}>gemini-3.8-flash</option>
          <option value="gemini-3.5-flash-lite" {% if gemini_model == 'gemini-3.5-flash-lite' %}selected{% endif %}>gemini-3.5-flash-lite</option>
        </select>
      </div>
      <div>
        <label>Number of Sprints</label>
        <input type="text" id="numSprints" value="1" placeholder="1"/>
      </div>
    </div>

    <!-- Options -->
    <div class="section">
      <div class="section-title">⚙️ Options</div>
      <div class="toggle-row">
        <label>Dry Run (preview only)</label>
        <input type="checkbox" id="dryRun"/><div class="toggle" onclick="document.getElementById('dryRun').click()"></div>
      </div>
      <div class="toggle-row">
        <label>Skip PDF stage (use cached)</label>
        <input type="checkbox" id="skipStage1"/><div class="toggle" onclick="document.getElementById('skipStage1').click()"></div>
      </div>
      <div class="toggle-row">
        <label>Skip AI stage (use backlog.json)</label>
        <input type="checkbox" id="skipStage2"/><div class="toggle" onclick="document.getElementById('skipStage2').click()"></div>
      </div>
    </div>

    <!-- Actions -->
    <div style="display:flex;flex-direction:column;gap:8px;margin-top:auto;">
      <button class="btn-run" id="runBtn" onclick="startRun()">
        <div class="spinner" id="spinner"></div>
        <span id="runBtnText">▶ Run Agent</span>
      </button>
      <button class="btn-stop" id="stopBtn" onclick="stopRun()">⏹ Stop</button>
    </div>
  </aside>

  <!-- ── Main Panel ────────────────────────────────────────── -->
  <main class="main">
    <!-- Top Bar -->
    <div class="topbar">
      <div class="topbar-left">
        <div class="topbar-title">Agent Output</div>
        <div class="topbar-sub" id="topbarSub">Configure settings and upload a PDF to begin.</div>
      </div>
      <span class="badge badge-idle" id="statusBadge">Idle</span>
    </div>

    <!-- Stages -->
    <div class="stages">
      <div class="stage" id="stage1">
        <div class="stage-icon">📄</div>
        <div class="stage-info">
          <div class="stage-label">Stage 1</div>
          <div class="stage-name">PDF Ingestion</div>
        </div>
      </div>
      <div class="stage-arrow">›</div>
      <div class="stage" id="stage2">
        <div class="stage-icon">🤖</div>
        <div class="stage-info">
          <div class="stage-label">Stage 2</div>
          <div class="stage-name">AI Decomposition</div>
        </div>
      </div>
      <div class="stage-arrow">›</div>
      <div class="stage" id="stage3">
        <div class="stage-icon">📋</div>
        <div class="stage-info">
          <div class="stage-label">Stage 3</div>
          <div class="stage-name">Jira Board Build</div>
        </div>
      </div>
    </div>

    <!-- Result Banner -->
    <div class="result-banner" id="resultBanner">
      <h3 id="resultTitle"></h3>
      <p id="resultMsg"></p>
      <div class="result-stats" id="resultStats" style="display:none"></div>
    </div>

    <!-- Log Output -->
    <div class="log-area" id="logArea">
      <div class="log-placeholder" id="logPlaceholder">
        <div class="ph-icon">🚀</div>
        <p>Your agent output will appear here.<br/>Upload a PDF and click <strong>Run Agent</strong> to start.</p>
      </div>
    </div>
  </main>
</div>

<script>
let eventSource = null;
let currentStage = 0;

// ── Drag & Drop ────────────────────────────────────────────────────
const zone = document.getElementById('uploadZone');
zone.addEventListener('dragover', e => { e.preventDefault(); zone.classList.add('dragover'); });
zone.addEventListener('dragleave', () => zone.classList.remove('dragover'));
zone.addEventListener('drop', e => {
  e.preventDefault(); zone.classList.remove('dragover');
  const file = e.dataTransfer.files[0];
  if (file && file.type === 'application/pdf') {
    const dt = new DataTransfer();
    dt.items.add(file);
    document.getElementById('pdfFile').files = dt.files;
    showFileName(file.name);
  }
});

function onFileChange(input) {
  if (input.files[0]) showFileName(input.files[0].name);
}
function showFileName(name) {
  const el = document.getElementById('fileName');
  el.textContent = '✓ ' + name;
  el.style.display = 'block';
}

// ── Stage Management ──────────────────────────────────────────────
function setStage(n, state) {
  const el = document.getElementById('stage' + n);
  el.classList.remove('active', 'done', 'error');
  if (state) el.classList.add(state);
}
function resetStages() {
  [1,2,3].forEach(n => setStage(n, null));
}

// ── Log Helpers ───────────────────────────────────────────────────
function appendLog(text, cls='plain') {
  const logArea = document.getElementById('logArea');
  const ph = document.getElementById('logPlaceholder');
  if (ph) ph.remove();
  const line = document.createElement('div');
  line.className = 'line ' + cls;
  line.textContent = text;
  logArea.appendChild(line);
  logArea.scrollTop = logArea.scrollHeight;
}

function classifyLine(line) {
  if (line.includes('Stage 1') || line.includes('PDF Ingestion')) return 'stage';
  if (line.includes('Stage 2') || line.includes('Agile Decomposition')) return 'stage';
  if (line.includes('Stage 3') || line.includes('Jira Board')) return 'stage';
  if (line.includes('ERROR') || line.includes('FATAL') || line.includes('✗')) return 'error';
  if (line.includes('WARNING') || line.includes('⚠')) return 'warn';
  if (line.includes('✓') || line.includes('successfully') || line.includes('created')) return 'success';
  if (line.includes('INFO')) return 'info';
  return 'plain';
}

// ── Status ─────────────────────────────────────────────────────────
function setStatus(state, text) {
  const badge = document.getElementById('statusBadge');
  badge.className = 'badge badge-' + state;
  badge.textContent = text;
}

// ── Start Run ─────────────────────────────────────────────────────
async function startRun() {
  const pdfInput = document.getElementById('pdfFile');
  const skipStage1 = document.getElementById('skipStage1').checked;
  const skipStage2 = document.getElementById('skipStage2').checked;

  if (!pdfInput.files[0] && !skipStage1 && !skipStage2) {
    alert('Please upload a PDF file first, or enable Skip PDF Stage.');
    return;
  }

  // Clear log
  const logArea = document.getElementById('logArea');
  logArea.innerHTML = '';

  // Reset UI
  resetStages();
  setStatus('running', 'Running');
  document.getElementById('runBtn').disabled = true;
  document.getElementById('spinner').style.display = 'block';
  document.getElementById('runBtnText').textContent = 'Running…';
  document.getElementById('stopBtn').style.display = 'block';
  document.getElementById('resultBanner').style.display = 'none';
  document.getElementById('topbarSub').textContent = 'Agent is running…';

  // Collect config
  const formData = new FormData();
  if (pdfInput.files[0]) formData.append('pdf', pdfInput.files[0]);
  formData.append('members',     document.getElementById('members').value);
  formData.append('jira_domain', document.getElementById('jiraDomain').value);
  formData.append('jira_email',  document.getElementById('jiraEmail').value);
  formData.append('jira_token',  document.getElementById('jiraToken').value);
  formData.append('project_key', document.getElementById('projectKey').value.toUpperCase());
  formData.append('project_name',document.getElementById('projectName').value);
  formData.append('gemini_key',  document.getElementById('geminiKey').value);
  formData.append('gemini_model',document.getElementById('geminiModel').value);
  formData.append('num_sprints', document.getElementById('numSprints').value);
  formData.append('dry_run',     document.getElementById('dryRun').checked ? '1' : '0');
  formData.append('skip_stage1', skipStage1 ? '1' : '0');
  formData.append('skip_stage2', skipStage2 ? '1' : '0');

  // Submit
  const resp = await fetch('/run', { method: 'POST', body: formData });
  const data = await resp.json();
  if (!resp.ok) {
    appendLog('Error: ' + (data.error || 'Unknown error'), 'error');
    finishRun(false);
    return;
  }

  // Stream logs via SSE
  setStage(1, 'active');
  currentStage = 1;
  eventSource = new EventSource('/stream');
  eventSource.onmessage = (e) => {
    const msg = JSON.parse(e.data);

    if (msg.type === 'line') {
      const text = msg.text;
      appendLog(text, classifyLine(text));

      // Update stage indicators
      if (text.includes('Stage 1') || text.includes('PDF Ingestion')) {
        setStage(1, 'active'); setStage(2, null); setStage(3, null); currentStage = 1;
        document.getElementById('topbarSub').textContent = 'Stage 1 — PDF Ingestion running…';
      } else if (text.includes('Stage 2') || text.includes('Agile Decomposition')) {
        setStage(1, 'done'); setStage(2, 'active'); setStage(3, null); currentStage = 2;
        document.getElementById('topbarSub').textContent = 'Stage 2 — AI Decomposition running…';
      } else if (text.includes('Stage 3') || text.includes('Jira Board')) {
        setStage(1, 'done'); setStage(2, 'done'); setStage(3, 'active'); currentStage = 3;
        document.getElementById('topbarSub').textContent = 'Stage 3 — Building Jira board…';
      }
    } else if (msg.type === 'done') {
      eventSource.close();
      showResult(msg);
      finishRun(msg.success);
    }
  };
  eventSource.onerror = () => {
    eventSource.close();
    finishRun(false);
  };
}

// ── Stop ──────────────────────────────────────────────────────────
function stopRun() {
  if (eventSource) { eventSource.close(); eventSource = null; }
  fetch('/stop', { method: 'POST' });
  appendLog('\n[Stopped by user]', 'warn');
  finishRun(false, true);
}

// ── Finish ────────────────────────────────────────────────────────
function finishRun(success, stopped=false) {
  document.getElementById('runBtn').disabled = false;
  document.getElementById('spinner').style.display = 'none';
  document.getElementById('runBtnText').textContent = '▶ Run Agent';
  document.getElementById('stopBtn').style.display = 'none';

  if (stopped) {
    setStatus('idle', 'Stopped');
    document.getElementById('topbarSub').textContent = 'Run was stopped.';
    [1,2,3].forEach(n => setStage(n, null));
    return;
  }
  if (success) {
    setStatus('done', 'Done');
    setStage(1, 'done'); setStage(2, 'done'); setStage(3, 'done');
    document.getElementById('topbarSub').textContent = 'Jira board populated successfully!';
  } else {
    setStatus('error', 'Error');
    setStage(currentStage, 'error');
    document.getElementById('topbarSub').textContent = 'Run failed — check logs above.';
  }
}

// ── Result Banner ──────────────────────────────────────────────────
function showResult(msg) {
  const banner = document.getElementById('resultBanner');
  banner.className = 'result-banner ' + (msg.success ? 'success' : 'error');
  document.getElementById('resultTitle').textContent = msg.success
    ? '✅ Jira Board Populated Successfully!'
    : '❌ Run Failed';

  if (msg.success && msg.board_url) {
    document.getElementById('resultMsg').innerHTML =
      `Board: <a href="${msg.board_url}" target="_blank">${msg.board_url}</a>`;
    const stats = document.getElementById('resultStats');
    stats.style.display = 'flex';
    stats.innerHTML = `
      <div class="stat epics">  <strong>${msg.epics || 0}</strong>   Epics</div>
      <div class="stat stories"><strong>${msg.stories || 0}</strong> Stories</div>
      <div class="stat tasks">  <strong>${msg.tasks || 0}</strong>   Sub-tasks</div>`;
  } else {
    document.getElementById('resultMsg').textContent = msg.error || 'Check the log for details.';
    document.getElementById('resultStats').style.display = 'none';
  }
  banner.style.display = 'block';
}
</script>
</body>
</html>
"""


# ══════════════════════════════════════════════════════════════════════════════
#  Routes
# ══════════════════════════════════════════════════════════════════════════════

@app.route("/")
def index():
    return render_template_string(
        HTML,
        jira_domain=os.getenv("JIRA_DOMAIN", ""),
        jira_email=os.getenv("JIRA_EMAIL", ""),
        jira_token=os.getenv("JIRA_API_TOKEN", ""),
        project_key=os.getenv("JIRA_PROJECT_KEY", ""),
        project_name=os.getenv("JIRA_PROJECT_NAME", ""),
        gemini_key=os.getenv("GEMINI_API_KEY", ""),
        gemini_model=os.getenv("GEMINI_MODEL", "gemini-3.6-flash"),
    )


@app.route("/run", methods=["POST"])
def run():
    global _active_proc

    with _run_lock:
        if _active_proc and _active_proc.poll() is None:
            return jsonify({"error": "An agent run is already in progress."}), 409

        # ── Save uploaded PDF ──────────────────────────────────────────────
        pdf_file = request.files.get("pdf")
        pdf_path: str | None = None

        if pdf_file and pdf_file.filename:
            upload_dir = _SCRIPT_DIR / ".uploads"
            upload_dir.mkdir(exist_ok=True)
            pdf_path = str(upload_dir / pdf_file.filename)
            pdf_file.save(pdf_path)

        skip_stage1 = request.form.get("skip_stage1") == "1"
        skip_stage2 = request.form.get("skip_stage2") == "1"

        if not pdf_path and not skip_stage1 and not skip_stage2:
            return jsonify({"error": "No PDF uploaded and no stage skipped."}), 400

        # ── Build CLI command ──────────────────────────────────────────────
        cmd = [sys.executable, str(_SCRIPT_DIR / "agent.py")]

        if pdf_path:
            cmd += ["--pdf", pdf_path]
        elif not skip_stage1:
            # Use a dummy path — skip logic will handle it
            cmd += ["--pdf", ""]

        members = request.form.get("members", "").strip()
        if members:
            cmd += ["--members", members]

        cmd += ["--model", request.form.get("gemini_model", "gemini-3.6-flash")]

        sprints = request.form.get("num_sprints", "1").strip()
        if sprints.isdigit():
            cmd += ["--sprints", sprints]

        if request.form.get("dry_run") == "1":
            cmd.append("--dry-run")

        skip_n = None
        if skip_stage2:
            skip_n = 2
        elif skip_stage1:
            skip_n = 1
        if skip_n:
            cmd += ["--skip-stage", str(skip_n)]

        # ── Override env vars from form ────────────────────────────────────
        env = os.environ.copy()
        overrides = {
            "JIRA_DOMAIN":       request.form.get("jira_domain", ""),
            "JIRA_EMAIL":        request.form.get("jira_email", ""),
            "JIRA_API_TOKEN":    request.form.get("jira_token", ""),
            "JIRA_PROJECT_KEY":  request.form.get("project_key", "").upper(),
            "JIRA_PROJECT_NAME": request.form.get("project_name", ""),
            "GEMINI_API_KEY":    request.form.get("gemini_key", ""),
        }
        for k, v in overrides.items():
            if v:
                env[k] = v

        # ── Clear old queue ────────────────────────────────────────────────
        while not _log_queue.empty():
            try: _log_queue.get_nowait()
            except queue.Empty: break

        # ── Start subprocess ───────────────────────────────────────────────
        _active_proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            env=env,
            cwd=str(_SCRIPT_DIR),
        )

        # ── Read output in background thread ──────────────────────────────
        def _reader():
            assert _active_proc is not None
            for line in _active_proc.stdout:  # type: ignore[union-attr]
                _log_queue.put(("line", line.rstrip()))
            _active_proc.wait()
            _log_queue.put(("done", _active_proc.returncode))

        threading.Thread(target=_reader, daemon=True).start()

    return jsonify({"ok": True})


@app.route("/stream")
def stream():
    """Server-Sent Events endpoint — streams agent output to the browser."""

    def _generate():
        epics = stories = tasks = 0
        board_url = ""
        last_lines: list[str] = []

        while True:
            try:
                item = _log_queue.get(timeout=30)
            except queue.Empty:
                yield "data: {}\n\n"  # keepalive
                continue

            kind, payload = item

            if kind == "line":
                text: str = payload
                last_lines.append(text)
                if len(last_lines) > 50:
                    last_lines.pop(0)

                # Count created issues from summary output
                if "│ Epic" in text:      epics += 1
                if "│ Story" in text:     stories += 1
                if "│ Sub-task" in text:  tasks += 1

                # Capture board URL
                if "/jira/software/" in text and "https://" in text:
                    for word in text.split():
                        if word.startswith("https://") and "/boards" in word:
                            board_url = word.rstrip(".,")

                yield f"data: {json.dumps({'type':'line','text':text})}\n\n"

            elif kind == "done":
                rc: int = payload
                success = (rc == 0)
                # Try extracting board URL from recent lines if not found
                if not board_url:
                    for ln in reversed(last_lines):
                        if "https://" in ln and "/boards" in ln:
                            for word in ln.split():
                                if word.startswith("https://") and "/boards" in word:
                                    board_url = word.rstrip(".,")
                                    break
                        if board_url:
                            break

                yield f"data: {json.dumps({'type':'done','success':success,'board_url':board_url,'epics':epics,'stories':stories,'tasks':tasks})}\n\n"
                break

    return Response(
        stream_with_context(_generate()),
        mimetype="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


@app.route("/stop", methods=["POST"])
def stop():
    global _active_proc
    if _active_proc and _active_proc.poll() is None:
        _active_proc.terminate()
    return jsonify({"ok": True})


# ══════════════════════════════════════════════════════════════════════════════
#  Entry Point
# ══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    import webbrowser
    port = int(os.getenv("GUI_PORT", "7860"))
    print(f"\n  🚀 Jira Agile Agent GUI")
    print(f"  Open → http://localhost:{port}\n")
    webbrowser.open(f"http://localhost:{port}")
    app.run(host="0.0.0.0", port=port, debug=False, threaded=True)
