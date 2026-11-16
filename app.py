"""ClaimWise AI: Streamlit app (customer portal + employee dashboard + audit log).

    streamlit run app.py
"""
from __future__ import annotations
from pathlib import Path

import pandas as pd
import streamlit as st

from claimwise import config
from claimwise.db import Database
from claimwise.orchestrator import CUSTOMER_STATUS, Orchestrator, ValidationError
from claimwise.policy_service import PolicyService
from claimwise.quality import check_image_quality
from claimwise.report import build_report, to_markdown, to_pdf
from claimwise.schemas import (DOC_LABELS, LANE_PRIORITY, LANE_TEXT, REQUIRED_DOCS, SEVERITY_ORDER)

st.set_page_config(page_title="ClaimWise AI", page_icon="🛡️", layout="wide")

LANE_ICON = {"MANUAL_REVIEW": "🔴", "EXCEPTION_QUEUE": "🟠", "FAST_LANE": "🟢"}
STATUS_ICON = {"MATCH": "✅", "MISMATCH": "❌", "REVIEW": "⚠️", "MISSING": "📄", "CANDIDATE": "🔁"}


@st.cache_resource
def services():
    db = Database(config.DB_PATH)
    for u in config.DEMO_USERS:
        if not db.check_login(u, config.DEMO_PASSWORD):
            db.add_user(u, config.DEMO_PASSWORD, "employee")
    orch = Orchestrator(db, PolicyService(path=config.CARRIER_DATA, api_url=config.POLICY_API_URL))
    return db, orch


db, orch = services()


# ----------------------------------------------------------------------------- helpers
def sample_cases():
    root = config.SAMPLES_DIR
    return sorted(p for p in root.iterdir() if p.is_dir()) if root.exists() else []


def load_sample(folder: Path) -> dict:
    files = {}
    for doc in REQUIRED_DOCS:
        for ext in config.ALLOWED_EXT:
            f = folder / f"{doc}{ext}"
            if f.exists():
                files[doc] = (f.name, f.read_bytes())
    return files


def read_uploads(uploads: dict) -> dict:
    return {doc: (f.name, f.getvalue()) for doc, f in uploads.items() if f is not None}


# ----------------------------------------------------------------------------- customer portal
def page_customer():
    st.title("🛡️ ClaimWise AI: Customer portal")
    st.caption("Upload your claim documents and track your claim. Demo uses synthetic documents only. "
               "Your claim is always decided by a person from our claims team.")
    tab_submit, tab_track = st.tabs(["Submit a claim", "Track a claim"])

    with tab_submit:
        st.subheader("Upload documents")
        st.caption("Each photo is checked as soon as you select it, so you can retake a blurry or dark photo before submitting.")
        cols = st.columns(2)
        uploads, poor = {}, []
        for i, doc in enumerate(REQUIRED_DOCS):
            with cols[i % 2]:
                f = st.file_uploader(DOC_LABELS[doc], type=[e.strip(".") for e in config.ALLOWED_EXT], key=f"up_{doc}")
                uploads[doc] = f
                if f is not None:
                    q = check_image_quality(f.getvalue(), f.name)
                    if q.ok:
                        st.success("Photo looks clear.")
                    else:
                        poor.append(DOC_LABELS[doc])
                        st.warning(f"This photo looks {q.summary}. Tip: {q.advice}.")
        ref = st.text_input("Your name or reference (optional)")
        consent = st.checkbox("I consent to my documents being processed for this claim (synthetic data only in this demo).")
        override = st.checkbox("Submit anyway, even though some photos look unclear.") if poor else False
        if st.button("Submit claim", type="primary"):
            files = read_uploads(uploads)
            if not consent:
                st.error("Please confirm consent to continue.")
            elif not files:
                st.error("Please upload at least one document.")
            elif poor and not override:
                st.error(f"Please retake: {', '.join(poor)}. Or tick 'Submit anyway'.")
            else:
                try:
                    with st.spinner("Reading and checking your documents..."):
                        cid = orch.submit_claim(files, claimant_ref=ref)
                    st.success(f"Claim submitted. Your claim ID is **{cid}**. Keep it to track progress.")
                    _show_customer_status(cid)
                except ValidationError as e:
                    st.error(str(e))
        with st.expander("Demo: submit a prepared sample claim"):
            st.caption("Tip: submit S001 before S011 (duplicate files) and S002 before S012 (similar claim) to see duplicate detection.")
            names = [p.name for p in sample_cases()]
            if names:
                choice = st.selectbox("Sample claim", names)
                if st.button("Submit this sample"):
                    with st.spinner("Processing sample..."):
                        cid = orch.submit_claim(load_sample(config.SAMPLES_DIR / choice), claimant_ref=f"sample:{choice}")
                    st.success(f"Sample submitted as claim **{cid}**")
                    _show_customer_status(cid)
            else:
                st.info("No samples found. Run `python scripts/generate_synthetic.py`.")

    with tab_track:
        cid = st.text_input("Claim ID", placeholder="CW-20261112-0001").strip()
        if cid:
            if not _show_customer_status(cid):
                st.warning("Claim not found.")


