"""
render_pdf.py — Resume PDF Rendering
======================================
Converts a tailored resume JSON into an ATS-compatible PDF using
WeasyPrint (HTML → PDF via Jinja2 template).

Template: templates/resume.html  (Jinja2, do NOT inline styles in this script)
Output:   output/<company>_<variant>_<YYYYMMDD>.pdf

Rendering approach chosen: WeasyPrint (HTML → PDF).
Do not switch approach without updating this docstring and Agents.md.

Input:
  - tailored_json: dict — output of tailor_resume.py
  - company:       str  — used in output filename
  - date:          str  — YYYYMMDD, defaults to today

Output:
  - Absolute path to the generated PDF file (str)
"""

from __future__ import annotations

import logging
import os
from datetime import datetime
from pathlib import Path
from jinja2 import Environment, FileSystemLoader
from weasyprint import HTML

from dotenv import load_dotenv

load_dotenv()
logger = logging.getLogger(__name__)

TEMPLATES_DIR = Path(__file__).parent.parent / "templates"
OUTPUT_DIR = Path(__file__).parent.parent / "output"


# ── Rendering ─────────────────────────────────────────────────────────────────

def render_pdf(tailored_json: dict, company: str, date: str | None = None) -> str:
    """
    Render tailored_json to PDF and return the output path.
    Uses templates/resume.html via Jinja2 + WeasyPrint.

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
        env = Environment(loader=FileSystemLoader(str(TEMPLATES_DIR)))
        template = env.get_template("resume.html")
        
        rendered_html = template.render(**tailored_json)
        
        HTML(string=rendered_html).write_pdf(str(output_path))
        
        logger.info("PDF generated successfully: %s", output_path)
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
    tailored = json.loads(Path(sys.argv[1]).read_text())
    pdf_path = render_pdf(tailored, company=sys.argv[2])
    print(f"PDF written to: {pdf_path}")
