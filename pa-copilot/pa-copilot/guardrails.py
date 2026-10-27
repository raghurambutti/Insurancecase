"""Safety & compliance controls used by the agents.

1. Prompt-injection firewall  - quarantine instruction-like text before any LLM sees it
2. PHI minimisation           - pseudonymise identifiers for the LLM, redact the audit log,
                                block outbound letters that leak other people's data
3. Citation grounding         - every fact must quote the source page verbatim (no hallucinations)
4. Deterministic routing      - code decides the route; the AI can never deny
5. Regulatory clock           - CMS-0057-F: 72h expedited / 7 days standard
6. Audit log                  - append-only, hash-chained, PHI-redacted
"""
import hashlib
import json
import re
import unicodedata
from datetime import datetime, timedelta, timezone
from pathlib import Path

# ---------------------------------------------------------------- 1. injection
HIDDEN_CHARS = re.compile("[​-‏‪-‮⁠-⁤﻿]")
INJECTION_PATTERNS = [
    r"\b(?:ignore|disregard|forget)\b[^.\n]{0,40}?\b(?:previous|prior|above|all)\b[^.\n]{0,20}?\binstructions?\b",
    r"\bauto(?:matically|-)?\s*approve\b",
    r"\b(?:mark|report)\s+(?:all\s+)?criteria\s+(?:as\s+)?met\b",
    r"\bset\s+(?:the\s+)?(?:decision|status)\s*(?:=|to)",
    r"^\s*(?:system|assistant)\s*:",
    r"\b(?:you are now|new instructions|developer mode)\b",
    r"\b(?:note|attention|message)\s+(?:to|for)\s+(?:the\s+)?(?:ai|automated|llm|model)\b",
]
INJECTION_RE = [re.compile(p, re.I | re.M) for p in INJECTION_PATTERNS]
REMOVED = "[REMOVED BY SAFETY FILTER: suspected instruction injection]"


def sanitize(text: str) -> tuple[str, int, int]:
    """Strip hidden characters and quarantine injection lines.
    Returns (clean_text, hidden_char_count, injected_line_count)."""
    hidden = len(HIDDEN_CHARS.findall(text))
    text = unicodedata.normalize("NFKC", HIDDEN_CHARS.sub("", text))
    out, hits = [], 0
    for line in text.splitlines():
        if any(p.search(line) for p in INJECTION_RE):
            out.append(REMOVED)
            hits += 1
        else:
            out.append(line)
    return "\n".join(out), hidden, hits


# ---------------------------------------------------------------- 2. PHI
SSN = re.compile(r"\b\d{3}-\d{2}-\d{4}\b")
PHONE = re.compile(r"\(?\b\d{3}\)?[-.\s]\d{3}[-.\s]\d{4}\b")
MEMBER_ID = re.compile(r"\bM\d{6}\b")


def pseudonymize(text: str, member: dict | None) -> str:
    """Replace direct identifiers with tokens. Clinical dates stay (needed for durations)."""
    if member:
        for tok in member["name"].split():
            text = re.sub(rf"\b{re.escape(tok)}\b", "[PATIENT]", text, flags=re.I)
        text = text.replace(member["dob"], "[DOB]")
    text = SSN.sub("[SSN]", text)
    text = PHONE.sub("[PHONE]", text)
    return MEMBER_ID.sub("[MEMBER_ID]", text)


def has_extra_identifiers(text: str) -> list[str]:
    return [name for name, pat in (("SSN", SSN), ("phone", PHONE)) if pat.search(text)]


def mask_id(member_id: str) -> str:
    return "*" * (len(member_id) - 4) + member_id[-4:]


def egress_check(letter: str, foreign_names: list[str]) -> list[str]:
    """Outbound letters may only contain this member's minimum-necessary data."""
    problems = [f"{n} in letter" for n, p in (("SSN", SSN), ("Phone", PHONE)) if p.search(letter)]
    problems += ["Another patient's name in letter" for n in foreign_names if n.lower() in letter.lower()]
    return problems


# ---------------------------------------------------------------- 3. grounding
def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", unicodedata.normalize("NFKC", s or "")).strip().lower()


