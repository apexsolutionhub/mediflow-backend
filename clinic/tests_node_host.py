"""Offline-sync phase 3: a clinic server serves the web app, guards what it refuses, and starts cleanly."""
import importlib.util
import json
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from io import StringIO
from pathlib import Path
from unittest import mock

from django.core.management import call_command
from django.core.management.base import CommandError
from django.http import Http404, HttpResponse
from django.test import RequestFactory, SimpleTestCase, TestCase, override_settings

from clinic import frontend
from clinic.models import SyncRun
from clinic.node_middleware import ClinicNodeGuardMiddleware

STARTER = Path(__file__).resolve().parents[2] / "clinic-node" / "start_clinic.py"


def _load_starter():
    spec = importlib.util.spec_from_file_location("start_clinic", STARTER)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class FrontendServingTests(SimpleTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        base = Path(self.tmp.name)
        self.root = base / "dist-node"
        (self.root / "manager").mkdir(parents=True)
        (self.root / "_next" / "static").mkdir(parents=True)
        (self.root / "index.html").write_text("<h1>home</h1>")
        (self.root / "manager" / "index.html").write_text("<h1>manager</h1>")
        (self.root / "billing.html").write_text("<h1>billing</h1>")
        (self.root / "404.html").write_text("<h1>nope</h1>")
        (self.root / "_next" / "static" / "app.js").write_text("console.log(1)")
        (base / "secret.txt").write_text("do not serve")
        self.factory = RequestFactory()

    def get(self, path):
        with override_settings(FRONTEND_DIR=str(self.root)):
            response = frontend.serve_frontend(self.factory.get("/" + path), path=path)
        self.addCleanup(response.close)  # release the open file (Windows cannot delete it otherwise)
        return response

    def body(self, response):
        return b"".join(response.streaming_content).decode()

    def test_the_root_and_pages_resolve(self):
        self.assertIn("home", self.body(self.get("")))
        self.assertIn("manager", self.body(self.get("manager/")))
        self.assertIn("manager", self.body(self.get("manager")))  # no trailing slash
        self.assertIn("billing", self.body(self.get("billing")))  # /billing -> billing.html

    def test_assets_are_cached_for_good_and_pages_are_not(self):
        asset = self.get("_next/static/app.js")
        self.assertEqual(asset.status_code, 200)
        self.assertIn("immutable", asset["Cache-Control"])
        self.assertEqual(self.get("")["Cache-Control"], "no-cache")

    def test_an_unknown_page_is_the_apps_404(self):
        res = self.get("nothing-here")
        self.assertEqual(res.status_code, 404)
        self.assertIn("nope", self.body(res))

    def test_nothing_outside_the_app_folder_is_served(self):
        for sneaky in ("../secret.txt", "..%2Fsecret.txt", "manager/../../secret.txt", "/../secret.txt"):
            res = self.get(sneaky)
            self.assertEqual(res.status_code, 404, sneaky)
            self.assertNotIn("do not serve", self.body(res), sneaky)
        self.assertIsNone(frontend.resolve_file("../secret.txt", self.root))

    def test_a_missing_api_route_is_json_not_a_page(self):
        res = self.get("api/does-not-exist/")
        self.assertEqual(res.status_code, 404)
        self.assertEqual(json.loads(res.content), {"detail": "Not found."})

    def test_only_get_is_served(self):
        with override_settings(FRONTEND_DIR=str(self.root)):
            res = frontend.serve_frontend(self.factory.post("/manager/"), path="manager/")
        self.assertEqual(res.status_code, 405)

    def test_a_server_without_the_app_says_so(self):
        with override_settings(FRONTEND_DIR=str(self.root / "missing")):
            with self.assertRaises(Http404):
                frontend.serve_frontend(self.factory.get("/"), path="")


class NodeGuardTests(SimpleTestCase):
    def call(self, request):
        middleware = ClinicNodeGuardMiddleware(lambda _r: HttpResponse("through"))
        return middleware(request)

    @override_settings(NODE_MODE="clinic")
    def test_a_clinic_server_refuses_the_apex_admin_and_clinic_signup(self):
        factory = RequestFactory()
        self.assertEqual(self.call(factory.get("/admin/login/")).status_code, 403)
        self.assertEqual(self.call(factory.post("/api/user/")).status_code, 403)
        self.assertEqual(self.call(factory.post("/api/user")).status_code, 403)

    @override_settings(NODE_MODE="clinic")
    def test_a_signed_in_manager_can_still_use_the_user_api(self):
        factory = RequestFactory()
        request = factory.post("/api/user/", HTTP_AUTHORIZATION="Bearer abc")
        self.assertEqual(self.call(request).status_code, 200)
        self.assertEqual(self.call(factory.get("/api/user/")).status_code, 200)
        self.assertEqual(self.call(factory.post("/api/auth/login/")).status_code, 200)

    @override_settings(NODE_MODE="cloud")
    def test_the_cloud_is_untouched(self):
        factory = RequestFactory()
        self.assertEqual(self.call(factory.get("/admin/login/")).status_code, 200)
        self.assertEqual(self.call(factory.post("/api/user/")).status_code, 200)


class HealthTests(TestCase):
    @override_settings(NODE_MODE="cloud")
    def test_the_cloud_health_check_is_unchanged(self):
        res = self.client.get("/api/health/")
        self.assertEqual((res.status_code, res.json()), (200, {"status": "ok"}))

    @override_settings(NODE_MODE="clinic")
    def test_a_clinic_server_reports_its_database(self):
        res = self.client.get("/api/health/")
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.json(), {"status": "ok", "node_mode": "clinic", "database": True})

    @override_settings(NODE_MODE="clinic")
    def test_a_clinic_server_with_no_database_is_not_healthy(self):
        with mock.patch("django.db.connection.ensure_connection", side_effect=Exception("down")):
            res = self.client.get("/api/health/")
        self.assertEqual(res.status_code, 503)
        self.assertFalse(res.json()["database"])


