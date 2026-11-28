"""Batch 4: payment rules, VAT, forgiven amounts, receipts per payment type, refunds, paying for someone else."""
from decimal import Decimal
from unittest import mock

from django.contrib.auth import get_user_model
from django.test import TestCase
from rest_framework.test import APIClient

from api.models import UserProfile
from clinic import billing
from clinic.models import BillableItem, BillableService, Encounter, Patient, PaymentRule, PaymentTransaction

TIN = "B4-TIN"
PERM = "tenants.permissions.TenantBillingAccessPermission.has_permission"


def make_user(name, role):
    user = get_user_model().objects.create_user(name, password="pw-123456")
    UserProfile.objects.create(user=user, clinic_tin=TIN, role=role, branch_name="Main")
    client = APIClient()
    client.force_authenticate(user)
    return client


class BillingBase(TestCase):
    def setUp(self):
        patcher = mock.patch(PERM, return_value=True)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.manager = make_user("b4mgr", "manager")
        self.reception = make_user("b4rec", "reception")
        self.lab = BillableService.objects.create(
            clinic_tin=TIN, branch_name="Main", code="LAB1", name="CBC", department="Lab", unit_price=200, service_type="lab"
        )
        self.consult = BillableService.objects.create(
            clinic_tin=TIN, branch_name="Main", code="CON1", name="Consultation", department="OPD", unit_price=300,
            service_type="consultation",
        )
        self.enc = self.visit("Hana Bekele", "M1", "E1")

    def visit(self, name, mrn, number):
        patient = Patient.objects.create(clinic_tin=TIN, branch_name="Main", mrn=mrn, full_name=name, age=30)
        enc = Encounter.objects.create(clinic_tin=TIN, branch_name="Main", number=number, patient=patient, arrival_type="new")
        for svc in (self.consult, self.lab):
            BillableItem.objects.create(
                encounter=enc, service=svc, description=svc.name, department=svc.department, unit_price=svc.unit_price
            )
        return enc

    def approve(self, enc=None, **body):
        body.setdefault("encounter", (enc or self.enc).id)
        return self.reception.post("/api/clinic/payments/approve/", body, format="json")

    def rule(self, kind="discount", **over):
        body = {"kind": kind, "name": "Staff", "reason": "Staff member", "all_services": True, "service_ids": [], "percent": "10"}
        body.update(over)
        return self.manager.post("/api/clinic/payment-rules/", body, format="json")


class VatTests(BillingBase):
    def test_vat_is_15_percent_of_the_price_and_shown(self):
        data = self.reception.get(f"/api/clinic/encounters/{self.enc.id}/").json()
        self.assertEqual(Decimal(str(data["vat_total"])), Decimal("75.00"))
        self.assertEqual({b["description"]: b["vat_amount"] for b in data["billables"]}["CBC"], 30.0)

    def test_a_payment_records_its_vat(self):
        self.approve(parts=[{"method": "cash", "amount": "500"}])
        self.assertEqual(PaymentTransaction.objects.get().vat_amount, Decimal("75.00"))


