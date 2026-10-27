r"""LangGraph wiring + batch runner.

    START -> intake_agent --(file ok)--> clinical_agent -> policy_agent -> END
                          \--(file unreadable)---------> policy_agent

Run:  python pipeline.py      (processes every case in data/cases and checks expected routes)
"""
import json
import time
from pathlib import Path
from typing import TypedDict

from langgraph.graph import END, START, StateGraph

import guardrails as g
from agents import clinical_agent, intake_agent, policy_agent, use_claude

ROOT = Path(__file__).parent
RESULTS = ROOT / "outputs" / "results"


class PAState(TypedDict, total=False):
    case_dir: str
    case_id: str
    manifest: dict
    member: dict
    provider: dict
    policy: dict
    documents: list
    flags: list
    intake_ok: bool
    sla: dict
    facts: dict
    extraction_mode: str
    criteria: list
    contradictions: list
    missing_info: list
    route: str
    brief: dict


def build_graph():
    graph = StateGraph(PAState)
    graph.add_node("intake_agent", intake_agent)
    graph.add_node("clinical_agent", clinical_agent)
    graph.add_node("policy_agent", policy_agent)
    graph.add_edge(START, "intake_agent")
    graph.add_conditional_edges("intake_agent", lambda s: "ok" if s["intake_ok"] else "exception",
                                {"ok": "clinical_agent", "exception": "policy_agent"})
    graph.add_edge("clinical_agent", "policy_agent")
    graph.add_edge("policy_agent", END)
    return graph.compile()


APP = build_graph()


def run_case(case_dir: Path) -> dict:
    start = time.time()
    state = APP.invoke({"case_dir": str(case_dir)})
    brief = {**state["brief"], "processing_seconds": round(time.time() - start, 2),
             "documents": [{k: v for k, v in d.items()} for d in state["documents"]]}
    RESULTS.mkdir(parents=True, exist_ok=True)
    (RESULTS / f"{brief['case_id']}.json").write_text(json.dumps(brief, indent=2, default=str))
    return brief


def run_all() -> list:
    return [run_case(d) for d in sorted((ROOT / "data" / "cases").iterdir()) if d.is_dir()]


if __name__ == "__main__":
    g.AUDIT_LOG.unlink(missing_ok=True)  # fresh audit trail for each demo run
    expected = json.loads((ROOT / "data" / "expected.json").read_text())
    print(f"Mode: {'Claude' if use_claude() else 'offline (no API key)'}\n")
    print(f"{'CASE':<10} {'ROUTE':<17} {'EXPECTED':<17} {'OK':<4} FLAGS")
    passed = 0
    for b in run_all():
        exp = expected.get(b["case_id"], "?")
        ok = b["route"] == exp
        passed += ok
        codes = sorted({f["code"] for f in b["flags"]})
        print(f"{b['case_id']:<10} {b['route']:<17} {exp:<17} {'yes' if ok else 'NO':<4} {', '.join(codes)}")
    print(f"\nRoute accuracy: {passed}/{len(expected)}")
    print(f"Audit chain intact: {g.verify_audit_chain()}")
