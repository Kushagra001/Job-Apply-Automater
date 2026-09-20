import json
import pytest
import pypdf
from pathlib import Path
from scripts.render_pdf import render_pdf, OUTPUT_DIR


def test_render_pdf_strict_one_page_compression(tmp_path):
    # Load base resume and inflate it with additional long experience bullets
    base_resume = json.loads(Path("resumes/backend-node.json").read_text())
    inflated_resume = json.loads(json.dumps(base_resume))

    # Add extra projects to force overflow beyond 1 page
    extra_project = {
        "name": "Distributed Realtime Cache Infrastructure",
        "technologies": ["Node.js", "Redis", "Docker", "TypeScript", "gRPC"],
        "bullets": [
            "Architected high-throughput distributed memory caching service achieving sub-5ms p99 latency across 100,000 concurrent websocket connections.",
            "Designed master-replica failover mechanism with automated health checks reducing downtime to under 0.01% in production.",
            "Built comprehensive monitoring dashboards tracking hit rates, memory saturation, and network bottlenecks."
        ]
    }
    inflated_resume["projects"].append(extra_project)
    inflated_resume["projects"].append(extra_project)

    pdf_path = render_pdf(inflated_resume, company="TestCompressCo")
    reader = pypdf.PdfReader(pdf_path)

    # Must be compressed to strictly 1 page
    assert len(reader.pages) == 1
    # Must have selectable text containing candidate name
    text = reader.pages[0].extract_text()
    assert "kushagra singh negi" in text.lower()
    assert len(text) > 1000