def _show_customer_status(cid: str) -> bool:
    view = orch.customer_view(cid)
    if not view:
        return False
    st.markdown(f"### Claim {view['claim_id']}")
    st.info(view["status_text"])
    if view["message_from_team"]:
        st.warning(f"Message from our claims team: {view['message_from_team']}")
    if view["requests"]:
        st.markdown("**What we need from you**")
        for r in view["requests"]:
            st.write(f"- {r}")
    if view["status"] in ("QUERY_RAISED", "PENDING_REVIEW"):
        with st.expander("Upload additional or replacement documents"):
            with st.form(f"resubmit_{cid}"):
                ups = {doc: st.file_uploader(DOC_LABELS[doc], type=[e.strip(".") for e in config.ALLOWED_EXT], key=f"re_{cid}_{doc}")
                       for doc in REQUIRED_DOCS}
                if st.form_submit_button("Send documents"):
                    files = read_uploads(ups)
                    try:
                        orch.resubmit(cid, files)
                        st.success("Thank you. Your documents were received and re-checked.")
                        st.rerun()
                    except ValidationError as e:
                        st.error(str(e))
    return True


# ----------------------------------------------------------------------------- employee dashboard
def login_box():
    if st.session_state.get("user"):
        st.sidebar.success(f"Signed in as {st.session_state['user']['username']}")
        if st.sidebar.button("Sign out"):
            st.session_state.pop("user", None)
            st.rerun()
        return True
    st.sidebar.subheader("Employee sign-in")
    u = st.sidebar.text_input("Username")
    p = st.sidebar.text_input("Password", type="password")
    st.sidebar.caption("Prototype demo accounts: officer1 or officer2, password from CLAIMWISE_DEMO_PASSWORD (default: demo-pass).")
    if st.sidebar.button("Sign in"):
        user = db.check_login(u, p)
        if user:
            st.session_state["user"] = user
            db.audit(None, u, "employee", "LOGIN", {})
            st.rerun()
        else:
            st.sidebar.error("Invalid credentials")
    return False


def page_employee():
    st.title("🧾 Claims officer dashboard")
    if not login_box():
        st.info("Please sign in from the sidebar to review claims.")
        return
    claims = db.list_claims()
    if not claims:
        st.info("No claims yet. Submit one from the Customer portal (try a sample).")
        return
    df = pd.DataFrame(claims)
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Total claims", len(df))
    c2.metric("Awaiting review", int((df["status"] == "PENDING_REVIEW").sum()))
    c3.metric("🔴 Manual review", int((df["lane"] == "MANUAL_REVIEW").sum()))
    c4.metric("Median processing (s)", f"{df['processing_ms'].dropna().median() / 1000:.1f}" if df["processing_ms"].notna().any() else "-")

    df["priority"] = df["lane"].map(LANE_PRIORITY).fillna(9)
    df["open"] = df["status"].isin(["PENDING_REVIEW", "QUERY_RAISED"]).map({True: 0, False: 1})
    df = df.sort_values(["open", "priority", "created_at"], ascending=[True, True, False])
    df["lane_label"] = df["lane"].map(lambda l: f"{LANE_ICON.get(l, '')} {l}" if l else "")
    show = df[["claim_id", "status", "lane_label", "score", "vehicle_reg", "incident_date", "claimed_amount", "created_at"]]
    show.columns = ["Claim", "Status", "Suggested lane", "Score", "Vehicle", "Incident", "Claimed (Rs)", "Submitted"]
    st.dataframe(show, hide_index=True)
    st.caption("Lane and score are advisory and only order your queue. They never approve or reject a claim.")

    cid = st.selectbox("Open claim", df["claim_id"].tolist())
    if cid:
        claim_detail(cid)


