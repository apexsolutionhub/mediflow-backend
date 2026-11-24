"""Offline-sync phase 0: stock ledger, sync identity columns, ops-mode seed rule, generated TIN."""
from unittest import mock

from django.contrib.auth import get_user_model
from django.utils import timezone
from rest_framework.test import APITestCase

from api.models import UserProfile
from clinic import ledger
from clinic.models import Medicine, Patient, StockMovement, Supplier
from tenants.models import TenantAccount, TenantOpsModeChangeRequest
from tenants.ops_mode_views import (
    active_ops_mode_block,
    awaiting_step,
    complete_ops_mode_switch,
    is_immediate_transition,
)
from tenants.services import generate_clinic_tin

TIN = "SYNC-TIN-1"
PERM = "tenants.permissions.TenantBillingAccessPermission.has_permission"


def _user(name, role):
    user = get_user_model().objects.create_user(name, password="x")
    UserProfile.objects.create(user=user, clinic_tin=TIN, role=role, branch_name="Main")
    return user


def _line(supplier, **over):
    base = {
        "name": "Paracetamol",
        "category": "pain_fever",
        "supplier_id": supplier.id,
        "unit_price": "100",
        "cost": "60",
        "on_hand": 5,
        "unit_of_measure": "box",
        "batch_number": "B1",
        "expiry_date": "2030-01-31",
        "pack_size": 10,
        "loose_unit": "Tablet",
    }
    base.update(over)
    return base


@mock.patch(PERM, return_value=True)
class StockLedgerTests(APITestCase):
    def setUp(self):
        self.mgr = _user("lmgr", "manager")
        self.sup = Supplier.objects.create(
            clinic_tin=TIN, branch_name="Main", supplier_name="S", supplier_phone="0911"
        )
        self.client.force_authenticate(self.mgr)

    def register(self, **over):
        res = self.client.post("/api/clinic/medicines/", _line(self.sup, **over), format="json")
        self.assertEqual(res.status_code, 201, res.content)
        return Medicine.objects.get(pk=res.json()["id"])

    def assertBalanced(self, med):
        med.refresh_from_db()
        self.assertEqual(ledger.ledger_total(med), ledger.stock_units(med))
        self.assertEqual(ledger.rebuild_stock(med), (med.on_hand, med.loose_stock))

    def test_registering_a_medicine_writes_a_receive_movement(self, _perm):
        med = self.register()
        move = StockMovement.objects.get(medicine=med)
        self.assertEqual((move.kind, move.delta, move.pack_size), ("receive", 50, 10))
        self.assertBalanced(med)

    def test_unsplit_medicine_counts_in_packs(self, _perm):
        med = self.register(pack_size=1, loose_unit="")
        self.assertEqual(StockMovement.objects.get(medicine=med).delta, 5)
        self.assertBalanced(med)

    def test_damage_edit_and_delete_keep_the_ledger_balanced(self, _perm):
        med = self.register()
        res = self.client.post(
            "/api/clinic/medicine-damages/",
            {"medicine": med.id, "quantity": 3, "sale_unit": "loose"},
            format="json",
        )
        self.assertEqual(res.status_code, 201)
        self.assertBalanced(med)
        self.assertEqual(StockMovement.objects.filter(medicine=med, kind="damage").count(), 1)
        entry = res.json()["id"]
        self.client.patch(f"/api/clinic/medicine-damages/{entry}/", {"quantity": 12}, format="json")
        self.assertBalanced(med)
        self.client.delete(f"/api/clinic/medicine-damages/{entry}/")
        self.assertBalanced(med)
        med.refresh_from_db()
        self.assertEqual((med.on_hand, med.loose_stock), (5, 0))

    def test_manager_edit_records_receive_or_adjust(self, _perm):
        med = self.register()
        self.client.patch(f"/api/clinic/medicines/{med.id}/", {"on_hand": 8}, format="json")
        self.client.patch(f"/api/clinic/medicines/{med.id}/", {"on_hand": 6}, format="json")
        kinds = list(StockMovement.objects.filter(medicine=med).values_list("kind", "delta"))
        self.assertEqual(kinds, [("receive", 50), ("receive", 30), ("adjust", -20)])
        self.assertBalanced(med)

    def test_no_change_writes_no_movement(self, _perm):
        med = self.register()
        self.client.patch(f"/api/clinic/medicines/{med.id}/", {"description": "x"}, format="json")
        self.assertEqual(StockMovement.objects.filter(medicine=med).count(), 1)