class PaymentRuleTests(BillingBase):
    def test_manager_makes_edits_and_deletes_a_rule(self):
        res = self.rule()
        self.assertEqual(res.status_code, 201, res.content)
        rid = res.json()["id"]
        self.assertEqual(res.json()["service_names"], ["All services"])
        self.assertEqual(self.manager.patch(f"/api/clinic/payment-rules/{rid}/", {"percent": "20"}, format="json").status_code, 200)
        self.assertEqual(self.manager.delete(f"/api/clinic/payment-rules/{rid}/").status_code, 204)

    def test_reception_can_read_but_not_change_rules(self):
        self.rule()
        self.assertEqual(self.reception.get("/api/clinic/payment-rules/?kind=discount").status_code, 200)
        res = self.reception.post(
            "/api/clinic/payment-rules/",
            {"kind": "discount", "name": "X", "reason": "r", "all_services": True, "percent": "5"},
            format="json",
        )
        self.assertEqual(res.status_code, 403)

    def test_bad_percentages_and_empty_service_lists_are_refused(self):
        self.assertEqual(self.rule(percent="0").status_code, 400)
        self.assertEqual(self.rule(percent="101").status_code, 400)
        self.assertEqual(self.rule(all_services=False, service_ids=[]).status_code, 400)
        self.assertEqual(self.rule(all_services=False, service_ids=[99999]).status_code, 400)

    def test_the_same_name_twice_is_refused_but_a_refund_rule_may_share_it(self):
        self.assertEqual(self.rule().status_code, 201)
        self.assertEqual(self.rule(name="  staff ").status_code, 400)
        self.assertEqual(self.rule(kind="refund").status_code, 201)

    def test_batch_saves_all_or_none(self):
        good = {"kind": "discount", "name": "A", "reason": "a", "all_services": False, "service_ids": [self.lab.id], "percent": "5"}
        bad = {**good, "name": "B", "percent": "500"}
        self.assertEqual(self.manager.post("/api/clinic/payment-rules/batch/", {"items": [good, bad]}, format="json").status_code, 400)
        self.assertEqual(PaymentRule.objects.count(), 0)
        res = self.manager.post("/api/clinic/payment-rules/batch/", {"items": [good, {**good, "name": "B"}]}, format="json")
        self.assertEqual(res.status_code, 201, res.content)
        self.assertEqual(PaymentRule.objects.count(), 2)
        again = self.manager.post("/api/clinic/payment-rules/batch/", {"items": [good]}, format="json")
        self.assertEqual(again.status_code, 400)


class DiscountPaymentTests(BillingBase):
    def test_a_discount_rule_reduces_what_the_patient_pays(self):
        rid = self.rule(all_services=False, service_ids=[self.lab.id], percent="50").json()["id"]
        # 50% off the 200 lab line = 100 off a 500 bill
        res = self.approve(discount_rule=rid, parts=[{"method": "cash", "amount": "400"}])
        self.assertEqual(res.status_code, 201, res.content)
        self.assertEqual(PaymentTransaction.objects.get().discount_amount, Decimal("100.00"))
        self.assertEqual(billing.amount_due(Encounter.objects.get(pk=self.enc.pk)), 0)

    def test_the_payment_must_match_the_discounted_total(self):
        rid = self.rule().json()["id"]
        res = self.approve(discount_rule=rid, parts=[{"method": "cash", "amount": "500"}])
        self.assertEqual(res.status_code, 400)
        self.assertIn("450", res.json()["detail"])

    def test_plain_partial_payment_still_works(self):
        res = self.approve(amount="200", tender_method="cash")
        self.assertEqual(res.status_code, 201, res.content)
        self.assertEqual(billing.amount_due(Encounter.objects.get(pk=self.enc.pk)), Decimal("300"))


class ReceiptTests(BillingBase):
    def test_mixed_payment_gives_one_receipt_per_type_under_one_invoice(self):
        res = self.approve(parts=[{"method": "cash", "amount": "300"}, {"method": "bank", "amount": "200"}])
        self.assertEqual(res.status_code, 201, res.content)
        rows = list(PaymentTransaction.objects.order_by("id"))
        self.assertEqual([(r.tender_method, r.amount) for r in rows], [("cash", Decimal("300")), ("bank", Decimal("200"))])
        self.assertEqual(len({r.invoice_number for r in rows}), 1)
        self.assertEqual(len({r.receipt_number for r in rows}), 2)
        self.assertTrue(rows[0].invoice_number.startswith("INV-"))

    def test_an_unknown_type_is_refused(self):
        self.assertEqual(self.approve(parts=[{"method": "gold", "amount": "500"}]).status_code, 400)


