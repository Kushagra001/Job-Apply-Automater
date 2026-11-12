"""
outreach.py — Direct Founder / Hiring-Manager Cold Outreach Drafts
=================================================================
Generates concise, proof-of-work pitch emails for high-fit applications and
saves them as review-ready drafts (locally under output/outreach/ and, when
GMAIL_USER + GMAIL_APP_PASSWORD are configured, directly in [Gmail]/Drafts
with the tailored resume PDF attached).

Uses stdlib (imaplib, email) + existing repo dependencies only.
Never auto-sends emails without human review and never logs credentials.
"""

from __future__ import annotations

from datetime import datetime, timezone
from email.message import EmailMessage
import imaplib
import json
import logging
import os
from pathlib import Path
import re
import time
from typing import Any
from urllib.parse import urlparse
import requests
from dotenv import load_dotenv

load_dotenv()
logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
OUTREACH_OUTPUT_DIR = PROJECT_ROOT / "output" / "outreach"

_EXCLUDED_WEB_DOMAINS = (
    "greenhouse.io", "lever.co", "ashbyhq.com", "remotive.com", "remoteok.com",
    "arbeitnow.com", "arbeitnow.co.uk", "jobicy.com", "himalayas.app",
    "linkedin.com", "github.com", "twitter.com", "x.com", "youtube.com",
    "ycombinator.com", "news.ycombinator.com", "google.com", "w3.org",
)
_GENERIC_EMAIL_PREFIXES = (
    "no-reply", "noreply", "donotreply", "do-not-reply", "privacy", "legal",
    "support", "abuse", "dmca", "security", "unsubscribe", "billing",
)
_GENERIC_EMAIL_DOMAINS = (
    "example.com", "greenhouse.io", "lever.co", "ashbyhq.com",
    "sentry.io", "w3.org", "github.com",
)
_STATIC_ASSET_TLDS = frozenset({
    "png", "jpg", "jpeg", "gif", "svg", "webp", "ico", "bmp", "avif",
    "woff", "woff2", "ttf", "eot", "otf",
    "css", "js", "mjs", "map", "json", "xml", "pdf", "mp4", "webm", "mp3", "wav",
})


def _slug(text: str, max_len: int = 45) -> str:
    s = re.sub(r"[^a-z0-9_-]+", "_", (text or "").lower()).strip("_-")
    return s[:max_len].strip("_-") or "company"


def _extract_emails_from_text(text: str) -> list[str]:
    """Extract clean public contact/hiring/founder emails from arbitrary text."""
    if not text:
        return []
    found = re.findall(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}", text)
    out: list[str] = []
    seen: set[str] = set()
    for em in found:
        em_clean = em.strip(".,;:)>\"'").lower()
        local, _, dom = em_clean.partition("@")
        if not local or not dom:
            continue
        tld = dom.rsplit(".", 1)[-1].lower()
        if tld in _STATIC_ASSET_TLDS:
            continue
        if dom[0].isdigit() and any(ch.isdigit() for ch in dom):
            # Reject versioned frontend bundles / retina sizes like logo@2x.png or bundle@1.0.min.js
            if "min." in dom or re.match(r"^\d+(?:x|\.\d+)", dom):
                continue
        if any(local.startswith(p) for p in _GENERIC_EMAIL_PREFIXES):
            continue
        if any(dom == gd or dom.endswith("." + gd) for gd in _GENERIC_EMAIL_DOMAINS):
            continue
        if em_clean not in seen:
            seen.add(em_clean)
            out.append(em_clean)
    return out


def _extract_company_homepage(jd: dict) -> str:
    """Heuristically locate a company homepage URL from JD description (ignoring ATS/aggregator links)."""
    desc = jd.get("description", "") or ""
    for raw_url in re.findall(r"https?://[^\s<>\"')]+", desc):
        clean = raw_url.rstrip(".,;:!?")
        host = (urlparse(clean).netloc or "").lower()
        if not host:
            continue
        if any(bad in host for bad in _EXCLUDED_WEB_DOMAINS):
            continue
        scheme = urlparse(clean).scheme or "https"
        return f"{scheme}://{host}"
    return ""


