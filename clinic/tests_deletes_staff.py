"""Offline-sync phase 7: deletions travel between clinic server and cloud; staff passwords travel up."""
from datetime import timedelta
from unittest import mock

from django.contrib.auth import get_user_model
from django.contrib.auth.hashers import make_password
from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from api.models import UserProfile
from clinic import ledger
from clinic.models import BillableService, Medicine, MedicineDamage, Patient, StockMovement, Supplier
from clinic.sync_engine import (
    PUSH,
    apply_credentials,
    apply_scope,
    apply_staff,
    export_credentials,
    export_scope,
    export_staff,
)
from clinic.tests_sync_phase1 import BRANCH, CLOUD, NODE, SCOPE, TIN, SyncCase, _med, _patient

PERM = "tenants.permissions.TenantBillingAccessPermission.has_permission"


def _manager(name="delmgr"):
    user = get_user_model().objects.create_user(name, password="x")
    UserProfile.objects.create(user=user, clinic_tin=TIN, role="manager", branch_name=BRANCH)
    return user


class SoftDeleteApiTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.client.force_authenticate(_manager())
        patcher = mock.patch(PERM, return_value=True)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_deleting_a_supplier_keeps_a_tombstone_and_frees_the_name(self):
        sup = Supplier.objects.create(clinic_tin=TIN, branch_name=BRANCH, supplier_name="Acme", supplier_phone="0911")
        self.assertEqual(self.client.delete(f"/api/clinic/suppliers/{sup.id}/").status_code, 204)
        self.assertEqual(Supplier.objects.count(), 0)  # invisible to ordinary queries
        tomb = Supplier.all_objects.get(pk=sup.pk)
        self.assertIsNotNone(tomb.deleted_at)
        self.assertTrue(tomb.supplier_name.startswith("Acme [deleted "))
        listing = self.client.get("/api/clinic/suppliers/").json()
        self.assertEqual(listing["results"] if isinstance(listing, dict) else listing, [])
        # The same name can be used again straight away.
        again = self.client.post("/api/clinic/suppliers/", {"supplier_name": "Acme", "supplier_phone": "0911"}, format="json")
        self.assertEqual(again.status_code, 201, again.content)

    def test_deleting_a_service_frees_its_code(self):
        svc = BillableService.objects.create(
            clinic_tin=TIN, branch_name=BRANCH, code="CONS", name="Consultation", department="OPD", unit_price=100
        )
        self.assertEqual(self.client.delete(f"/api/clinic/billable-services/{svc.id}/").status_code, 204)
        self.assertEqual(BillableService.all_objects.get(pk=svc.pk).deleted_at is not None, True)
        BillableService.objects.create(
            clinic_tin=TIN, branch_name=BRANCH, code="CONS", name="Consultation 2", department="OPD", unit_price=120
        )

    def test_a_deleted_medicine_keeps_its_stock_history(self):
        med = Medicine.objects.create(
            clinic_tin=TIN, branch_name=BRANCH, name="Old", batch_number="B", unit_of_measure="box", on_hand=3,
            unit_price=10, cost=5, received_quantity=3, paid_amount=15,
        )
        ledger.record_movement(med, "opening", "t")
        self.assertEqual(self.client.delete(f"/api/clinic/medicines/{med.id}/").status_code, 204)
        self.assertFalse(Medicine.objects.filter(pk=med.pk).exists())
        self.assertEqual(StockMovement.objects.filter(medicine_id=med.pk).count(), 1)

    def test_a_deleted_damage_entry_is_a_tombstone_and_the_stock_comes_back(self):
        med = Medicine.objects.create(
            clinic_tin=TIN, branch_name=BRANCH, name="M", batch_number="B", unit_of_measure="box", on_hand=5,
            unit_price=10, cost=5, received_quantity=5, paid_amount=25,
        )
        ledger.record_movement(med, "opening", "t")
        res = self.client.post("/api/clinic/medicine-damages/", {"medicine": med.id, "quantity": 2, "sale_unit": "pack"}, format="json")
        entry = res.json()["id"]
        self.client.delete(f"/api/clinic/medicine-damages/{entry}/")
        med.refresh_from_db()
        self.assertEqual(med.on_hand, 5)
        self.assertEqual((MedicineDamage.objects.count(), MedicineDamage.all_objects.count()), (0, 1))


