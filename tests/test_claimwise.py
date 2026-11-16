"""Tests. Run:  python -m unittest discover -s tests -v   (or: pytest tests)

Unit tests need nothing installed beyond the project requirements. Integration tests need the Tesseract binary
and are skipped automatically if it is missing.
"""
import random
import shutil
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from claimwise import config  # noqa: E402
from claimwise.agents.confidence import ConfidenceEvidenceAgent  # noqa: E402
from claimwise.agents.field_verification import FieldVerificationAgent  # noqa: E402
from claimwise.agents.policy_verification import PolicyVerificationAgent  # noqa: E402
from claimwise.db import Database  # noqa: E402
from claimwise.normalize import (clean_value, name_similarity, norm_alnum, ocr_confusable_equal, parse_amount,  # noqa: E402
                                 parse_date, repair_id)
from claimwise.orchestrator import Orchestrator, ValidationError  # noqa: E402
from claimwise.policy_service import PolicyService  # noqa: E402
from claimwise.quality import check_image_quality  # noqa: E402
from claimwise.report import build_report, to_markdown, to_pdf  # noqa: E402
from claimwise.schemas import ClaimContext, Extraction, FieldValue  # noqa: E402
from claimwise.synthetic import build_case, build_dataset  # noqa: E402


def make_ext(doc, **fields):
    ex = Extraction(doc_type=doc)
    for k, v in fields.items():
        ex.fields[k] = FieldValue(value=str(v), raw=str(v), confidence=0.95)
    return ex


def ctx_for(extractions, db=None, svc=None):
    return ClaimContext("T-1", {}, extractions, db, svc)


class NormalizeTests(unittest.TestCase):
    def test_dates_and_amounts(self):
        self.assertEqual(str(parse_date("10-11-2026")), "2026-11-10")
        self.assertEqual(str(parse_date("10 Nov 2026")), "2026-11-10")
        self.assertEqual(str(parse_date("1O-11-2O26")), "2026-11-10")  # OCR O for 0
        self.assertIsNone(parse_date("not a date"))
        self.assertEqual(parse_amount("Rs. 62,000/-"), 62000.0)
        self.assertIsNone(parse_amount("abc"))

    def test_names(self):
        self.assertGreater(name_similarity("Ramesh Reddy", "REDDY RAMESH"), 0.95)
        self.assertGreater(name_similarity("Mr. Ramesh Reddy", "Ramesh Reddy"), 0.95)
        self.assertLess(name_similarity("Ramesh Reddy", "Sita Sharma"), 0.5)

    def test_id_repair_and_confusable(self):
        self.assertEqual(repair_id("reg", "MHO5CJ8767"), "MH05CJ8767")
        self.assertEqual(repair_id("reg", "TS09AB1234"), "TS09AB1234")
        self.assertEqual(repair_id("dl", "MH052O107536114"), "MH0520107536114")
        self.assertEqual(repair_id("chassis", "OD9PCF43ESF4DHRD2"), "0D9PCF43ESF4DHRD2")
        self.assertTrue(ocr_confusable_equal("TS09AB1234", "TS09A81234"))
        self.assertFalse(ocr_confusable_equal("TS09AB1234", "TS09AB1235"))
        self.assertEqual(norm_alnum("ts 09-ab 1234"), "TS09AB1234")
        self.assertEqual(clean_value("reg", "ts 09 ab 1234"), ("TS09AB1234", True))


class QualityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import io
        from PIL import Image, ImageEnhance, ImageFilter
        cls.io = io
        case = build_case(random.Random(5), 1, "clean", "clean")
        cls.good = Image.open(io.BytesIO(case.docs["rc"][1])).convert("RGB")
        cls.variants = {
            "blur": cls.good.filter(ImageFilter.GaussianBlur(3)),
            "dark": ImageEnhance.Brightness(cls.good).enhance(0.25),
            "faded": ImageEnhance.Contrast(cls.good).enhance(0.15),
            "tiny": cls.good.resize((400, int(cls.good.height * 400 / cls.good.width))),
        }

    def png(self, img):
        buf = self.io.BytesIO()
        img.save(buf, "PNG")
        return buf.getvalue()

    def test_sharp_scan_passes(self):
        self.assertTrue(check_image_quality(self.png(self.good), "a.png").ok)

    def test_each_problem_is_detected(self):
        expect = {"blur": "blurry", "dark": "dark", "faded": "low_contrast", "tiny": "low_resolution"}
        for name, code in expect.items():
            r = check_image_quality(self.png(self.variants[name]), f"{name}.png")
            self.assertFalse(r.ok, name)
            self.assertIn(code, [i["code"] for i in r.issues], name)
            self.assertTrue(r.advice)

    def test_degraded_but_readable_scan_passes(self):
        deg = build_case(random.Random(5), 2, "clean", "degraded")
        for name, (fn, data) in deg.docs.items():
            self.assertTrue(check_image_quality(data, fn).ok, name)

    def test_pdf_skipped_and_garbage_rejected(self):
        self.assertTrue(check_image_quality(b"%PDF-1.4", "x.pdf").ok)
        r = check_image_quality(b"not an image at all", "x.png")
        self.assertFalse(r.ok)
        self.assertEqual(r.issues[0]["code"], "unreadable_file")


