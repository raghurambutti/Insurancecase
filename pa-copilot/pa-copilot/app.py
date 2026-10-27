"""Nurse review dashboard (human-in-the-loop).   Run: streamlit run app.py"""
import html
import json
from pathlib import Path

import pandas as pd
import streamlit as st

import guardrails as g
from agents import use_claude
from pipeline import RESULTS, run_all

DECISIONS = Path(__file__).parent / "outputs" / "decisions.json"
ICON = {"MET": "✅", "UNMET": "❌", "MISSING": "📄", "NEEDS_REVIEW": "⚠️"}
BANNER = {"FAST_TRACK": st.success, "PEND_FOR_INFO": st.info, "NURSE_REVIEW": st.warning,
          "MD_REVIEW": st.warning, "SECURITY_REVIEW": st.error, "INTAKE_EXCEPTION": st.error}

st.set_page_config(page_title="PA Review Copilot", layout="wide")

# ---------------------------------------------------------------- sidebar
with st.sidebar:
    st.title("PA Review Copilot")
    st.caption("Synthetic data only. AI recommends; licensed clinicians decide.")
    role = st.selectbox("Signed in as", ["Nurse reviewer", "Physician reviewer"])
    reviewer = st.text_input("Reviewer ID", "RN-0142" if role == "Nurse reviewer" else "MD-0007")
    st.write(f"Extraction mode: **{'Claude' if use_claude() else 'Offline rules'}**")
    if st.button("Run AI pipeline on all cases", type="primary"):
        with st.spinner("Running 3-agent pipeline..."):
            g.AUDIT_LOG.unlink(missing_ok=True)
            run_all()

results = {p.stem: json.loads(p.read_text()) for p in sorted(RESULTS.glob("*.json"))} if RESULTS.exists() else {}
decisions = json.loads(DECISIONS.read_text()) if DECISIONS.exists() else {}
if not results:
    st.info("No cases processed yet. Click **Run AI pipeline on all cases** in the sidebar.")
    st.stop()

queue_tab, case_tab, audit_tab, impact_tab = st.tabs(["Work queue", "Case review", "Audit trail", "Business impact"])

# ---------------------------------------------------------------- work queue
with queue_tab:
    order = list(g.ROUTES)
    rows = sorted(results.values(), key=lambda b: (order.index(b["route"]), b["sla"]["hours_left"]))
    routes = [b["route"] for b in rows]
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Cases", len(rows))
    c2.metric("Fast-track (nurse confirm)", routes.count("FAST_TRACK"))
    c3.metric("Pended at intake", routes.count("PEND_FOR_INFO"))
    c4.metric("Avg AI processing", f"{sum(b['processing_seconds'] for b in rows) / len(rows):.1f}s")
    st.dataframe(pd.DataFrame([{
        "Case": b["case_id"], "Service": b["service"], "AI route": b["route_label"],
        "Urgency": b["sla"]["urgency"], "Hours to deadline": b["sla"]["hours_left"],
        "Flags": ", ".join(sorted({f["code"] for f in b["flags"] if f["severity"] != "info"})),
        "Decision": decisions.get(b["case_id"], {}).get("decision", "pending")} for b in rows]),
        hide_index=True)