def discover_contact_emails(jd: dict, probe_website: bool = True) -> list[str]:
    """
    Discover direct hiring/founder email addresses for a JD in priority order:
      1. jd['contact_emails'] (already extracted during discovery, e.g. HackerNews)
      2. Emails embedded inside jd['description']
      3. Optional lightweight fetch of the company homepage + /careers or /contact
    """
    emails: list[str] = []
    seen: set[str] = set()

    def _add_all(candidates: list[str]) -> None:
        for c in candidates:
            cl = str(c).strip().lower()
            if cl and cl not in seen:
                seen.add(cl)
                emails.append(cl)

    _add_all(jd.get("contact_emails") or [])
    _add_all(_extract_emails_from_text(jd.get("description", "") or ""))

    if emails or not probe_website:
        return emails

    if jd.get("source") in ("yc_startups", "ashby", "lever", "greenhouse", "hackernews"):
        homepage = _extract_company_homepage(jd)
        if homepage:
            for path in ("", "/careers", "/contact"):
                try:
                    resp = requests.get(
                        homepage.rstrip("/") + path,
                        headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"},
                        timeout=4,
                    )
                    if resp.status_code == 200 and resp.text:
                        found = _extract_emails_from_text(resp.text[:50_000])
                        _add_all(found)
                        if emails:
                            break
                except Exception:
                    continue

    return emails


def should_draft_outreach(jd: dict, score: int) -> bool:
    """Return True if this application qualifies for direct founder/recruiter outreach drafting."""
    if os.environ.get("ENABLE_OUTREACH", "true").lower() not in ("true", "1", "yes"):
        return False
    try:
        threshold = int(os.environ.get("OUTREACH_SCORE_THRESHOLD", "72"))
    except ValueError:
        threshold = 72
    if score >= threshold:
        return True
    if jd.get("contact_emails"):
        return True
    if jd.get("source") in ("hackernews", "yc_startups") and score >= 65:
        return True
    return False


def _pick_relevant_project(variant_json: dict) -> tuple[str, str]:
    """Return (project_name, highlight_line) from variant_json projects without inventing anything."""
    projects = variant_json.get("projects") or []
    if projects and isinstance(projects[0], dict):
        p = projects[0]
        name = str(p.get("name", "Stackform"))
        bullets = p.get("bullets") or []
        highlight = str(bullets[0]) if bullets else f"built with {p.get('stack', 'modern web & AI stack')}"
        return name, highlight
    return "BidCopy & Stackform", "built full-stack web apps and LLM-powered automation systems"


def _deterministic_outreach_pitch(jd: dict, variant_json: dict) -> dict[str, str]:
    """Fallback concise cold outreach email using strictly verified resume facts."""
    title = jd.get("title", "Software Engineer").strip()
    company = jd.get("company", "your team").strip()
    proj_name, proj_highlight = _pick_relevant_project(variant_json)
    skills = ", ".join((variant_json.get("skills") or [])[:5])
    contact = variant_json.get("contact") or {}
    github = contact.get("github", "https://github.com/Kushagra001")
    portfolio = contact.get("portfolio", "https://www.stack-form.dev/")
    linkedin = contact.get("linkedin", "https://www.linkedin.com/in/kushh01")

    is_remote_global = bool(jd.get("remote")) and "india" not in (jd.get("location") or "").lower()
    geo_line = (
        "Based in India with strong async documentation habits, flexible IST overlap, and immediate contractor/EOR readiness."
        if is_remote_global
        else "Based in India and available to join immediately."
    )

    subject = f"{title} at {company} — Kushagra Singh Negi ({proj_name} / {skills.split(',')[0].strip() if skills else 'Full-Stack'})"
    body = (
        f"Hi {company} team,\n\n"
        f"I just applied for the {title} role and wanted to share a direct note along with my tailored resume (attached).\n\n"
        f"Most recently I shipped production features across {skills}, and built {proj_name}: {proj_highlight}\n\n"
        f"{geo_line}\n\n"
        f"Links: GitHub ({github}) | Portfolio ({portfolio}) | LinkedIn ({linkedin})\n\n"
        f"Best,\nKushagra Singh Negi"
    )
    return {"subject": subject[:140], "body": body}