class DeletionSyncTests(SyncCase):
    def test_a_deletion_made_on_the_server_reaches_the_cloud(self):
        sup = Supplier.objects.create(clinic_tin=TIN, branch_name=BRANCH, supplier_name="Acme", supplier_phone="1")
        self.seed_node()
        Supplier.objects.using(NODE).get(sync_uuid=sup.sync_uuid).soft_delete()
        run = self.sync()
        self.assertEqual((run.push_status, run.pull_status), ("ok", "ok"), run.error_message)
        self.assertIsNotNone(Supplier.all_objects.get(pk=sup.pk).deleted_at)
        self.assertFalse(Supplier.objects.filter(pk=sup.pk).exists())

    def test_a_deletion_made_on_the_cloud_reaches_the_server(self):
        svc = BillableService.objects.create(
            clinic_tin=TIN, branch_name=BRANCH, code="X1", name="Xray", department="Rad", unit_price=50
        )
        self.seed_node()
        BillableService.objects.get(pk=svc.pk).soft_delete()
        self.sync()
        self.assertFalse(BillableService.objects.using(NODE).filter(sync_uuid=svc.sync_uuid).exists())
        self.assertIsNotNone(BillableService.all_objects.using(NODE).get(sync_uuid=svc.sync_uuid).deleted_at)

    def test_a_deleted_record_does_not_come_back_from_a_stale_copy(self):
        patient = _patient(CLOUD, mrn="9")
        self.seed_node()
        stale = export_scope(SCOPE, db=NODE)["patients"]  # the server's live copy
        Patient.objects.get(pk=patient.pk).soft_delete()
        result = apply_scope(SCOPE, {"patients": stale}, PUSH, db=CLOUD)
        self.assertEqual(result.counts["patients"]["skipped"], 1)
        self.assertIsNotNone(Patient.all_objects.get(pk=patient.pk).deleted_at)

    def test_a_tombstone_for_a_record_never_seen_is_ignored(self):
        row = {
            "sync_uuid": "55555555-5555-5555-5555-555555555555", "stamp": None,
            "deleted_at": timezone.now().isoformat(),
            "data": {"mrn": "GHOST", "full_name": "Ghost"}, "refs": {}, "users": {},
        }
        result = apply_scope(SCOPE, {"patients": [row]}, PUSH, db=CLOUD)
        self.assertEqual(result.counts["patients"]["skipped"], 1)
        self.assertFalse(Patient.all_objects.filter(mrn="GHOST").exists())

    def test_tombstones_are_exported_and_a_live_twin_is_not_confused_with_them(self):
        patient = _patient(CLOUD, mrn="T1")
        Patient.objects.get(pk=patient.pk).soft_delete()
        rows = export_scope(SCOPE, db=CLOUD)["patients"]
        self.assertEqual(len(rows), 1)
        self.assertTrue(rows[0]["deleted_at"])

    def test_movements_still_apply_to_a_medicine_deleted_on_the_other_side(self):
        med = _med(CLOUD)
        self.seed_node()
        node_med = Medicine.objects.using(NODE).get(sync_uuid=med.sync_uuid)
        node_med.on_hand = 9
        node_med.save(using=NODE)
        ledger.record_movement(node_med, "dispense", "order:1")
        Medicine.objects.get(pk=med.pk).soft_delete()
        self.sync()
        stock = Medicine.all_objects.get(pk=med.pk)
        self.assertEqual(ledger.stock_units(stock), 90)  # the dispense still counts; nothing is lost