def verify_citation(fact: dict, spec: dict, page_text: str | None) -> str | None:
    """Return None if grounded, else the reason it failed."""
    if page_text is None:
        return "cited page does not exist"
    quote = _norm(fact.get("quote"))
    if len(quote) < 3 or quote not in _norm(page_text):
        return "quote not found verbatim on cited page"
    value = fact.get("value")
    if spec["type"] in ("number", "date", "string", "enum") and value is not None:
        tokens = [str(value), str(value).replace("_", " ")]
        if isinstance(value, float) and value.is_integer():
            tokens.append(str(int(value)))
        tokens += [k for k, v in spec.get("map", {}).items() if v == value]
        if not any(_norm(t) in quote for t in tokens):
            return "value does not appear in the quote"
    return None


# ---------------------------------------------------------------- 4. routing
ROUTES = {
    "FAST_TRACK": "Approval recommended - nurse confirms",
    "NURSE_REVIEW": "Nurse clinical review",
    "PEND_FOR_INFO": "Pend - missing info requested from provider",
    "MD_REVIEW": "Physician review - criteria not met (AI cannot deny)",
    "SECURITY_REVIEW": "Security hold - prompt injection",
    "INTAKE_EXCEPTION": "Intake exception - file unreadable",
}


def decide_route(intake_ok, flags, criteria, contradictions, missing) -> tuple[str, list]:
    """First match wins. There is no DENY route: denials are a physician's decision in the UI."""
    high = [f["message"] for f in flags if f["severity"] == "high"]
    medium = [f["message"] for f in flags if f["severity"] == "medium"]
    unmet = [f"{c['id']} not met: {c['text']}" for c in criteria if c["status"] == "UNMET"]
    unverified = [f"{c['id']}: evidence could not be verified" for c in criteria if c["status"] == "NEEDS_REVIEW"]

    if not intake_ok:
        route, reasons = "INTAKE_EXCEPTION", high
    elif any(f["code"] == "PROMPT_INJECTION" for f in flags):
        route, reasons = "SECURITY_REVIEW", ["Instruction-like text aimed at the AI was found and quarantined"]
    elif unmet:
        route, reasons = "MD_REVIEW", unmet + ["Possible denial: only a physician reviewer may deny"]
    elif contradictions or high or unverified:
        route, reasons = "NURSE_REVIEW", contradictions + high + unverified
    elif missing:
        route, reasons = "PEND_FOR_INFO", [f"Missing: {m}" for m in missing]
    elif medium:
        route, reasons = "NURSE_REVIEW", medium
    else:
        route, reasons = "FAST_TRACK", ["All criteria met with verified citations and no safety flags"]
    assert route in ROUTES  # invariant: the pipeline cannot emit anything else (e.g. DENY)
    return route, reasons


# ---------------------------------------------------------------- 5. regulatory clock
def sla(received_at: str, urgency: str) -> dict:
    hours = 72 if urgency == "expedited" else 168
    due = datetime.fromisoformat(received_at) + timedelta(hours=hours)
    left = (due - datetime.now(timezone.utc)).total_seconds() / 3600
    return {"urgency": urgency, "due_at": due.isoformat(timespec="minutes"), "hours_left": round(left, 1),
            "status": "breached" if left <= 0 else ("at_risk" if left < hours * 0.25 else "on_track")}


# ---------------------------------------------------------------- 6. audit log
AUDIT_LOG = Path(__file__).parent / "outputs" / "audit_log.jsonl"


def _hash(entry: dict) -> str:
    return hashlib.sha256(json.dumps({k: v for k, v in entry.items() if k != "hash"},
                                     sort_keys=True, default=str).encode()).hexdigest()


def audit(case_id: str, event: str, actor: str, details: dict, member: dict | None = None):
    """Append-only, hash-chained, PHI-redacted. Records what happened and why, never clinical text."""
    AUDIT_LOG.parent.mkdir(exist_ok=True)
    lines = AUDIT_LOG.read_text().splitlines() if AUDIT_LOG.exists() else []
    entry = {"ts": datetime.now(timezone.utc).isoformat(timespec="seconds"), "case_id": case_id,
             "event": event, "actor": actor,
             "details": json.loads(pseudonymize(json.dumps(details, default=str), member)),
             "prev_hash": json.loads(lines[-1])["hash"] if lines else "0" * 64}
    entry["hash"] = _hash(entry)
    with AUDIT_LOG.open("a") as fh:
        fh.write(json.dumps(entry) + "\n")


def verify_audit_chain() -> bool:
    prev = "0" * 64
    for line in (AUDIT_LOG.read_text().splitlines() if AUDIT_LOG.exists() else []):
        e = json.loads(line)
        if e["prev_hash"] != prev or _hash(e) != e["hash"]:
            return False
        prev = e["hash"]
    return True
