"""
pdf_ingestion.py
────────────────
Stage 1 of the Jira Agile Agent pipeline:

  1. Validate the local PDF file.
  2. Upload it to the Gemini File API (handles files up to 2 GB).
  3. Call Gemini to extract a list of high-level project modules.
  4. Write those modules to `modules.txt` as an intermediate artifact.
  5. Return the module list for Stage 2 (ai_decomposer.py).

Uses the new `google.genai` SDK (replaces deprecated `google.generativeai`).
"""

from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Any

from google import genai
from google.genai import types
import openai
import pypdf
from pypdf import PdfReader

from prompts import MODULE_EXTRACTION_PROMPT
from utils import (
    call_with_model_fallback,
    console,
    extract_json,
    is_overload_error,
    make_retry_decorator,
    print_banner,
)

logger = logging.getLogger("jira_agent.pdf")

# ── Gemini retry decorator for non-overload transient errors ──────────────────
# 503/429 overload errors are handled by call_with_model_fallback; this
# decorator only fires on other transient failures (network drops, etc.).
_gemini_retry = make_retry_decorator(
    max_attempts=3,
    min_wait=5.0,
    max_wait=60.0,
    exceptions=(Exception,),
)


def validate_pdf(pdf_path: str) -> Path:
    """
    Ensure the given path points to a readable, non-empty PDF file.

    Args:
        pdf_path: Absolute or relative path to the PDF.

    Returns:
        A resolved pathlib.Path object.

    Raises:
        FileNotFoundError: If the file does not exist.
        ValueError:        If the file is not a valid PDF or is empty.
    """
    path = Path(pdf_path).resolve()

    if not path.exists():
        raise FileNotFoundError(f"PDF not found: {path}")

    if path.suffix.lower() != ".pdf":
        raise ValueError(f"Expected a .pdf file, got: {path.suffix}")

    if path.stat().st_size == 0:
        raise ValueError(f"PDF file is empty: {path}")

    # Quick structural validation — PdfReader raises if the file is corrupt
    try:
        reader = PdfReader(str(path))
        page_count = len(reader.pages)
        if page_count == 0:
            raise ValueError("PDF has zero pages.")
        logger.info(
            "PDF validated: %s (%d pages, %.1f KB)",
            path.name, page_count, path.stat().st_size / 1024,
        )
    except Exception as exc:
        raise ValueError(f"PDF appears to be corrupt or unreadable: {exc}") from exc

    return path


def _upload_pdf_to_gemini(client: genai.Client, path: Path) -> Any:
    """
    Upload the PDF to the Gemini File API and wait until it is in ACTIVE state.

    Args:
        client: Authenticated genai.Client instance.
        path:   Validated local PDF path.

    Returns:
        A Gemini File object with `.name`, `.uri`, and `.state` attributes.
    """
    console.print(f"  [dim]Uploading [bold]{path.name}[/bold] to Gemini File API…[/dim]")

    uploaded_file = client.files.upload(
        file=str(path),
        config=types.UploadFileConfig(
            mime_type="application/pdf",
            display_name=path.stem,
        ),
    )

    logger.info("File uploaded — name: %s | state: %s", uploaded_file.name, uploaded_file.state)

    # Poll until the file transitions from PROCESSING → ACTIVE
    max_wait_seconds = 120
    poll_interval = 5
    waited = 0

    while "PROCESSING" in str(uploaded_file.state).upper():
        if waited >= max_wait_seconds:
            raise TimeoutError(
                f"Gemini file processing timed out after {max_wait_seconds}s. "
                "Try again or use a smaller PDF."
            )
        console.print(f"  [dim]⏳ File still processing… ({waited}s elapsed)[/dim]")
        time.sleep(poll_interval)
        waited += poll_interval
        uploaded_file = client.files.get(name=uploaded_file.name)

    if "ACTIVE" not in str(uploaded_file.state).upper():
        raise RuntimeError(
            f"Uploaded file entered unexpected state: {uploaded_file.state}"
        )

    logger.info("File is ACTIVE and ready for inference.")
    return uploaded_file