class NodeSyncCommandTests(SimpleTestCase):
    def run_command(self, *args):
        out = StringIO()
        call_command("node_sync", *args, stdout=out)
        return out.getvalue()

    @override_settings(NODE_MODE="cloud")
    def test_it_refuses_to_run_on_the_cloud(self):
        with self.assertRaisesMessage(CommandError, "not a clinic server"):
            self.run_command()

    @override_settings(NODE_MODE="clinic", CLOUD_API_URL="", NODE_TOKEN="", NODE_CLINIC_TIN="")
    def test_it_says_what_is_missing(self):
        with self.assertRaisesMessage(CommandError, "CLOUD_API_URL, NODE_TOKEN, NODE_CLINIC_TIN"):
            self.run_command("--seed")

    @override_settings(
        NODE_MODE="clinic", CLOUD_API_URL="https://c.example/api", NODE_TOKEN="t", NODE_CLINIC_TIN="T1", NODE_BRANCH="Main"
    )
    def test_a_seed_runs_with_the_servers_own_scope(self):
        ok = mock.Mock(push_status="skipped", pull_status="ok", error_message="")
        with mock.patch("clinic.management.commands.node_sync.run_sync", return_value=ok) as run:
            out = self.run_command("--seed")
        self.assertIn("Seed complete", out)
        scope, = run.call_args.args[:1]
        self.assertEqual((scope.tin, scope.branch), ("T1", "Main"))
        self.assertEqual(run.call_args.kwargs["kind"], SyncRun.KIND_SEED)

    @override_settings(
        NODE_MODE="clinic", CLOUD_API_URL="https://c.example/api", NODE_TOKEN="t", NODE_CLINIC_TIN="T1"
    )
    def test_a_failed_sync_is_an_error_with_the_reason(self):
        bad = mock.Mock(push_status="failed", pull_status="idle", error_message="The cloud is unreachable.")
        with mock.patch("clinic.management.commands.node_sync.run_sync", return_value=bad):
            with self.assertRaisesMessage(CommandError, "unreachable"):
                self.run_command()


class _Health(BaseHTTPRequestHandler):
    payload = {"status": "ok", "database": True}
    code = 200

    def do_GET(self):
        body = json.dumps(self.payload).encode()
        self.send_response(self.code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


class StarterTests(SimpleTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.starter = _load_starter()

    def serve(self, payload, code=200):
        handler = type("H", (_Health,), {"payload": payload, "code": code})
        server = HTTPServer(("127.0.0.1", 0), handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return server.server_address[1]

    def test_env_file_parsing(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / ".env"
            path.write_text('# comment\n\nNODE_PORT=8080\nDB_PASSWORD="p=ss word"\nBAD LINE\nHOST = 10.0.0.5\n')
            values = self.starter.load_env(path)
        self.assertEqual(values, {"NODE_PORT": "8080", "DB_PASSWORD": "p=ss word", "HOST": "10.0.0.5"})
        self.assertEqual(self.starter.load_env(Path("/nope/.env")), {})

    def test_the_clinic_address(self):
        self.assertEqual(self.starter.clinic_url(80), "http://127.0.0.1/")
        self.assertEqual(self.starter.clinic_url(8080, "192.168.10.10"), "http://192.168.10.10:8080/")

    def test_health_check_passes_only_with_a_working_database(self):
        ok = self.serve({"status": "ok", "database": True})
        self.assertTrue(self.starter.wait_for_health(f"http://127.0.0.1:{ok}/", timeout=3, interval=0.1))
        no_db = self.serve({"status": "degraded", "database": False}, code=503)
        self.assertFalse(self.starter.wait_for_health(f"http://127.0.0.1:{no_db}/", timeout=1, interval=0.1))
        self.assertFalse(self.starter.wait_for_health("http://127.0.0.1:1/", timeout=1, interval=0.1))

    def test_port_checks(self):
        port = self.serve({"status": "ok", "database": True})
        self.assertTrue(self.starter.port_open("127.0.0.1", port))
        self.assertTrue(self.starter.ensure_mysql("127.0.0.1", port, "MySQL80", wait=1))
        self.assertFalse(self.starter.port_open("127.0.0.1", 1, timeout=0.3))