class FieldVerificationTests(unittest.TestCase):
    def run_agent(self, claim_reg, rc_reg, claimant="Ramesh Reddy", owner="Ramesh Reddy"):
        ex = {"claim_form": make_ext("claim_form", vehicle_reg_no=claim_reg, claimant_name=claimant),
              "rc": make_ext("rc", vehicle_reg_no=rc_reg, owner_name=owner)}
        return {f.check_id: f for f in FieldVerificationAgent().run(ctx_for(ex))}

    def test_match(self):
        f = self.run_agent("TS09AB1234", "TS09AB1234")
        self.assertEqual(f["REG_CLAIM_RC"].status, "MATCH")
        self.assertEqual(f["NAME_CLAIM_RC"].status, "MATCH")

    def test_mismatch_has_evidence(self):
        f = self.run_agent("TS09AB1234", "TS09AB9999", claimant="Ramesh Reddy", owner="Sita Sharma")
        self.assertEqual(f["REG_CLAIM_RC"].status, "MISMATCH")
        self.assertEqual(f["NAME_CLAIM_RC"].status, "MISMATCH")
        self.assertEqual(len(f["REG_CLAIM_RC"].evidence), 2)
        self.assertTrue(f["REG_CLAIM_RC"].action)

    def test_ocr_confusable_is_review_not_mismatch(self):
        self.assertEqual(self.run_agent("TS09AB1234", "TS09A81234")["REG_CLAIM_RC"].status, "REVIEW")

    def test_low_confidence_mismatch_is_downgraded(self):
        ex = {"claim_form": make_ext("claim_form", vehicle_reg_no="TS09AB1234"), "rc": make_ext("rc", vehicle_reg_no="TS09AB9999")}
        ex["rc"].fields["vehicle_reg_no"].confidence = 0.3
        f = {x.check_id: x for x in FieldVerificationAgent().run(ctx_for(ex))}
        self.assertEqual(f["REG_CLAIM_RC"].status, "REVIEW")

    def test_missing_document_is_skipped_here(self):
        ex = {"claim_form": make_ext("claim_form", vehicle_reg_no="TS09AB1234")}
        self.assertEqual(FieldVerificationAgent().run(ctx_for(ex)), [])


class PolicyVerificationTests(unittest.TestCase):
    REC = {"POL-MTR-2025-000001": {"insured_name": "Ramesh Reddy", "vehicle_reg_no": "TS09AB1234", "chassis_no": "A" * 17,
                                   "policy_start": "2025-06-01", "policy_end": "2026-05-31", "idv": 500000,
                                   "coverage_type": "COMPREHENSIVE", "status": "ACTIVE"}}

    def run_agent(self, incident="2025-12-01", claimed=40000, estimate=40000, valid_upto="2030-01-01", rec=None):
        data = self.REC if rec is None else rec
        ex = {"claim_form": make_ext("claim_form", policy_number="POL-MTR-2025-000001", incident_date=incident,
                                     claimed_amount=claimed, repair_estimate=estimate),
              "driving_licence": make_ext("driving_licence", valid_upto=valid_upto)}
        return {f.check_id: f for f in PolicyVerificationAgent().run(ctx_for(ex, svc=PolicyService(data=data)))}

    def test_all_pass(self):
        f = self.run_agent()
        for k in ("POLICY_EXISTS", "POLICY_STATUS", "POLICY_PERIOD", "DL_VALIDITY", "AMOUNT_VS_IDV", "ESTIMATE_VS_CLAIM"):
            self.assertEqual(f[k].status, "MATCH", k)

    def test_rules_fail(self):
        self.assertEqual(self.run_agent(incident="2026-08-01")["POLICY_PERIOD"].status, "MISMATCH")
        self.assertEqual(self.run_agent(claimed=600000, estimate=600000)["AMOUNT_VS_IDV"].status, "MISMATCH")
        self.assertEqual(self.run_agent(claimed=60000, estimate=40000)["ESTIMATE_VS_CLAIM"].status, "MISMATCH")
        self.assertEqual(self.run_agent(valid_upto="2025-01-01")["DL_VALIDITY"].status, "MISMATCH")

    def test_unknown_and_inactive_policy(self):
        self.assertEqual(self.run_agent(rec={})["POLICY_EXISTS"].status, "MISMATCH")
        rec = {k: {**v, "status": "LAPSED"} for k, v in self.REC.items()}
        self.assertEqual(self.run_agent(rec=rec)["POLICY_STATUS"].status, "MISMATCH")