class ForgivenTests(BillingBase):
    def ask(self, amount="100", reason="Cannot afford it", enc=None):
        return self.reception.post(
            "/api/clinic/payments/forgive/", {"encounter": (enc or self.enc).id, "amount": amount, "reason": reason}, format="json"
        )

    def decide(self, rid, decision="approve"):
        return self.manager.post("/api/clinic/payments/forgiveness/", {"id": rid, "decision": decision}, format="json")

    def test_negative_or_missing_reason_or_too_large_is_refused(self):
        self.assertEqual(self.ask(amount="-1").status_code, 400)
        self.assertEqual(self.ask(reason="").status_code, 400)
        self.assertEqual(self.ask(amount="9999").status_code, 400)

    def test_reception_cannot_take_payment_until_the_manager_approves(self):
        rid = self.ask().json()["id"]
        blocked = self.approve(parts=[{"method": "cash", "amount": "400"}])
        self.assertEqual(blocked.status_code, 400)
        self.assertIn("waiting", blocked.json()["detail"])
        self.assertEqual(self.decide(rid).status_code, 200)
        ok = self.approve(parts=[{"method": "cash", "amount": "400"}])
        self.assertEqual(ok.status_code, 201, ok.content)
        pay = PaymentTransaction.objects.get()
        self.assertEqual(pay.forgiven_amount, Decimal("100"))
        self.assertEqual(billing.amount_due(Encounter.objects.get(pk=self.enc.pk)), 0)

    def test_a_declined_request_blocks_payment_until_it_changes(self):
        rid = self.ask().json()["id"]
        self.decide(rid, "decline")
        blocked = self.approve(parts=[{"method": "cash", "amount": "400"}])
        self.assertEqual(blocked.status_code, 400)
        self.assertIn("declined", blocked.json()["detail"])
        self.assertEqual(self.ask(amount="50").json()["status"], "pending")
        self.assertEqual(self.ask(amount="0").json().get("cancelled"), True)
        self.assertEqual(self.approve(amount="500").status_code, 201)

    def test_any_change_to_an_approved_amount_needs_approval_again(self):
        rid = self.ask().json()["id"]
        self.decide(rid)
        again = self.ask(amount="150").json()
        self.assertEqual(again["status"], "pending")
        self.assertEqual(self.approve(parts=[{"method": "cash", "amount": "350"}]).status_code, 400)

    def test_the_same_request_twice_does_not_reset_the_approval(self):
        rid = self.ask().json()["id"]
        self.decide(rid)
        self.assertEqual(self.ask().json()["status"], "approved")

    def test_only_the_manager_decides(self):
        rid = self.ask().json()["id"]
        res = self.reception.post("/api/clinic/payments/forgiveness/", {"id": rid, "decision": "approve"}, format="json")
        self.assertEqual(res.status_code, 403)

    def test_the_request_shows_on_the_visit_with_who_asked(self):
        self.ask()
        data = self.reception.get(f"/api/clinic/encounters/{self.enc.id}/").json()
        self.assertEqual(data["forgiveness"]["status"], "pending")
        self.assertEqual(data["forgiveness"]["requested_by_name"], "b4rec")


class RefundTests(BillingBase):
    def test_only_the_manager_refunds_with_an_amount_and_reason(self):
        self.approve(amount="500")
        body = {"encounter": self.enc.id, "amount": "100", "reason": "Wrong test"}
        self.assertEqual(self.reception.post("/api/clinic/payments/refund/", body, format="json").status_code, 403)
        self.assertEqual(self.manager.post("/api/clinic/payments/refund/", {**body, "reason": ""}, format="json").status_code, 400)
        self.assertEqual(self.manager.post("/api/clinic/payments/refund/", {**body, "amount": "0"}, format="json").status_code, 400)
        self.assertEqual(self.manager.post("/api/clinic/payments/refund/", body, format="json").status_code, 201)

    def test_cannot_refund_more_than_was_paid(self):
        self.approve(amount="500")
        body = {"encounter": self.enc.id, "amount": "400", "reason": "x"}
        self.assertEqual(self.manager.post("/api/clinic/payments/refund/", body, format="json").status_code, 201)
        self.assertEqual(self.manager.post("/api/clinic/payments/refund/", body, format="json").status_code, 400)

    def test_a_refund_rule_works_out_the_amount(self):
        self.approve(amount="500")
        rid = self.rule("refund", name="Lab error", reason="Lab error", all_services=False, service_ids=[self.lab.id], percent="50").json()["id"]
        res = self.manager.post("/api/clinic/payments/refund/", {"encounter": self.enc.id, "rule": rid}, format="json")
        self.assertEqual(res.status_code, 201, res.content)
        self.assertEqual(Decimal(res.json()["amount"]), Decimal("100.00"))
        self.assertEqual(res.json()["rule_name"], "Lab error")

    def test_refunds_come_off_revenue_in_the_report(self):
        self.approve(amount="500")
        self.manager.post("/api/clinic/payments/refund/", {"encounter": self.enc.id, "amount": "120", "reason": "Overcharge"}, format="json")
        data = self.manager.get("/api/clinic/reports/").json()["billing"]
        self.assertEqual((Decimal(str(data["gross_revenue"])), Decimal(str(data["refunds"])), Decimal(str(data["net_revenue"]))),
                         (Decimal("500"), Decimal("120"), Decimal("380")))
        self.assertEqual(data["refund_rows"][0]["reason"], "Overcharge")
        self.assertEqual(data["refund_rows"][0]["by"], "b4mgr")