def generate_outreach_pitch(jd: dict, variant_json: dict, zero_llm_mode: bool = False) -> dict[str, str]:
    """
    Generate a concise (<125 words) proof-of-work outreach subject & body using Groq,
    falling back deterministically when zero_llm_mode=True or API fails.
    """
    if zero_llm_mode or not os.environ.get("GROQ_API_KEY"):
        return _deterministic_outreach_pitch(jd, variant_json)

    try:
        from groq import Groq
        try:
            from scripts.score_and_pick import FALLBACK_MODELS, _clean_llm_json
        except Exception:
            FALLBACK_MODELS = ["openai/gpt-oss-20b", "qwen/qwen3.8-27b", "openai/gpt-oss-120b"]
            _clean_llm_json = lambda s: (s or "{}").strip()

        is_remote_global = bool(jd.get("remote")) and "india" not in (jd.get("location") or "").lower()
        geo_rule = (
            "Briefly mention remote collaboration readiness from India (flexible overlap + contractor/EOR ready, zero visa barrier)."
            if is_remote_global
            else "Candidate is based in Jaipur, India and available immediately."
        )

        projects = variant_json.get("projects") or []
        summary_context = {
            "variant": variant_json.get("variant", ""),
            "skills": (variant_json.get("skills") or [])[:10],
            "experience": [
                {"company": e.get("company"), "role": e.get("role"), "bullet": (e.get("bullets") or [""])[0]}
                for e in (variant_json.get("experience") or [])[:2]
            ],
            "projects": [
                {"name": p.get("name"), "stack": p.get("stack"), "bullet": (p.get("bullets") or [""])[0]}
                for p in projects[:2]
            ],
            "links": {
                "github": (variant_json.get("contact") or {}).get("github", "https://github.com/Kushagra001"),
                "portfolio": (variant_json.get("contact") or {}).get("portfolio", "https://www.stack-form.dev/"),
            },
        }

        prompt = f"""
Write a short, direct cold outreach email to the founder / hiring manager after applying for this role.

Target Role: {jd.get('title', '')} at {jd.get('company', '')}
Location: {jd.get('location', '')} (Remote: {jd.get('remote', False)})
JD Excerpt: {(jd.get('description') or '')[:1200]}

Truthful Candidate Proof-of-Work:
{json.dumps(summary_context, indent=2)}

Strict Rules:
1. Body MUST be under 120 words, direct, builder-to-builder tone (NO "I hope this email finds you well", NO buzzwords).
2. Cite ONLY real projects, companies, metrics, and skills present above — never invent facts.
3. Lead with 1 specific real project/achievement matching the JD's problem space + include GitHub and Portfolio links.
4. {geo_rule}
5. Return valid JSON ONLY with exact keys: "subject" and "body".
"""
        client = Groq()
        forced = os.environ.get("GROQ_MODEL")
        models = [forced] if forced else FALLBACK_MODELS
        for m in models:
            try:
                resp = client.chat.completions.create(
                    model=m,
                    messages=[
                        {"role": "system", "content": "You output JSON only with 'subject' and 'body' strings."},
                        {"role": "user", "content": prompt},
                    ],
                    response_format={"type": "json_object"},
                    temperature=0.2,
                )
                parsed = json.loads(_clean_llm_json(resp.choices[0].message.content))
                if isinstance(parsed, list) and parsed:
                    parsed = parsed[0]
                subj = str(parsed.get("subject", "")).strip()
                body = str(parsed.get("body", "")).strip()
                if subj and body:
                    return {"subject": subj[:140], "body": body}
            except Exception as e:
                logger.debug("Outreach LLM call failed on %s: %s", m, e)
                continue
    except Exception as e:
        logger.debug("Outreach LLM error, using deterministic pitch: %s", e)

    return _deterministic_outreach_pitch(jd, variant_json)