class ConfidenceTests(unittest.TestCase):
    def test_missing_docs_force_manual_review(self):
        ex = {"claim_form": make_ext("claim_form", vehicle_reg_no="TS09AB1234")}
        findings, summary = ConfidenceEvidenceAgent().run(ctx_for(ex), [])
        self.assertEqual(summary["lane"], "MANUAL_REVIEW")
        self.assertTrue(any(f.check_id == "DOC_MISSING_RC" for f in findings))
        self.assertIn("Advisory", summary["note"])


class AuditAndDecisionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.db = Database(self.tmp / "t.db")
        self.orch = Orchestrator(self.db, PolicyService(data={}), store_dir=self.tmp / "f", use_llm=False)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_audit_log_is_append_only(self):
        self.db.audit("C1", "x", "system", "EVENT", {})
        with self.assertRaises(sqlite3.DatabaseError):
            with self.db.conn() as c:
                c.execute("UPDATE audit_log SET event='tampered'")
        with self.assertRaises(sqlite3.DatabaseError):
            with self.db.conn() as c:
                c.execute("DELETE FROM audit_log")

    def test_login_and_roles(self):
        self.db.add_user("o1", "pw", "employee")
        self.assertIsNone(self.db.check_login("o1", "wrong"))
        self.assertEqual(self.db.check_login("o1", "pw")["role"], "employee")

    def test_validation(self):
        with self.assertRaises(ValidationError):
            self.orch.submit_claim({"rc": ("x.exe", b"data")}, process=False)
        with self.assertRaises(ValidationError):
            self.orch.submit_claim({"nope": ("x.png", b"data")}, process=False)
        with self.assertRaises(ValidationError):
            self.orch.submit_claim({}, process=False)

    def test_only_employee_can_decide_and_rules(self):
        cid = self.orch.submit_claim({"rc": ("rc.png", b"\x89PNG not really")}, process=False)
        self.db.update_claim(cid, status="PENDING_REVIEW")
        with self.assertRaises(PermissionError):
            self.orch.decide(cid, "APPROVE", None)
        with self.assertRaises(PermissionError):
            self.orch.decide(cid, "APPROVE", {"username": "cust", "role": "customer"})
        emp = {"username": "officer1", "role": "employee"}
        with self.assertRaises(ValidationError):
            self.orch.decide(cid, "REJECT", emp, reason="")
        with self.assertRaises(ValidationError):
            self.orch.decide(cid, "MAYBE", emp)  # not a valid decision
        # a query with a reason but no separate customer message falls back to the reason
        self.assertEqual(self.orch.decide(cid, "QUERY", emp, reason="need a clearer RC"), "QUERY_RAISED")
        self.assertEqual(self.orch.customer_view(cid)["message_from_team"], "need a clearer RC")
        self.assertEqual(self.orch.decide(cid, "APPROVE", emp), "APPROVED")
        with self.assertRaises(ValidationError):
            self.orch.decide(cid, "REJECT", emp, reason="late change")  # already final
        events = [r["event"] for r in self.db.audit_rows(cid)]
        self.assertIn("HUMAN_DECISION", events)