def claim_detail(cid: str):
    r = build_report(db, cid)
    c, s = r["claim"], r["summary"]
    st.divider()
    st.subheader(f"Claim {cid}")
    a, b, d = st.columns(3)
    a.metric("Status", c["status"])
    b.metric("Suggested lane", f"{LANE_ICON.get(s.get('lane'), '')} {s.get('lane', '-')}")
    d.metric("Confidence score (advisory)", f"{s.get('score', '-')}/100")
    st.caption(LANE_TEXT.get(s.get("lane"), ""))
    if s.get("recommended_actions"):
        with st.expander("Recommended next steps", expanded=True):
            for act in s["recommended_actions"]:
                st.write(f"- {act}")

    t_rep, t_data, t_docs, t_dec, t_dl = st.tabs(["Verification report", "Extracted data", "Documents", "Decision", "Download"])
    with t_rep:
        issues = sorted(r["issues"], key=lambda f: (SEVERITY_ORDER.get(f["severity"], 9), f["check_id"]))
        if not issues:
            st.success("All automated checks agreed. Review the source documents and decide.")
        for f in issues:
            with st.expander(f"{STATUS_ICON.get(f['status'], '')} {f['title']}: {f['status']} ({f['severity']})",
                             expanded=f["severity"] in ("critical", "high")):
                st.write(f["message"])
                if f["evidence"]:
                    st.dataframe(pd.DataFrame(f["evidence"])[["doc", "field", "value", "confidence"]].rename(
                        columns={"doc": "Document", "field": "Field", "value": "Extracted value", "confidence": "Confidence"}),
                        hide_index=True)
                if f["action"]:
                    st.markdown(f"**Suggested action:** {f['action']}")
        with st.expander(f"✅ Checks passed ({len(r['passed'])})"):
            for f in r["passed"]:
                st.write(f"- **{f['title']}**: {f['message']}")
    with t_data:
        data = pd.DataFrame(r["extracted"])
        if not data.empty:
            st.dataframe(data.rename(columns={"document": "Document", "field": "Field", "value": "Value",
                                              "confidence": "Confidence", "source": "Source"}),
                         hide_index=True)
    with t_docs:
        for doc_type, d in r["documents"].items():
            st.markdown(f"**{DOC_LABELS[doc_type]}** ({d['filename']})")
            p = Path(d["path"])
            if p.suffix.lower() in (".png", ".jpg", ".jpeg") and p.exists():
                st.image(str(p))
            else:
                st.caption("PDF file stored; open it from the file store to view.")
    with t_dec:
        decision_form(cid, c["status"], r["decisions"])
    with t_dl:
        st.download_button("Download report (PDF)", data=to_pdf(r), file_name=f"{cid}_report.pdf", mime="application/pdf")
        st.download_button("Download report (Markdown)", data=to_markdown(r), file_name=f"{cid}_report.md")


def decision_form(cid: str, status: str, decisions: list):
    for d in decisions:
        st.write(f"- {d['decided_at']}: **{d['decision']}** by {d['reviewer']}. {d['reason']}")
    if status not in ("PENDING_REVIEW", "QUERY_RAISED"):
        st.info(f"Claim is {status}. No further decision can be recorded.")
        return
    st.warning("You make the final decision. The system never approves or rejects a claim.")
    with st.form(f"decision_{cid}"):
        choice = st.radio("Decision", ["APPROVE", "QUERY", "REJECT"], horizontal=True)
        reason = st.text_area("Reason (required for Query or Reject; stored in the audit trail)")
        msg = st.text_area("Message to the customer (required for Query)")
        reviewed = st.checkbox("I have reviewed the flagged items against the source documents.")
        if st.form_submit_button("Record decision", type="primary"):
            if not reviewed:
                st.error("Please confirm you reviewed the evidence.")
            else:
                try:
                    new = orch.decide(cid, choice, st.session_state["user"], reason, msg)
                    st.success(f"Recorded. Claim is now {new}.")
                    st.rerun()
                except (ValidationError, PermissionError) as e:
                    st.error(str(e))


# ----------------------------------------------------------------------------- audit + about
def page_audit():
    st.title("📜 Audit log")
    if not login_box():
        st.info("Employees only. Please sign in from the sidebar.")
        return
    cid = st.text_input("Filter by claim ID (optional)").strip() or None
    rows = db.audit_rows(cid)
    st.caption("Append-only: database triggers block updates and deletes of audit rows.")
    st.dataframe(pd.DataFrame(rows), hide_index=True)


def page_about():
    st.title("About ClaimWise AI")
    st.markdown("""
**ClaimWise AI** is a human-in-the-loop verification layer for motor insurance claims.
It reads uploaded documents (OCR + structured extraction), cross-checks them, validates the policy against carrier
records, looks for duplicate submissions, and prepares an **evidence report**. An authorised employee makes every decision.

**In this prototype:** document intelligence, field verification, policy verification (mock carrier data),
basic duplicate detection (identical files by SHA-256; similar claims by vehicle and date), confidence-based review lanes,
employee review, audit trail and customer status tracking. Data is synthetic.

**Future scope (not built):** damage assessment with computer vision, advanced fraud analytics, live Guidewire / Duck Creek integration,
other insurance lines.
""")


PAGES = {"Customer portal": page_customer, "Employee dashboard": page_employee, "Audit log": page_audit, "About": page_about}
choice = st.sidebar.radio("Go to", list(PAGES))
PAGES[choice]()