# ---------------------------------------------------------------- case review
with case_tab:
    case_id = st.selectbox("Case", list(results))
    b = results[case_id]
    BANNER[b["route"]](f"**{b['route_label']}**  \n" + "  \n".join(b["reasons"]))
    left, right = st.columns([3, 2])

    with left:
        st.markdown(f"**Service:** {b['service']}  \n**Policy:** {b['policy']}  \n"
                    f"**Member:** {b['member']}  \n**Provider:** {b['provider']}")
        if b["criteria"]:
            st.subheader("Policy criteria")
            for c in b["criteria"]:
                st.write(f"{ICON[c['status']]} **{c['id']}** {c['text']} - _{c['status']}_")
        for x in b["contradictions"]:
            st.error(x)

        st.subheader("Evidence (every value is cited and verified)")
        for key, f in b["facts"].items():
            if f["status"] == "not_found":
                st.write(f"📄 **{key}**: not documented")
            else:
                mark = "✅" if f["status"] == "found" else "⚠️ " + f.get("problem", "")
                st.write(f"{mark} **{key}** = `{f['value']}`  ·  {f['doc_id']} p{f.get('page') or '-'}  \n"
                         f"> {f.get('quote')}")

        st.subheader("Source documents (identifiers masked)")
        quotes = [f["quote"] for f in b["facts"].values() if f.get("quote")]
        for d in b["documents"]:
            label = f"{d['doc_id']} · {d['file']} · {d['type']}" + ("  (EXCLUDED)" if d["excluded"] else "")
            with st.expander(label):
                for p in d["pages"]:
                    text = html.escape(p["text"])
                    for q in quotes:
                        text = text.replace(html.escape(q), f"<mark>{html.escape(q)}</mark>")
                    st.markdown(f"<pre style='white-space:pre-wrap'>{text}</pre>", unsafe_allow_html=True)

    with right:
        s = b["sla"]
        st.metric("Decision due (CMS-0057-F)", s["due_at"].replace("T", " ")[:16], f"{s['hours_left']}h left · {s['urgency']}")
        for f in b["flags"]:
            {"high": st.error, "medium": st.warning, "info": st.caption}[f["severity"]](f"{f['code']}: {f['message']}")

        letter = st.text_area("Provider letter (editable)", b["provider_letter"], height=220) if b["provider_letter"] else ""

        st.subheader("Clinical decision")
        options = ["Approve", "Pend - send letter to provider", "Escalate to physician"]
        if role == "Physician reviewer":
            options.append("Deny")  # adverse determinations: physician only
        decision = st.radio("Decision", options)
        unmet = [c["id"] for c in b["criteria"] if c["status"] != "MET"]
        deny_basis = st.multiselect("Denial basis (specific criteria)", unmet) if decision == "Deny" else []
        security_ok = st.checkbox("I reviewed the quarantined content") if b["route"] == "SECURITY_REVIEW" else True
        agree = st.radio("Was the AI assessment accurate?", ["Yes", "Partly", "No"], horizontal=True)
        note = st.text_area("Rationale (required)")

        if st.button("Submit decision", type="primary"):
            if not note.strip():
                st.error("Rationale is required.")
            elif decision == "Deny" and not deny_basis:
                st.error("A denial must cite the specific unmet criteria (CMS-0057-F specific-reason requirement).")
            elif not security_ok:
                st.error("Confirm you reviewed the quarantined content first.")
            else:
                record = {"decision": decision, "reviewer": f"{role}:{reviewer}", "ai_route": b["route"],
                          "ai_accurate": agree, "deny_basis": deny_basis, "letter_sent": bool(letter) and decision.startswith("Pend")}
                decisions[case_id] = record
                DECISIONS.write_text(json.dumps(decisions, indent=2))
                g.audit(case_id, "HUMAN_DECISION", record["reviewer"], {**record, "rationale": note})
                st.success(f"Recorded: {decision}")

# ---------------------------------------------------------------- audit
with audit_tab:
    st.write("Chain intact ✅" if g.verify_audit_chain() else "⚠️ Audit chain broken - possible tampering")
    events = [json.loads(l) for l in g.AUDIT_LOG.read_text().splitlines()] if g.AUDIT_LOG.exists() else []
    pick = st.selectbox("Filter by case", ["All"] + list(results), key="audit_case")
    st.dataframe(pd.DataFrame([{"time": e["ts"], "case": e["case_id"], "event": e["event"], "actor": e["actor"],
                                "details": json.dumps(e["details"]), "hash": e["hash"][:12]}
                               for e in events if pick in ("All", e["case_id"])]), hide_index=True)

# ---------------------------------------------------------------- business impact
with impact_tab:
    st.caption("Simple scenario model. Assumptions are adjustable and should be validated with the plan's own data.")
    a, b2 = st.columns(2)
    volume = a.number_input("Requests per month", value=15000, step=500)
    base_tat = a.number_input("Current turnaround (days)", value=4.2)
    rework = a.slider("Current rework rate", 0.0, 0.5, 0.22)
    rework_days = a.slider("Days added per rework loop", 1.0, 6.0, 3.0)
    fast = b2.slider("Share fast-tracked (same-day nurse confirm)", 0.0, 0.6, 0.25)
    rework_cut = b2.slider("Rework avoided by intake completeness check", 0.0, 0.9, 0.5)
    review_cut = b2.slider("Review time saved by cited brief", 0.0, 0.6, 0.30)
    mins = b2.slider("Nurse minutes per case today", 10, 60, 30)

    core = base_tat - rework * rework_days
    new_tat = fast * 0.5 + (1 - fast) * core * (1 - review_cut) + rework * (1 - rework_cut) * rework_days
    hours_saved = volume * mins * (fast * 0.8 + (1 - fast) * review_cut) / 60
    m1, m2, m3 = st.columns(3)
    m1.metric("Projected turnaround", f"{new_tat:.1f} days", f"{(new_tat - base_tat) / base_tat:.0%}")
    m2.metric("Rework rate", f"{rework * (1 - rework_cut):.0%}", f"{-rework * rework_cut:.0%}")
    m3.metric("Nurse hours freed / month", f"{hours_saved:,.0f}", f"≈{hours_saved / 140:.1f} FTE")
    st.bar_chart(pd.DataFrame({"days": [base_tat, new_tat]}, index=["Today", "With AI"]))