@unittest.skipUnless(shutil.which("tesseract"), "Tesseract binary not installed")
class EndToEndTests(unittest.TestCase):
    """Real OCR on freshly generated synthetic documents."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp())
        rng = random.Random(99)
        cls.clean = build_case(rng, 1, "clean", "clean")
        cls.reg = build_case(rng, 2, "reg_mismatch", "clean")
        cls.exp = build_case(rng, 3, "policy_expired", "clean")
        cls.dup = build_case(rng, 4, "duplicate_file", "clean", base=cls.clean)
        cls.miss = build_case(rng, 5, "missing_doc", "clean")
        carrier = {}
        for c in (cls.clean, cls.reg, cls.exp, cls.miss):
            carrier.update(c.carrier)
        cls.db = Database(cls.tmp / "e.db")
        cls.orch = Orchestrator(cls.db, PolicyService(data=carrier), store_dir=cls.tmp / "f", use_llm=False)
        cls.ids = {n: cls.orch.submit_claim(c.docs) for n, c in
                   (("clean", cls.clean), ("reg", cls.reg), ("exp", cls.exp), ("dup", cls.dup), ("miss", cls.miss))}
        # statuses captured straight after processing, before any test records a human decision
        cls.initial_status = {n: cls.db.get_claim(cid)["status"] for n, cid in cls.ids.items()}

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def flagged(self, name):
        return {f.check_id: f for f in self.db.get_findings(self.ids[name]) if f.status != "MATCH"}

    def test_clean_claim_has_no_hard_flags(self):
        hard = [f for f in self.flagged("clean").values() if f.status in ("MISMATCH", "MISSING", "CANDIDATE")]
        self.assertEqual(hard, [])

    def test_planted_reg_mismatch_found_with_evidence(self):
        f = self.flagged("reg")
        self.assertIn("REG_CLAIM_RC", f)
        self.assertEqual(f["REG_CLAIM_RC"].status, "MISMATCH")
        self.assertEqual({e["doc_type"] for e in f["REG_CLAIM_RC"].evidence}, {"claim_form", "rc"})

    def test_expired_policy_period(self):
        self.assertEqual(self.flagged("exp")["POLICY_PERIOD"].status, "MISMATCH")

    def test_identical_files_flagged_as_duplicate(self):
        self.assertEqual(self.flagged("dup")["DUP_FILE"].status, "CANDIDATE")

    def test_missing_document(self):
        self.assertEqual(self.flagged("miss")["DOC_MISSING_DRIVING_LICENCE"].status, "MISSING")
        self.assertEqual(self.db.get_claim(self.ids["miss"])["lane"], "MANUAL_REVIEW")

    def test_pipeline_never_decides(self):
        self.assertEqual(set(self.initial_status.values()), {"PENDING_REVIEW"})

    def test_customer_view_hides_internal_scores_and_flags(self):
        v = self.orch.customer_view(self.ids["dup"])
        text = str(v).lower()
        for hidden in ("score", "dup_file", "duplicate", "fraud", "mismatch", "lane"):
            self.assertNotIn(hidden, text)
        miss = self.orch.customer_view(self.ids["miss"])
        self.assertTrue(any("driving licence" in r.lower() for r in miss["requests"]))

    def test_blurry_upload_is_flagged_and_customer_is_told_to_retake(self):
        from PIL import ImageFilter, Image
        import io
        c = build_case(random.Random(123), 7, "clean", "clean")
        blurred = Image.open(io.BytesIO(c.docs["driving_licence"][1])).convert("RGB").filter(ImageFilter.GaussianBlur(3))
        buf = io.BytesIO()
        blurred.save(buf, "PNG")
        docs = dict(c.docs)
        docs["driving_licence"] = ("driving_licence.png", buf.getvalue())
        self.orch.policy_service = PolicyService(data=c.carrier)
        cid = self.orch.submit_claim(docs)
        f = {x.check_id: x for x in self.db.get_findings(cid)}
        self.assertIn("IMAGE_QUALITY:driving_licence", f)
        self.assertNotIn("IMAGE_QUALITY:rc", f)
        reqs = " ".join(self.orch.customer_view(cid)["requests"]).lower()
        self.assertIn("retake", reqs)
        self.assertIn("driving licence", reqs)

    def test_reports_render(self):
        r = build_report(self.db, self.ids["reg"])
        self.assertIn("REG_CLAIM_RC", str(r["issues"]))
        self.assertIn("Advisory", to_markdown(r))
        self.assertTrue(to_pdf(r).startswith(b"%PDF"))

    def test_human_decision_flow(self):
        emp = {"username": "officer1", "role": "employee"}
        cid = self.ids["clean"]
        self.assertEqual(self.orch.decide(cid, "APPROVE", emp), "APPROVED")
        self.assertEqual(self.orch.customer_view(cid)["status"], "APPROVED")


if __name__ == "__main__":
    unittest.main()
