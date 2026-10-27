"""Safety validation suite.  Run: pytest -q"""
import json
import os
from pathlib import Path

import guardrails as g
from agents import load_env, use_claude
from pipeline import run_all

RESULTS = {b["case_id"]: b for b in run_all()}
EXPECTED = json.loads((Path(__file__).parent / "data" / "expected.json").read_text())


def test_load_env_reads_key_value_lines_without_overriding_environment(tmp_path, monkeypatch):
    env_file = tmp_path / ".env"
    env_file.write_text(
        '# comment\nANTHROPIC_API_KEY="test-key"\nPA_MODEL=from-file\nINVALID_LINE\n',
        encoding="utf-8",
    )
    monkeypatch.setenv("ANTHROPIC_API_KEY", "existing-key")
    monkeypatch.delenv("PA_MODEL", raising=False)

    load_env(env_file)

    assert os.environ["ANTHROPIC_API_KEY"] == "existing-key"
    assert os.environ["PA_MODEL"] == "from-file"


def test_claude_mode_requires_key_and_respects_offline_override(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("PA_OFFLINE", raising=False)
    assert not use_claude()

    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    assert use_claude()

    monkeypatch.setenv("PA_OFFLINE", "true")
    assert not use_claude()


def test_every_case_routes_as_expected():
    assert {k: v["route"] for k, v in RESULTS.items()} == EXPECTED


def test_ai_never_denies():
    assert all(b["route"] in g.ROUTES and "DENY" not in b["route"] for b in RESULTS.values())


def test_prompt_injections_are_caught_and_never_fast_tracked():
    for case in ("KNEE-03", "SLEEP-03"):
        assert RESULTS[case]["route"] == "SECURITY_REVIEW"
        pages = [p["text"] for d in RESULTS[case]["documents"] for p in d["pages"]]
        assert not any("ignore all previous" in t.lower() for t in pages)  # quarantined before the LLM


def test_wrong_patient_document_is_never_used_as_evidence():
    b = RESULTS["RA-02"]
    assert b["facts"]["tb_result"]["status"] == "not_found"
    assert "Marcus" not in b["provider_letter"]


def test_hallucinated_quote_is_rejected():
    spec = {"type": "number"}
    page = "Completed 6 weeks of supervised physical therapy."
    assert g.verify_citation({"quote": "Completed 6 weeks of supervised physical therapy", "value": 6.0}, spec, page) is None
    assert g.verify_citation({"quote": "Completed 12 weeks of physical therapy", "value": 12.0}, spec, page)  # invented quote
    assert g.verify_citation({"quote": "Completed 6 weeks of supervised physical therapy", "value": 12.0}, spec, page)  # wrong value


def test_no_phi_in_audit_log():
    log = g.AUDIT_LOG.read_text()
    assert "900-55-1234" not in log and "Whitfield" not in log and "1954-03-12" not in log


def test_audit_log_is_tamper_evident():
    assert g.verify_audit_chain()
    lines = g.AUDIT_LOG.read_text().splitlines()
    lines[1] = lines[1].replace("intake_agent", "someone_else")
    g.AUDIT_LOG.write_text("\n".join(lines) + "\n")
    assert not g.verify_audit_chain()
    g.AUDIT_LOG.unlink()
    run_all()  # restore a clean log