def save_gmail_draft(
    recipient: str,
    subject: str,
    body: str,
    pdf_path: str = "",
) -> tuple[bool, str]:
    """
    Save an email draft with optional tailored PDF attachment into Gmail's Drafts folder via IMAP.
    Never sends the email automatically; respects human-in-the-loop review.
    """
    gmail_user = (os.environ.get("GMAIL_USER") or "").strip()
    gmail_pw = (os.environ.get("GMAIL_APP_PASSWORD") or "").strip()
    if not gmail_user or not gmail_pw:
        return False, "Gmail IMAP credentials not configured"
    if not recipient:
        return False, "No public contact email discovered"

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = f"Kushagra Singh Negi <{gmail_user}>"
    msg["To"] = recipient
    msg.set_content(body)

    if pdf_path:
        p = Path(pdf_path)
        if p.exists() and p.is_file():
            msg.add_attachment(
                p.read_bytes(),
                maintype="application",
                subtype="pdf",
                filename=p.name,
            )

    try:
        mail = imaplib.IMAP4_SSL("imap.gmail.com")
        try:
            mail.login(gmail_user, gmail_pw)
            status, _ = mail.append(
                "[Gmail]/Drafts",
                "\\Draft",
                imaplib.Time2Internaldate(time.time()),
                msg.as_bytes(),
            )
            if status == "OK":
                logger.info("Saved Gmail outreach draft for recipient=%s", recipient)
                return True, f"Saved draft in [Gmail]/Drafts for {recipient}"
            return False, f"IMAP append returned status {status}"
        finally:
            try:
                mail.logout()
            except Exception:
                pass
    except Exception as e:
        logger.warning("Failed saving Gmail draft for %s: %s", recipient, e)
        return False, str(e)


def create_outreach_draft(
    jd: dict,
    variant_json: dict,
    score: int,
    pdf_path: str = "",
    dry_run: bool = False,
    zero_llm_mode: bool = False,
    probe_website: bool = True,
) -> dict[str, Any]:
    """
    Orchestrate outreach preparation for a qualifying job application:
      - Discovers contact emails (if any)
      - Generates concise proof-of-work subject & body
      - Persists local draft bundle to output/outreach/
      - Saves Gmail IMAP draft when credentials & recipient exist and not dry_run
      - Records entry into log_and_notify daily Telegram digest
    """
    company = jd.get("company", "Unknown Company")
    title = jd.get("title", "Software Engineer")

    emails = discover_contact_emails(jd, probe_website=probe_website and not dry_run)
    recipient = emails[0] if emails else ""
    pitch = generate_outreach_pitch(jd, variant_json, zero_llm_mode=zero_llm_mode)

    gmail_saved = False
    gmail_note = ""
    if recipient and not dry_run:
        gmail_saved, gmail_note = save_gmail_draft(
            recipient=recipient,
            subject=pitch["subject"],
            body=pitch["body"],
            pdf_path=pdf_path,
        )

    OUTREACH_OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    date_tag = datetime.now(timezone.utc).strftime("%Y%m%d")
    artifact_path = OUTREACH_OUTPUT_DIR / f"{_slug(company)}_{date_tag}.json"
    bundle = {
        "company": company,
        "title": title,
        "score": score,
        "source": jd.get("source", ""),
        "apply_url": jd.get("apply_url", ""),
        "discovered_emails": emails,
        "primary_recipient": recipient,
        "subject": pitch["subject"],
        "body": pitch["body"],
        "pdf_path": pdf_path,
        "gmail_draft_saved": gmail_saved,
        "gmail_note": gmail_note,
        "dry_run": dry_run,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    artifact_path.write_text(json.dumps(bundle, indent=2), encoding="utf-8")

    try:
        from scripts.log_and_notify import record_outreach_for_digest
        record_outreach_for_digest(
            company=company,
            title=title,
            recipient=recipient,
            gmail_draft_saved=gmail_saved,
        )
    except Exception:
        pass

    return {
        "created": True,
        "recipient": recipient,
        "subject": pitch["subject"],
        "body": pitch["body"],
        "gmail_draft_saved": gmail_saved,
        "artifact_path": str(artifact_path),
    }