class SyncIdentityTests(APITestCase):
    def test_new_rows_get_a_unique_sync_uuid_and_updated_at(self):
        a = Patient.objects.create(clinic_tin=TIN, branch_name="Main", mrn="1", full_name="A")
        b = Patient.objects.create(clinic_tin=TIN, branch_name="Main", mrn="2", full_name="B")
        self.assertIsNotNone(a.sync_uuid)
        self.assertNotEqual(a.sync_uuid, b.sync_uuid)
        self.assertIsNotNone(a.updated_at)
        self.assertIsNone(a.deleted_at)

    def test_updated_at_moves_on_save(self):
        p = Patient.objects.create(clinic_tin=TIN, branch_name="Main", mrn="1", full_name="A")
        before = p.updated_at
        p.full_name = "A2"
        p.save()
        p.refresh_from_db()
        self.assertGreater(p.updated_at, before)


class OpsModeSeedRuleTests(APITestCase):
    def setUp(self):
        self.tenant = TenantAccount.objects.create(clinic_tin=TIN, ops_mode="online")

    def approved(self, current, requested):
        return TenantOpsModeChangeRequest.objects.create(
            clinic_tin=TIN,
            current_ops_mode=current,
            requested_ops_mode=requested,
            status=TenantOpsModeChangeRequest.STATUS_APPROVED,
            reviewed_at=timezone.now(),
        )

    def test_no_transition_is_immediate(self):
        self.assertFalse(is_immediate_transition("online", "offline"))
        self.assertFalse(is_immediate_transition("offline", "online"))

    def test_online_to_offline_waits_for_the_seed(self):
        req = self.approved("online", "offline")
        self.assertEqual(active_ops_mode_block(self.tenant), req)
        self.assertEqual(awaiting_step(req), "seed")
        self.tenant.refresh_from_db()
        self.assertEqual(self.tenant.ops_mode, "online")

        applied = complete_ops_mode_switch(self.tenant)
        self.assertEqual(applied.status, "applied")
        self.assertIsNotNone(applied.applied_at)
        self.tenant.refresh_from_db()
        self.assertEqual(self.tenant.ops_mode, "offline")
        self.assertIsNone(active_ops_mode_block(self.tenant))

    def test_offline_to_online_waits_for_the_sync(self):
        self.tenant.ops_mode = "offline"
        self.tenant.save()
        req = self.approved("offline", "online")
        self.assertEqual(awaiting_step(req), "sync")
        complete_ops_mode_switch(self.tenant)
        self.tenant.refresh_from_db()
        self.assertEqual(self.tenant.ops_mode, "online")

    def test_nothing_waiting_is_a_noop(self):
        self.assertIsNone(complete_ops_mode_switch(self.tenant))


class GeneratedTinTests(APITestCase):
    def test_generated_tins_are_unique_and_tagged(self):
        seen = {generate_clinic_tin() for _ in range(20)}
        self.assertEqual(len(seen), 20)
        self.assertTrue(all(t.startswith("GEN-") and len(t) == 14 for t in seen))

    def test_a_taken_tin_is_never_reused(self):
        TenantAccount.objects.create(clinic_tin="GEN-AAAAAAAAAA")
        with mock.patch("secrets.token_hex", side_effect=["aaaaaaaaaa", "bbbbbbbbbb"]):
            self.assertEqual(generate_clinic_tin(), "GEN-BBBBBBBBBB")