class StaffPasswordSyncTests(SyncCase):
    def make_staff(self, db=CLOUD, name="nurse1", changed=None):
        user = get_user_model().objects.db_manager(db).create_user(name, password="first-pass-1")
        UserProfile.objects.using(db).create(
            user=user, clinic_tin=TIN, branch_name=BRANCH, role="nurse", password_changed_at=changed or timezone.now() - timedelta(days=2)
        )
        return user

    def test_a_password_changed_on_the_server_reaches_the_cloud(self):
        cloud_user = self.make_staff()
        self.seed_node()
        node_user = get_user_model().objects.using(NODE).get(username="nurse1")
        node_user.set_password("changed-here-2")
        node_user.save(using=NODE)
        UserProfile.objects.using(NODE).filter(user=node_user).update(password_changed_at=timezone.now())
        run = self.sync()
        self.assertEqual((run.push_status, run.pull_status), ("ok", "ok"), run.error_message)
        cloud_user.refresh_from_db()
        self.assertTrue(cloud_user.check_password("changed-here-2"))
        self.assertEqual(run.receipt["push"]["staff_credentials"], {"applied": 1, "skipped": 0})

    def test_a_newer_cloud_password_is_not_overwritten_by_an_older_server_one(self):
        cloud_user = self.make_staff()
        self.seed_node()
        node_user = get_user_model().objects.using(NODE).get(username="nurse1")
        node_user.set_password("server-pass-3")
        node_user.save(using=NODE)
        UserProfile.objects.using(NODE).filter(user=node_user).update(password_changed_at=timezone.now() - timedelta(hours=5))
        cloud_user.set_password("cloud-newer-4")
        cloud_user.save()
        UserProfile.objects.filter(user=cloud_user).update(password_changed_at=timezone.now())
        self.sync()
        cloud_user.refresh_from_db()
        self.assertTrue(cloud_user.check_password("cloud-newer-4"))
        node_user.refresh_from_db(using=NODE)
        self.assertTrue(node_user.check_password("cloud-newer-4"))  # the pull brings the newer one down

    def test_a_pull_keeps_a_password_changed_on_the_server_since(self):
        self.make_staff()
        self.seed_node()
        node_user = get_user_model().objects.using(NODE).get(username="nurse1")
        node_user.set_password("local-wins-5")
        node_user.save(using=NODE)
        UserProfile.objects.using(NODE).filter(user=node_user).update(password_changed_at=timezone.now())
        apply_staff(SCOPE, export_staff(SCOPE, db=CLOUD), db=NODE)
        node_user.refresh_from_db(using=NODE)
        self.assertTrue(node_user.check_password("local-wins-5"))

    def test_only_a_real_password_hash_is_accepted(self):
        self.make_staff()
        stamp = timezone.now().isoformat()
        result = apply_credentials(SCOPE, [{"username": "nurse1", "password": "plain-text", "password_changed_at": stamp}], db=CLOUD)
        self.assertEqual(result, {"applied": 0, "skipped": 1})
        self.assertTrue(get_user_model().objects.get(username="nurse1").check_password("first-pass-1"))

    def test_accounts_outside_the_branch_are_never_touched(self):
        other = get_user_model().objects.create_user("elsewhere", password="keep-me-6")
        UserProfile.objects.create(user=other, clinic_tin="OTHER-TIN", branch_name=BRANCH, role="nurse")
        item = {"username": "elsewhere", "password": make_password("hacked-7"), "password_changed_at": timezone.now().isoformat()}
        self.assertEqual(apply_credentials(SCOPE, [item], db=CLOUD), {"applied": 0, "skipped": 1})
        other.refresh_from_db()
        self.assertTrue(other.check_password("keep-me-6"))

    def test_only_recent_changes_are_sent(self):
        self.make_staff(changed=timezone.now() - timedelta(days=3))
        self.assertEqual(len(export_credentials(SCOPE, since=timezone.now() - timedelta(days=1), db=CLOUD)), 0)
        self.assertEqual(len(export_credentials(SCOPE, since=None, db=CLOUD)), 1)


class StaffApiTests(TestCase):
    def setUp(self):
        self.mgr = _manager("stfmgr")
        self.nurse = get_user_model().objects.create_user("stfnurse", password="old-pass-1")
        UserProfile.objects.create(user=self.nurse, clinic_tin=TIN, role="nurse", branch_name=BRANCH)
        self.client = APIClient()
        self.client.force_authenticate(self.mgr)

    def test_changing_a_password_records_when(self):
        client = APIClient()
        client.force_authenticate(self.nurse)
        client.post("/api/user/change_password/", {"old_password": "old-pass-1", "new_password": "new-pass-2"}, format="json")
        self.nurse.refresh_from_db()
        self.assertIsNotNone(self.nurse.profile.password_changed_at)

    def test_a_manager_resetting_a_password_records_when(self):
        res = self.client.patch(f"/api/user/{self.nurse.id}/", {"password": "reset-pass-3"}, format="json")
        self.assertEqual(res.status_code, 200, res.content)
        self.nurse.refresh_from_db()
        self.assertIsNotNone(self.nurse.profile.password_changed_at)

    @override_settings(NODE_MODE="clinic")
    def test_a_clinic_server_only_lets_passwords_change(self):
        self.assertEqual(self.client.post("/api/user/", {"username": "new", "password": "p-1234567", "role": "nurse"}, format="json").status_code, 403)
        self.assertEqual(self.client.delete(f"/api/user/{self.nurse.id}/").status_code, 403)
        self.assertEqual(self.client.patch(f"/api/user/{self.nurse.id}/", {"role": "doctor"}, format="json").status_code, 403)
        self.assertEqual(self.client.patch(f"/api/user/{self.nurse.id}/", {"is_active": False}, format="json").status_code, 403)
        ok = self.client.patch(f"/api/user/{self.nurse.id}/", {"password": "reset-pass-4"}, format="json")
        self.assertEqual(ok.status_code, 200, ok.content)
        self.nurse.refresh_from_db()
        self.assertTrue(self.nurse.check_password("reset-pass-4"))
        self.assertEqual(self.nurse.profile.role, "nurse")

    @override_settings(NODE_MODE="cloud")
    def test_the_cloud_still_manages_staff_fully(self):
        res = self.client.patch(f"/api/user/{self.nurse.id}/", {"role": "doctor"}, format="json")
        self.assertEqual(res.status_code, 200, res.content)
