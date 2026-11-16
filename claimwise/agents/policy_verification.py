"""Policy verification agent: configured rules checked against the carrier's own policy record."""
from __future__ import annotations
from datetime import date

from .. import config
from ..normalize import name_similarity, norm_alnum, ocr_confusable_equal, parse_amount, parse_date
from ..schemas import (DOC_LABELS, STATUS_MATCH, STATUS_MISMATCH, STATUS_REVIEW, Finding, evidence)
from .base import Agent, get_field


def _d(v):
    return parse_date(v.value) if v else None


class PolicyVerificationAgent(Agent):
    name = "Policy verification"

    def run(self, ctx):
        out = []
        ex = ctx.extractions
        _, pol_no = get_field(ctx, "policy", "policy_number")
        if pol_no is None:
            _, pol_no = get_field(ctx, "claim_form", "policy_number")
        if pol_no is None:
            if "policy" in ex or "claim_form" in ex:
                out.append(Finding("POLICY_EXISTS", "Policy number readable", STATUS_REVIEW, "medium", self.name,
                                   "No policy number could be read, so the carrier record could not be checked.",
                                   "Ask the claimant for a clearer copy of the policy schedule or claim form."))
            return out
        rec = ctx.policy_service.get_policy(pol_no.value) if ctx.policy_service else None
        src_doc = "policy" if "policy" in ex and "policy_number" in ex["policy"].fields else "claim_form"
        ev_pol = [evidence(ex, src_doc, "policy_number")]
        if rec is None:
            out.append(Finding("POLICY_EXISTS", "Policy exists in carrier records", STATUS_MISMATCH, "critical", self.name,
                               f"Policy number {pol_no.value} was not found in the carrier's records.",
                               "Verify the policy number with the claimant; check for a typing or reading error.", ev_pol))
            return out
        out.append(Finding("POLICY_EXISTS", "Policy exists in carrier records", STATUS_MATCH, "info", self.name,
                           f"Policy {pol_no.value} found in carrier records.", "", ev_pol))

        # status
        ok = str(rec.get("status", "")).upper() == "ACTIVE"
        out.append(Finding("POLICY_STATUS", "Policy status is active", STATUS_MATCH if ok else STATUS_MISMATCH,
                           "info" if ok else "critical", self.name,
                           f"Carrier record shows status {rec.get('status')}.",
                           "" if ok else "Confirm policy status with the underwriting team before proceeding.", ev_pol))

        # policy document vs carrier record
        hard, soft, matched_ev = [], [], []
        if "policy" in ex:
            checks = [("insured_name", "name"), ("vehicle_reg_no", "id"), ("chassis_no", "id"),
                      ("policy_start", "date"), ("policy_end", "date"), ("idv", "amount")]
            for f, kind in checks:
                _, fv = get_field(ctx, "policy", f)
                truth = rec.get(f)
                if fv is None or truth is None:
                    continue
                confusable = False
                if kind == "name":
                    same = name_similarity(fv.value, str(truth)) >= config.NAME_MATCH
                elif kind == "id":
                    same = norm_alnum(fv.value) == norm_alnum(str(truth))
                    confusable = not same and ocr_confusable_equal(fv.value, str(truth))
                elif kind == "date":
                    same = _d(fv) == parse_date(str(truth))
                else:
                    a = parse_amount(fv.value)
                    same = a is not None and abs(a - float(truth)) < 1
                e = evidence(ex, "policy", f)
                if same:
                    matched_ev.append(e)
                    continue
                note = f"{e['field']}: document '{fv.value}' vs carrier '{truth}'"
                (soft if (confusable or fv.confidence < config.LOW_CONF_DOWNGRADE) else hard).append((note, e))
        if hard:
            out.append(Finding("CARRIER_RECORD", "Policy document matches carrier record", STATUS_MISMATCH, "high", self.name,
                               "The policy document differs from the carrier's record: " + "; ".join(n for n, _ in hard + soft) + ".",
                               "The submitted policy copy may be altered or outdated. Rely on the carrier record and confirm with the claimant.",
                               [e for _, e in hard + soft]))
        elif soft:
            out.append(Finding("CARRIER_RECORD", "Policy document matches carrier record", STATUS_REVIEW, "low", self.name,
                               "Possible difference from the carrier record, but the value was read with low confidence or differs only by "
                               "commonly confused characters: " + "; ".join(n for n, _ in soft) + ".",
                               "Check the policy document against the carrier record manually.", [e for _, e in soft]))
        elif "policy" in ex:
            out.append(Finding("CARRIER_RECORD", "Policy document matches carrier record", STATUS_MATCH, "info", self.name,
                               "Submitted policy document is consistent with the carrier record.", "", matched_ev))

        # incident date within period (carrier dates are the source of truth)
        _, inc = get_field(ctx, "claim_form", "incident_date")
        inc_d = _d(inc)
        start, end = parse_date(str(rec.get("policy_start"))), parse_date(str(rec.get("policy_end")))
        if "claim_form" in ex:
            if inc_d is None or not start or not end:
                out.append(Finding("POLICY_PERIOD", "Incident date within policy period", STATUS_REVIEW, "medium", self.name,
                                   "Incident date could not be read reliably, so policy period could not be checked.",
                                   "Confirm the incident date with the claimant.", [evidence(ex, "claim_form", "incident_date")]))
            else:
                ok = start <= inc_d <= end
                out.append(Finding("POLICY_PERIOD", "Incident date within policy period", STATUS_MATCH if ok else STATUS_MISMATCH,
                                   "info" if ok else "critical", self.name,
                                   (f"Incident on {inc_d} is within the policy period {start} to {end}." if ok else
                                    f"Incident on {inc_d} is OUTSIDE the policy period {start} to {end} (carrier record)."),
                                   "" if ok else "Check the incident date and policy dates; coverage may not apply.",
                                   [evidence(ex, "claim_form", "incident_date")]))

        # coverage type
        cov = str(rec.get("coverage_type", "")).upper()
        if "claim_form" in ex and cov:
            ok = "COMPREHENSIVE" in cov
            out.append(Finding("COVERAGE_TYPE", "Coverage includes own damage", STATUS_MATCH if ok else STATUS_MISMATCH,
                               "info" if ok else "high", self.name,
                               f"Carrier record shows coverage type '{rec.get('coverage_type')}'.",
                               "" if ok else "Own-damage claims may not be covered; confirm policy terms.", ev_pol))

        # licence validity at incident date
        _, valid = get_field(ctx, "driving_licence", "valid_upto")
        if "driving_licence" in ex and "claim_form" in ex:
            v_d = _d(valid)
            if v_d is None or inc_d is None:
                out.append(Finding("DL_VALIDITY", "Driving licence valid on incident date", STATUS_REVIEW, "medium", self.name,
                                   "Licence expiry or incident date could not be read reliably.",
                                   "Check the licence expiry date manually.", [evidence(ex, "driving_licence", "valid_upto")]))
            else:
                ok = v_d >= inc_d
                out.append(Finding("DL_VALIDITY", "Driving licence valid on incident date", STATUS_MATCH if ok else STATUS_MISMATCH,
                                   "info" if ok else "critical", self.name,
                                   (f"Licence valid until {v_d}, incident on {inc_d}." if ok else
                                    f"Licence expired on {v_d}, before the incident on {inc_d}."),
                                   "" if ok else "Confirm licence validity (renewal grace rules may apply) before deciding.",
                                   [evidence(ex, "driving_licence", "valid_upto"), evidence(ex, "claim_form", "incident_date")]))

        # amounts
        _, claimed = get_field(ctx, "claim_form", "claimed_amount")
        _, est = get_field(ctx, "claim_form", "repair_estimate")
        c_amt = parse_amount(claimed.value) if claimed else None
        e_amt = parse_amount(est.value) if est else None
        idv = rec.get("idv")
        if c_amt is not None and idv is not None:
            ok = c_amt <= float(idv)
            out.append(Finding("AMOUNT_VS_IDV", "Claimed amount within insured declared value", STATUS_MATCH if ok else STATUS_MISMATCH,
                               "info" if ok else "critical", self.name,
                               (f"Claimed amount Rs {c_amt:,.0f} is within the IDV of Rs {float(idv):,.0f}." if ok else
                                f"Claimed amount Rs {c_amt:,.0f} EXCEEDS the insured declared value of Rs {float(idv):,.0f}."),
                               "" if ok else "Check the amount and policy limits.", [evidence(ex, "claim_form", "claimed_amount")]))
        if c_amt is not None and e_amt:
            gap = abs(c_amt - e_amt) / e_amt
            ok = gap <= config.ESTIMATE_TOLERANCE
            out.append(Finding("ESTIMATE_VS_CLAIM", "Claimed amount consistent with garage estimate", STATUS_MATCH if ok else STATUS_MISMATCH,
                               "info" if ok else "medium", self.name,
                               (f"Claimed Rs {c_amt:,.0f} vs estimate Rs {e_amt:,.0f} (difference {gap:.0%})."),
                               "" if ok else "Ask for the itemised estimate or invoice to explain the difference.",
                               [evidence(ex, "claim_form", "claimed_amount"), evidence(ex, "claim_form", "repair_estimate")]))
        return out
