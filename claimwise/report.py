"""Evidence report builder (dict, Markdown and PDF) for the employee review step."""
from __future__ import annotations
import io
import json

from .schemas import (DOC_LABELS, FIELD_SPECS, LANE_TEXT, SEVERITY_ORDER, STATUS_MATCH)


def build_report(db, claim_id: str) -> dict:
    claim = db.get_claim(claim_id)
    if not claim:
        raise KeyError(claim_id)
    findings = db.get_findings(claim_id)
    issues = sorted([f for f in findings if f.status != STATUS_MATCH], key=lambda f: (SEVERITY_ORDER.get(f.severity, 9), f.check_id))
    passes = [f for f in findings if f.status == STATUS_MATCH]
    ext = db.get_extractions(claim_id)
    table = []
    for doc_type, ex in ext.items():
        for fname, label, _ in FIELD_SPECS[doc_type]:
            fv = ex.fields.get(fname)
            table.append({"document": DOC_LABELS[doc_type], "field": label,
                          "value": fv.value if fv else "(not read)", "confidence": round(fv.confidence, 2) if fv else 0.0,
                          "source": fv.source if fv else ""})
    return {
        "claim": claim,
        "summary": json.loads(claim["summary_json"]) if claim.get("summary_json") else {},
        "issues": [f.to_dict() for f in issues],
        "passed": [f.to_dict() for f in passes],
        "extracted": table,
        "decisions": db.get_decisions(claim_id),
        "documents": db.get_documents(claim_id),
    }


def to_markdown(r: dict) -> str:
    c, s = r["claim"], r["summary"]
    out = [f"# Verification report: {c['claim_id']}", "",
           f"- Status: **{c['status']}**", f"- Suggested review lane: **{s.get('lane', '-')}** ({LANE_TEXT.get(s.get('lane'), '')})",
           f"- Confidence score: **{s.get('score', '-')}/100** (advisory)", f"- Submitted: {c['created_at']}", "",
           "> Advisory report. Flags are prompts for review, not proof of wrongdoing. "
           "The final decision is made by an authorised insurance employee.", "", "## Items needing attention", ""]
    if not r["issues"]:
        out.append("None. All automated checks agreed.")
    for f in r["issues"]:
        out += [f"### [{f['status']}] {f['title']} ({f['severity']})", f["message"], ""]
        for e in f["evidence"]:
            out.append(f"- {e['doc']} / {e['field']}: `{e['value']}` (confidence {e['confidence']})")
        if f["action"]:
            out.append(f"- **Suggested action:** {f['action']}")
        out.append("")
    out += ["## Checks passed", ""] + [f"- {f['title']}: {f['message']}" for f in r["passed"]]
    out += ["", "## Extracted data", "", "| Document | Field | Value | Confidence |", "|---|---|---|---|"]
    out += [f"| {x['document']} | {x['field']} | {x['value']} | {x['confidence']} |" for x in r["extracted"]]
    if r["decisions"]:
        out += ["", "## Human decision(s)"] + [f"- {d['decided_at']}: **{d['decision']}** by {d['reviewer']}. {d['reason']}" for d in r["decisions"]]
    return "\n".join(out)


def to_pdf(r: dict) -> bytes:
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import getSampleStyleSheet
    from reportlab.lib.units import mm
    from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

    st = getSampleStyleSheet()
    small = st["BodyText"].clone("small", fontSize=8.5, leading=11)
    c, s = r["claim"], r["summary"]
    esc = lambda t: str(t).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4, leftMargin=15 * mm, rightMargin=15 * mm, topMargin=14 * mm, bottomMargin=14 * mm,
                            title=f"ClaimWise verification report {c['claim_id']}")
    el = [Paragraph(f"ClaimWise AI: Verification Report {esc(c['claim_id'])}", st["Title"]),
          Paragraph(f"Status: <b>{esc(c['status'])}</b> | Suggested lane: <b>{esc(s.get('lane', '-'))}</b> | "
                    f"Confidence score: <b>{esc(s.get('score', '-'))}/100</b> (advisory)", st["BodyText"]),
          Paragraph("Advisory report. Flags are prompts for review, not proof of wrongdoing. The final decision is made by an "
                    "authorised insurance employee.", small), Spacer(1, 6), Paragraph("Items needing attention", st["Heading2"])]
    if not r["issues"]:
        el.append(Paragraph("None. All automated checks agreed.", st["BodyText"]))
    rows = [[Paragraph("<b>Status</b>", small), Paragraph("<b>Check</b>", small), Paragraph("<b>Explanation and evidence</b>", small)]]
    for f in r["issues"]:
        ev = "<br/>".join(f"{esc(e['doc'])} / {esc(e['field'])}: <b>{esc(e['value'])}</b> ({e['confidence']})" for e in f["evidence"])
        body = esc(f["message"]) + (f"<br/>{ev}" if ev else "") + (f"<br/><i>Action: {esc(f['action'])}</i>" if f["action"] else "")
        rows.append([Paragraph(f"{esc(f['status'])}<br/>{esc(f['severity'])}", small), Paragraph(esc(f["title"]), small), Paragraph(body, small)])
    if len(rows) > 1:
        t = Table(rows, colWidths=[24 * mm, 45 * mm, 111 * mm], repeatRows=1)
        t.setStyle(TableStyle([("GRID", (0, 0), (-1, -1), 0.4, colors.grey), ("BACKGROUND", (0, 0), (-1, 0), colors.lightgrey),
                               ("VALIGN", (0, 0), (-1, -1), "TOP")]))
        el.append(t)
    el += [Spacer(1, 8), Paragraph("Checks passed", st["Heading2"])]
    el += [Paragraph(f"&bull; {esc(f['title'])}: {esc(f['message'])}", small) for f in r["passed"]]
    el += [Spacer(1, 8), Paragraph("Extracted data", st["Heading2"])]
    rows = [["Document", "Field", "Value", "Conf."]] + [[x["document"], x["field"], x["value"], x["confidence"]] for x in r["extracted"]]
    rows = [[Paragraph(esc(v), small) for v in row] for row in rows]
    t = Table(rows, colWidths=[45 * mm, 45 * mm, 70 * mm, 20 * mm], repeatRows=1)
    t.setStyle(TableStyle([("GRID", (0, 0), (-1, -1), 0.3, colors.grey), ("BACKGROUND", (0, 0), (-1, 0), colors.lightgrey)]))
    el.append(t)
    if r["decisions"]:
        el += [Spacer(1, 8), Paragraph("Human decision(s)", st["Heading2"])]
        el += [Paragraph(f"{esc(d['decided_at'])}: <b>{esc(d['decision'])}</b> by {esc(d['reviewer'])}. {esc(d['reason'])}", small) for d in r["decisions"]]
    doc.build(el)
    return buf.getvalue()
