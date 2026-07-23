import json
import os
from unittest.mock import patch

from django.test import Client, TestCase

from accounts.models import Organization
from bots.models import GoogleWorkspaceSsoSigningCertificate, Project


class GoogleWorkspaceSsoControlPlaneTests(TestCase):
    def setUp(self):
        self.environment = patch.dict(
            os.environ,
            {
                "ATTENDEE_INTERNAL_SERVICE_KEY": "test-internal-key",
                "GOOGLE_MEET_SSO_FACADE_BASE_URL": "https://api.example.test/api/v1/integrations/google-workspace/meetbot-sso",
            },
        )
        self.environment.start()
        self.addCleanup(self.environment.stop)
        organization = Organization.objects.create(name="Workspace SSO control plane organization")
        self.project = Project.objects.create(name="Shared Meetbot pool", organization=organization)
        self.client = Client()
        self.headers = {"HTTP_X_INTERNAL_SERVICE_KEY": "test-internal-key"}

    def _json_post(self, path, payload):
        return self.client.post(path, data=json.dumps(payload), content_type="application/json", **self.headers)

    def _json_patch(self, path, payload):
        return self.client.patch(path, data=json.dumps(payload), content_type="application/json", **self.headers)

    def test_control_plane_keeps_private_key_in_attendee_and_manages_rotation(self):
        list_path = "/internal/google-workspace-sso-tenants"
        self.assertEqual(self.client.get(list_path, {"project_object_id": self.project.object_id}).status_code, 401)

        create_response = self._json_post(
            list_path,
            {
                "project_object_id": self.project.object_id,
                "workspace_domain": "Example.COM.",
                "display_name": "Example Workspace",
            },
        )
        self.assertEqual(create_response.status_code, 201)
        tenant = create_response.json()
        self.assertEqual(tenant["workspace_domain"], "example.com")
        self.assertTrue(tenant["idp_entity_id"].startswith("https://api.example.test/"))
        self.assertEqual(len(tenant["certificates"]), 1)
        certificate = tenant["certificates"][0]
        self.assertIn("BEGIN CERTIFICATE", certificate["certificate_pem"])
        self.assertNotIn("private_key", certificate)

        tenant_path = f"{list_path}/{tenant['object_id']}"
        configure_response = self._json_patch(
            tenant_path,
            {
                "google_sp_entity_id": "https://accounts.google.com/o/saml2?idpid=example",
                "google_acs_url": "https://accounts.google.com/a/example.com/acs",
                "is_active": True,
            },
        )
        self.assertEqual(configure_response.status_code, 200)
        self.assertTrue(configure_response.json()["saml_ready"])

        login_response = self._json_post(
            f"{tenant_path}/bot-logins",
            {"email": "meetbot@example.com"},
        )
        self.assertEqual(login_response.status_code, 201)
        self.assertEqual(login_response.json()["bot_logins"][0]["email"], "meetbot@example.com")

        rotate_response = self._json_post(f"{tenant_path}/certificates/rotate", {})
        self.assertEqual(rotate_response.status_code, 200)
        self.assertEqual(
            {item["state"] for item in rotate_response.json()["certificates"]},
            {GoogleWorkspaceSsoSigningCertificate.States.ACTIVE, GoogleWorkspaceSsoSigningCertificate.States.NEXT},
        )

        promote_response = self._json_post(f"{tenant_path}/certificates/promote", {})
        self.assertEqual(promote_response.status_code, 200)
        self.assertEqual(
            [item["state"] for item in promote_response.json()["certificates"]],
            [GoogleWorkspaceSsoSigningCertificate.States.ACTIVE],
        )