class PayForSomeoneElseTests(BillingBase):
    def test_one_payment_settles_other_bills_each_with_a_receipt(self):
        other = self.visit("Kebede Alemu", "M2", "E2")
        res = self.reception.post(
            "/api/clinic/payments/pay-for/",
            {"payer_encounter": self.enc.id, "encounters": [other.id], "tender_method": "cash"},
            format="json",
        )
        self.assertEqual(res.status_code, 201, res.content)
        self.assertEqual(Decimal(str(res.json()["total"])), Decimal("500"))
        pay = PaymentTransaction.objects.get()
        self.assertEqual(pay.encounter_id, other.id)
        self.assertEqual(pay.paid_by_patient_id, self.enc.patient_id)
        self.assertEqual(billing.amount_due(Encounter.objects.get(pk=other.pk)), 0)
        self.assertGreater(billing.amount_due(Encounter.objects.get(pk=self.enc.pk)), 0)

    def test_cannot_pay_your_own_bill_this_way_or_a_settled_one(self):
        res = self.reception.post(
            "/api/clinic/payments/pay-for/", {"payer_encounter": self.enc.id, "encounters": [self.enc.id]}, format="json"
        )
        self.assertEqual(res.status_code, 400)
        other = self.visit("Kebede Alemu", "M2", "E2")
        self.approve(other, amount="500")
        res = self.reception.post(
            "/api/clinic/payments/pay-for/", {"payer_encounter": self.enc.id, "encounters": [other.id]}, format="json"
        )
        self.assertEqual(res.status_code, 400)

    def test_the_report_names_payer_and_beneficiary(self):
        other = self.visit("Kebede Alemu", "M2", "E2")
        self.reception.post("/api/clinic/payments/pay-for/", {"payer_encounter": self.enc.id, "encounters": [other.id]}, format="json")
        rows = self.manager.get("/api/clinic/reports/").json()["billing"]["paid_for_others"]
        self.assertEqual((rows[0]["payer"], rows[0]["patient"]), ("Hana Bekele", "Kebede Alemu"))


class BillingReportTests(BillingBase):
    def test_discounts_forgiven_and_merged_payments_are_reported_with_who_and_why(self):
        rid = self.rule(name="Staff", reason="Staff member").json()["id"]
        self.approve(discount_rule=rid, parts=[{"method": "cash", "amount": "250"}, {"method": "bank", "amount": "200"}])
        data = self.manager.get("/api/clinic/reports/").json()["billing"]
        self.assertEqual(data["discounts"][0]["rule"], "Staff")
        self.assertEqual(data["discounts"][0]["reason"], "Staff member")
        self.assertEqual(Decimal(str(data["discount_total"])), Decimal("50"))
        self.assertEqual(len(data["merged_payments"]), 1)
        self.assertEqual(len(data["merged_payments"][0]["parts"]), 2)
        self.assertEqual(Decimal(str(data["vat_collected"])), Decimal("67.50"))

    def test_forgiven_amounts_are_reported_with_both_names(self):
        enc2 = self.visit("Sara T", "M3", "E3")
        rid = self.reception.post("/api/clinic/payments/forgive/", {"encounter": enc2.id, "amount": "100", "reason": "Hardship"}, format="json").json()["id"]
        self.manager.post("/api/clinic/payments/forgiveness/", {"id": rid, "decision": "approve"}, format="json")
        self.approve(enc2, amount="400")
        row = self.manager.get("/api/clinic/reports/").json()["billing"]["forgiven"][0]
        self.assertEqual((row["by"], row["approved_by"], row["reason"]), ("b4rec", "b4mgr", "Hardship"))