@_gemini_retry
def _call_gemini_for_modules(
    client: Any,
    pdf_path_obj: Path,
    model_name: str,
) -> list[str]:
    """
    Ask Gemini or DeepSeek to read the uploaded PDF and return the list of project modules.
    """
    logger.info("Calling LLM to extract modules from PDF…")

    if isinstance(client, openai.OpenAI):
        logger.info("Extracting text locally for DeepSeek...")
        reader = pypdf.PdfReader(pdf_path_obj)
        text = "\n".join(page.extract_text() for page in reader.pages)
        response = client.chat.completions.create(
            model=model_name,
            messages=[{"role": "user", "content": text + "\n\n" + MODULE_EXTRACTION_PROMPT}],
            temperature=0.2,
        )
        raw_text = response.choices[0].message.content
    else:
        # Upload to Gemini File API 
        console.print("[dim]  Uploading PDF to Gemini File API…[/dim]")
        gemini_file = _upload_pdf_to_gemini(client, pdf_path_obj)
        
        response = client.models.generate_content(
            model=model_name,
            contents=[gemini_file, MODULE_EXTRACTION_PROMPT],
            config=types.GenerateContentConfig(
                temperature=0.2,
                max_output_tokens=8192,
            ),
        )
        raw_text = response.text
        
        try:
            client.files.delete(name=gemini_file.name)
            logger.info("Deleted uploaded file from Gemini File API: %s", gemini_file.name)
        except Exception as exc:
            logger.warning("Could not delete Gemini file (non-fatal): %s", exc)

    logger.debug("Raw module response:\n%s", raw_text)

    modules: list[str] = extract_json(raw_text)

    if not isinstance(modules, list) or not all(isinstance(m, str) for m in modules):
        raise ValueError(
            "LLM did not return a JSON array of strings for modules. "
            f"Got: {type(modules)} — {str(modules)[:200]}"
        )

    if len(modules) == 0:
        raise ValueError("LLM returned an empty module list.")

    # Normalise: strip whitespace, remove empty strings
    modules = [m.strip() for m in modules if m.strip()]
    logger.info("Extracted %d modules: %s", len(modules), modules)
    return modules


def write_modules_file(modules: list[str], output_path: str = "modules.txt") -> Path:
    """
    Write the extracted module list to a plain-text file (one module per line).

    Args:
        modules:     List of module name strings.
        output_path: File path to write to (default: modules.txt in CWD).

    Returns:
        The resolved path of the written file.
    """
    path = Path(output_path).resolve()

    with path.open("w", encoding="utf-8") as f:
        f.write("# Project Modules — Auto-extracted by Jira Agile Agent\n")
        f.write("# Each line represents one Agile Epic on the Jira board.\n\n")
        for i, module in enumerate(modules, start=1):
            f.write(f"{i}. {module}\n")

    logger.info("Module list written to: %s", path)
    return path


def ingest_pdf(
    pdf_path: str,
    gemini_api_key: str,
    gemini_model_name: str = "gemini-3.6-flash",
    modules_output_path: str = "modules.txt",
    extra_api_keys: list[str] | None = None,
    deepseek_api_key: str | None = None,
) -> list[str]:
    """
    Full Stage 1 pipeline: validate → upload → extract modules → write file.

    Args:
        pdf_path:            Path to the project proposal PDF.
        gemini_api_key:      Google AI Studio API key.
        gemini_model_name:   Gemini model to use.
        modules_output_path: Where to write modules.txt.

    Returns:
        List of module name strings.
    """
    print_banner(
        "Stage 1 — PDF Ingestion & Module Extraction",
        f"PDF: {pdf_path}",
    )

    # ── Create Gemini client ─────────────────────────────────────────────────
    client = genai.Client(api_key=gemini_api_key)

    # ── Validate the PDF ─────────────────────────────────────────────────────
    with console.status("[bold green]Validating PDF…"):
        pdf_path_obj = validate_pdf(pdf_path)

    console.print(f"  [green]✓[/green] PDF validated: [bold]{pdf_path_obj.name}[/bold]")

    # ── Extract modules via LLM ───────────────────────────────────────────
    console.print(
        f"  [dim]Primary model: [bold]{gemini_model_name}[/bold] "
        "(auto-fallback enabled on 503/429)…[/dim]"
    )
    with console.status("[bold green]Asking LLM to identify project modules…"):
        modules = call_with_model_fallback(
            _call_gemini_for_modules,
            gemini_model_name,
            client,
            pdf_path_obj,
            gemini_model_name,
            model_arg_index=2,
            extra_api_keys=extra_api_keys,
            deepseek_api_key=deepseek_api_key,
        )

    console.print(f"  [green]✓[/green] Extracted [bold]{len(modules)}[/bold] modules:")
    for m in modules:
        console.print(f"      • {m}")

    # ── Write modules.txt ────────────────────────────────────────────────────
    modules_path = write_modules_file(modules, modules_output_path)
    console.print(
        f"  [green]✓[/green] Module list saved → [bold]{modules_path}[/bold]\n"
    )

    return modules
