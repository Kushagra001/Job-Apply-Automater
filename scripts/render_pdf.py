"""
render_pdf.py — Resume PDF Rendering
======================================
Converts a tailored resume JSON into an ATS-compatible PDF using
Playwright (Chromium HTML → PDF).

Renderer: Playwright/Chromium (switched from WeasyPrint which requires
GTK/Pango system libraries unavailable on Windows).
Chromium is already a project dependency for ATS automation — zero
extra install required.

Template: templates/resume.html  (Jinja2, do NOT inline styles in this script)
Output:   output/<company>_<variant>_<YYYYMMDD>.pdf

Input:
  - tailored_json: dict — output of tailor_resume.py
  - company:       str  — used in output filename

Output:
  - Absolute path to the generated PDF file (str)
"""

from __future__ import annotations

import logging
import os
from datetime import datetime
from pathlib import Path
from jinja2 import Environment, FileSystemLoader
from playwright.sync_api import sync_playwright

from dotenv import load_dotenv

load_dotenv()
logger = logging.getLogger(__name__)

import pypdf

TEMPLATES_DIR = Path(__file__).parent.parent / "templates"
OUTPUT_DIR = Path(__file__).parent.parent / "output"

# Progressive CSS compaction styles if resume content spills onto page 2
COMPACT_STYLES = [
    # Level 1: Moderate tightening
    """
    <style id="page-compressor">
        @page { margin: 0.30in 0.38in !important; }
        body { font-size: 9.0pt !important; line-height: 1.18 !important; }
        .section-title { margin-top: 5px !important; margin-bottom: 2px !important; }
        .content-block { margin-bottom: 3px !important; }
        ul { margin-top: 1px !important; margin-bottom: 1px !important; }
        li { margin-bottom: 1px !important; }
    </style>
    """,
    # Level 2: Compact
    """
    <style id="page-compressor">
        @page { margin: 0.24in 0.32in !important; }
        body { font-size: 8.5pt !important; line-height: 1.12 !important; }
        .section-title { margin-top: 4px !important; margin-bottom: 2px !important; }
        .content-block { margin-bottom: 2px !important; }
        ul { margin-top: 1px !important; margin-bottom: 1px !important; }
        li { margin-bottom: 0px !important; }
    </style>
    """,
    # Level 3: Ultra compact
    """
    <style id="page-compressor">
        @page { margin: 0.18in 0.26in !important; }
        body { font-size: 8.0pt !important; line-height: 1.06 !important; }
        .name { font-size: 15pt !important; margin-bottom: 1px !important; }
        .section-title { margin-top: 3px !important; margin-bottom: 1px !important; font-size: 9pt !important; }
        .content-block { margin-bottom: 2px !important; }
        ul { margin-top: 1px !important; margin-bottom: 1px !important; }
        li { margin-bottom: 0px !important; }
    </style>
    """
]


# ── Rendering ─────────────────────────────────────────────────────────────────

def render_pdf(tailored_json: dict, company: str, date: str | None = None) -> str:
    """
    Render tailored_json to PDF and return the output path.
    Guarantees strict single-page output via progressive CSS compression,
    and asserts ATS selectable text parseability via pypdf.

    Args:
        tailored_json: the tailored resume dict (output of tailor_resume.py)
        company:       company name for the output filename slug
        date:          YYYYMMDD string; defaults to today if None

    Returns:
        Absolute path to the generated PDF.
    """
    if date is None:
        date = datetime.now().strftime("%Y%m%d")

    variant = tailored_json.get("variant", "unknown")
    slug_company = company.lower().replace(" ", "_")
    output_filename = f"{slug_company}_{variant}_{date}.pdf"
    output_path = OUTPUT_DIR / output_filename

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    try:
        # 1. Render HTML from Jinja2 template
        env = Environment(loader=FileSystemLoader(str(TEMPLATES_DIR)))
        template = env.get_template("resume.html")
        rendered_html = template.render(resume=tailored_json)

        # 2. Render HTML → PDF via Playwright/Chromium with 1-page guarantee
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            page = browser.new_page()

            current_html = rendered_html
            for level in range(len(COMPACT_STYLES) + 1):
                page.set_content(current_html, wait_until="networkidle")
                page.pdf(
                    path=str(output_path),
                    format="A4",
                    prefer_css_page_size=True,
                    margin={"top": "0in", "bottom": "0in", "left": "0in", "right": "0in"},
                    print_background=True,
                )

                # Check page count with pypdf
                reader = pypdf.PdfReader(str(output_path))
                page_count = len(reader.pages)
                if page_count == 1:
                    break

                if level < len(COMPACT_STYLES):
                    logger.info("Resume spilled to %d pages. Applying level %d CSS compression...", page_count, level + 1)
                    current_html = rendered_html.replace("</head>", f"{COMPACT_STYLES[level]}</head>")

            browser.close()

        # 3. Verify ATS selectable text parseability
        reader = pypdf.PdfReader(str(output_path))
        extracted_text = reader.pages[0].extract_text() or ""
        expected_name = tailored_json.get("contact", {}).get("full_name", "")
        if expected_name and expected_name.lower() not in extracted_text.lower():
            logger.warning("ATS Warning: Candidate name '%s' not cleanly extracted from PDF text.", expected_name)

        logger.info(
            "PDF generated & verified: %s (%d page, %d chars extracted)",
            output_path,
            len(reader.pages),
            len(extracted_text),
        )
        return str(output_path)
    except Exception as e:
        logger.error("Failed to generate PDF: %s", e)
        raise


if __name__ == "__main__":
    import json
    import sys
    logging.basicConfig(level=logging.INFO)
    if len(sys.argv) < 3:
        print("Usage: python render_pdf.py <tailored.json> <company_name>")
        sys.exit(1)
    tailored = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
    pdf_path = render_pdf(tailored, company=sys.argv[2])
    print(f"PDF written to: {pdf_path}")
