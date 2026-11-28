"""Several people under one role login, told apart by password, and the audit trail that names them."""
from unittest import mock

from django.contrib.auth import get_user_model
from django.test import override_settings
from rest_framework.test import APIClient, APITestCase

from api.models import AuditEvent, StaffPerson, UserProfile
from tenants.models import TenantAccount

TIN = "PERSONS-1"
BRANCH = "Main"
PERM = "tenants.permissions.TenantBillingAccessPermission.has_permission"


def _user(name, role, password="pw-default-1"):
    user = get_user_model().objects.create_user(name, password=password)
    UserProfile.objects.create(user=user, clinic_tin=TIN, role=role, branch_name=BRANCH, clinic_name="Persons Clinic")
    return user


@override_settings(NODE_MODE="cloud")
class PersonTests(APITestCase):
    def setUp(self):
        TenantAccount.objects.create(clinic_tin=TIN, is_illustration=True, setup_fee_approved=True)
        self.manager = _user("pmgr", "manager", "mgr-pass-1")
        self.nurse = _user("nursedesk", "nurse", "nurse-default")
        self.client = APIClient()
        self.client.force_authenticate(self.manager)

    # helpers
    def add(self, name, password, role="nurse", confirm=None):
        return self.client.post(
            "/api/user/persons/",
            {"full_name": name, "role": role, "password": password, "confirm_password": confirm or password},
            format="json",
        )

    def login(self, password, username="nursedesk"):
        return APIClient().post("/api/auth/login/", {"username": username, "password": password}, format="json")

    def people(self, role="nurse"):
        res = self.client.get("/api/user/persons/")
        self.assertEqual(res.status_code, 200, res.content)
        return [p for p in res.json() if p["role"] == role]

    def person_named(self, name):
        return next(p for p in self.people() if p["full_name"] == name)

    # who can sign in as whom
    def test_the_existing_role_login_gets_a_default_person(self):
        rows = self.people()
        self.assertEqual(len(rows), 1)
        self.assertTrue(rows[0]["is_default"])
        self.assertEqual(rows[0]["username"], "nursedesk")

    def test_each_person_signs_in_with_their_own_password_and_is_named(self):
        self.assertEqual(self.add("Selam Tadesse", "selam-pass").status_code, 201)
        self.assertEqual(self.add("Dawit Alemu", "dawit-pass").status_code, 201)
        names = {pw: self.login(pw).json()["person"]["name"] for pw in ("selam-pass", "dawit-pass", "nurse-default")}
        self.assertEqual(names["selam-pass"], "Selam Tadesse")
        self.assertEqual(names["dawit-pass"], "Dawit Alemu")
        self.assertEqual(names["nurse-default"], "Nurse (default)")
        self.assertEqual(self.login("wrong-password").status_code, 401)

    def test_the_person_is_carried_in_the_token(self):
        self.add("Selam Tadesse", "selam-pass")
        access = self.login("selam-pass").json()["access"]
        client = APIClient()
        client.credentials(HTTP_AUTHORIZATION=f"Bearer {access}")
        self.assertEqual(client.get("/api/user/me/").json()["person"]["name"], "Selam Tadesse")

    # duplicates
    def test_a_password_already_used_in_the_role_is_refused(self):
        self.add("Selam Tadesse", "selam-pass")
        self.assertEqual(self.add("Dawit Alemu", "selam-pass").status_code, 400)
        self.assertEqual(self.add("Dawit Alemu", "nurse-default").status_code, 400)

    def test_the_same_password_in_another_role_is_fine(self):
        _user("doctordesk", "doctor", "doc-default")
        self.assertEqual(self.add("Hana Bekele", "nurse-default", role="doctor").status_code, 201)

    def test_a_person_with_the_same_name_in_the_role_is_refused(self):
        self.add("Selam Tadesse", "selam-pass")
        res = self.add("  selam   TADESSE ", "another-pass")
        self.assertEqual(res.status_code, 400)
        self.assertIn("already registered", str(res.json()))
        _user("doctordesk", "doctor", "doc-default")
        self.assertEqual(self.add("Selam Tadesse", "doc-person-1", role="doctor").status_code, 201)

    def test_a_role_that_already_has_a_login_cannot_be_created_again(self):
        res = self.client.post(
            "/api/user/", {"username": "nurse2", "password": "pw-123456", "role": "nurse"}, format="json"
        )
        self.assertEqual(res.status_code, 400)
        self.assertIn("already exists", str(res.json()))

    def test_a_short_password_or_a_mismatch_is_refused(self):
        self.assertEqual(self.add("Selam Tadesse", "abc").status_code, 400)
        self.assertEqual(self.add("Selam Tadesse", "selam-pass", confirm="selam-other").status_code, 400)

    def test_a_person_needs_a_role_login_first(self):
        self.assertEqual(self.add("Hana Bekele", "hana-pass", role="doctor").status_code, 400)

    # editing
    def test_changing_a_password_moves_the_person_to_the_new_one(self):
        self.add("Selam Tadesse", "selam-pass")
        pid = self.person_named("Selam Tadesse")["id"]
        res = self.client.patch(
            f"/api/user/persons/{pid}/", {"password": "selam-new-pw", "confirm_password": "selam-new-pw"}, format="json"
        )
        self.assertEqual(res.status_code, 200, res.content)
        self.assertEqual(self.login("selam-pass").status_code, 401)
        self.assertEqual(self.login("selam-new-pw").json()["person"]["id"], pid)

    def test_renaming_a_person_keeps_their_identity(self):
        self.add("Selam Tadesse", "selam-pass")
        pid = self.person_named("Selam Tadesse")["id"]
        self.client.patch(f"/api/user/persons/{pid}/", {"full_name": "Selam T. Girma"}, format="json")
        self.assertEqual(self.login("selam-pass").json()["person"]["name"], "Selam T. Girma")

    def test_editing_the_default_person_changes_the_role_password(self):
        pid = self.people()[0]["id"]
        res = self.client.patch(
            f"/api/user/persons/{pid}/", {"password": "fresh-default", "confirm_password": "fresh-default"}, format="json"
        )
        self.assertEqual(res.status_code, 200, res.content)
        self.assertEqual(self.login("nurse-default").status_code, 401)
        self.assertEqual(self.login("fresh-default").json()["person"]["id"], pid)

    # deleting
    def test_deleting_a_person_ends_their_access_but_keeps_the_record(self):
        self.add("Selam Tadesse", "selam-pass")
        pid = self.person_named("Selam Tadesse")["id"]
        self.assertEqual(self.client.delete(f"/api/user/persons/{pid}/").status_code, 204)
        self.assertEqual(self.login("selam-pass").status_code, 401)
        self.assertFalse(StaffPerson.objects.get(pk=pid).is_active)
        self.assertEqual([p["full_name"] for p in self.people()], ["Nurse (default)"])

    def test_deleting_the_default_person_passes_the_role_password_to_someone_else(self):
        self.add("Selam Tadesse", "selam-pass")
        selam = self.person_named("Selam Tadesse")["id"]
        default = next(p for p in self.people() if p["is_default"])["id"]
        self.assertEqual(self.client.delete(f"/api/user/persons/{default}/").status_code, 204)
        # The old default password no longer works; Selam's own password does, and she is now the default.
        self.assertEqual(self.login("nurse-default").status_code, 401)
        res = self.login("selam-pass")
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.json()["person"]["id"], selam)  # same person row: her past activity stays hers
        self.assertTrue(res.json()["person"]["is_default"])
        self.assertTrue(get_user_model().objects.filter(username="nursedesk").exists())  # the username stays

    def test_deleting_the_last_person_locks_the_login_until_someone_is_added(self):
        default = self.people()[0]["id"]
        self.assertEqual(self.client.delete(f"/api/user/persons/{default}/").status_code, 204)
        self.assertEqual(self.login("nurse-default").status_code, 401)
        self.assertTrue(get_user_model().objects.filter(username="nursedesk").exists())
        self.assertEqual(self.add("Selam Tadesse", "selam-pass").status_code, 201)
        res = self.login("selam-pass")
        self.assertEqual(res.status_code, 200)
        self.assertTrue(res.json()["person"]["is_default"])

    def test_only_managers_manage_people(self):
        client = APIClient()
        client.force_authenticate(self.nurse)
        self.assertEqual(client.get("/api/user/persons/").status_code, 403)
        self.assertEqual(
            client.post("/api/user/persons/", {"full_name": "X Y", "role": "nurse", "password": "abcdef"}, format="json").status_code,
            403,
        )

    # audit
    def test_sign_in_is_recorded_with_the_person(self):
        self.add("Selam Tadesse", "selam-pass")
        self.login("selam-pass")
        event = AuditEvent.objects.filter(action="login").latest("id")
        self.assertEqual((event.actor_name, event.actor_role, event.actor_username), ("Selam Tadesse", "nurse", "nursedesk"))

    def test_a_change_made_through_a_screen_names_who_did_it(self):
        self.add("Selam Tadesse", "selam-pass")
        access = self.login("selam-pass").json()["access"]
        client = APIClient()
        client.credentials(HTTP_AUTHORIZATION=f"Bearer {access}")
        with mock.patch(PERM, return_value=True):
            res = client.post(
                "/api/clinic/patients/", {"full_name": "Test Patient", "age": 30, "gender": "Female", "phone": "0911"}, format="json"
            )
        self.assertEqual(res.status_code, 201, res.content)
        event = AuditEvent.objects.get(action="create", entity="patient")
        self.assertEqual(event.actor_name, "Selam Tadesse")
        self.assertEqual(event.entity_id, str(res.json()["id"]))
        self.assertEqual(event.clinic_tin, TIN)
        self.assertEqual(event.details["fields"]["full_name"], "Test Patient")

    def test_secrets_never_reach_the_audit_log(self):
        self.add("Selam Tadesse", "selam-pass")
        text = str(list(AuditEvent.objects.values_list("details", "summary")))
        self.assertNotIn("selam-pass", text)

    def test_staff_changes_are_audited(self):
        self.add("Selam Tadesse", "selam-pass")
        event = AuditEvent.objects.get(entity="staff person", action="create")
        self.assertEqual(event.actor_username, "pmgr")
        self.assertIn("Selam Tadesse", event.summary)
