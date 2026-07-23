from __future__ import annotations

import json
from urllib.parse import urlparse

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from bots.google_workspace_sso import (
    GoogleWorkspaceSsoConfigurationError,
    create_signing_certificate,
    normalize_workspace_domain,
    public_sso_facade_url,
    retire_and_promote_next_certificate,
    validate_tenant_configuration,
)
from bots.models import GoogleWorkspaceSsoSigningCertificate, GoogleWorkspaceSsoTenant, Project


class Command(BaseCommand):
    help = "Manage private Google Workspace SAML SSO tenant configuration for Meetbot."

    def add_arguments(self, parser):
        subparsers = parser.add_subparsers(dest="action", required=True)

        create_parser = subparsers.add_parser("create")
        create_parser.add_argument("--project", required=True, help="Attendee project object_id")
        create_parser.add_argument("--workspace-domain", required=True)
        create_parser.add_argument("--idp-entity-id", required=True)
        create_parser.add_argument("--display-name", default="")

        configure_parser = subparsers.add_parser("configure-sp")
        configure_parser.add_argument("--tenant", required=True, help="Workspace SSO tenant object_id")
        configure_parser.add_argument("--google-sp-entity-id", required=True)
        configure_parser.add_argument("--google-acs-url", required=True)
        configure_parser.add_argument("--enable", action="store_true")

        show_parser = subparsers.add_parser("show")
        show_parser.add_argument("--tenant", required=True, help="Workspace SSO tenant object_id")
        show_parser.add_argument("--include-certificate", action="store_true")

        rotate_parser = subparsers.add_parser("rotate")
        rotate_parser.add_argument("--tenant", required=True, help="Workspace SSO tenant object_id")

        promote_parser = subparsers.add_parser("promote")
        promote_parser.add_argument("--tenant", required=True, help="Workspace SSO tenant object_id")

    def handle(self, *args, **options):
        action = options["action"]
        if action == "create":
            return self._create(**options)
        if action == "configure-sp":
            return self._configure_sp(**options)
        if action == "show":
            return self._show(**options)
        if action == "rotate":
            return self._rotate(**options)
        if action == "promote":
            return self._promote(**options)
        raise CommandError(f"Unsupported action: {action}")

    @staticmethod
    def _tenant_or_error(object_id: str) -> GoogleWorkspaceSsoTenant:
        try:
            return GoogleWorkspaceSsoTenant.objects.get(object_id=object_id)
        except GoogleWorkspaceSsoTenant.DoesNotExist as exc:
            raise CommandError("Workspace SSO tenant was not found") from exc

    @staticmethod
    def _validate_public_idp_entity_id(value: str) -> str:
        entity_id = value.strip()
        parsed = urlparse(entity_id)
        if parsed.scheme != "https" or not parsed.netloc:
            raise CommandError("--idp-entity-id must be a stable absolute HTTPS URL")
        return entity_id

    @staticmethod
    def _validate_google_acs_url(value: str) -> str:
        acs_url = value.strip()
        parsed = urlparse(acs_url)
        if parsed.scheme != "https" or parsed.hostname != "accounts.google.com":
            raise CommandError("--google-acs-url must be an HTTPS URL on accounts.google.com")
        return acs_url

    def _configuration_payload(self, tenant: GoogleWorkspaceSsoTenant, *, include_certificate: bool) -> dict[str, object]:
        certificate = tenant.active_signing_certificate()
        payload: dict[str, object] = {
            "tenant_object_id": tenant.object_id,
            "project_object_id": tenant.project.object_id,
            "workspace_domain": tenant.workspace_domain,
            "idp_entity_id": tenant.idp_entity_id,
            "sign_in_url": public_sso_facade_url("sign-in"),
            "sign_out_url": public_sso_facade_url("sign-out"),
            "google_sp_entity_id": tenant.google_sp_entity_id,
            "google_acs_url": tenant.google_acs_url,
            "is_active": tenant.is_active,
            "saml_ready": tenant.is_saml_ready(),
            "active_certificate": (
                {
                    "object_id": certificate.object_id,
                    "fingerprint_sha256": certificate.fingerprint_sha256,
                    "not_after": certificate.not_after.isoformat() if certificate.not_after else None,
                    **({"certificate_pem": certificate.certificate_pem} if include_certificate else {}),
                }
                if certificate
                else None
            ),
            "next_certificate": next(
                (
                    {
                        "object_id": item.object_id,
                        "fingerprint_sha256": item.fingerprint_sha256,
                        "not_after": item.not_after.isoformat() if item.not_after else None,
                        **({"certificate_pem": item.certificate_pem} if include_certificate else {}),
                    }
                    for item in tenant.signing_certificates.filter(
                        state=GoogleWorkspaceSsoSigningCertificate.States.NEXT,
                        retired_at__isnull=True,
                    ).order_by("created_at", "id")
                ),
                None,
            ),
        }
        return payload

    def _write_payload(self, payload: dict[str, object]) -> None:
        self.stdout.write(json.dumps(payload, indent=2, sort_keys=True, default=str))

    def _create(self, **options):
        try:
            project = Project.objects.get(object_id=options["project"])
        except Project.DoesNotExist as exc:
            raise CommandError("Project was not found") from exc
        domain = normalize_workspace_domain(options["workspace_domain"])
        idp_entity_id = self._validate_public_idp_entity_id(options["idp_entity_id"])

        with transaction.atomic():
            if GoogleWorkspaceSsoTenant.objects.filter(project=project, workspace_domain=domain).exists():
                raise CommandError("A Workspace SSO tenant already exists for this project and domain")
            tenant = GoogleWorkspaceSsoTenant.objects.create(
                project=project,
                workspace_domain=domain,
                display_name=options["display_name"].strip(),
                idp_entity_id=idp_entity_id,
                is_active=False,
            )
            create_signing_certificate(tenant=tenant, state=GoogleWorkspaceSsoSigningCertificate.States.ACTIVE)

        self._write_payload(self._configuration_payload(tenant, include_certificate=True))

    def _configure_sp(self, **options):
        tenant = self._tenant_or_error(options["tenant"])
        tenant.google_sp_entity_id = options["google_sp_entity_id"].strip()
        tenant.google_acs_url = self._validate_google_acs_url(options["google_acs_url"])
        if options["enable"]:
            tenant.is_active = True
        tenant.save(update_fields=["google_sp_entity_id", "google_acs_url", "is_active", "updated_at"])
        try:
            validate_tenant_configuration(tenant)
        except GoogleWorkspaceSsoConfigurationError as exc:
            raise CommandError(f"Tenant configuration is not ready: {exc}") from exc
        self._write_payload(self._configuration_payload(tenant, include_certificate=False))

    def _show(self, **options):
        self._write_payload(self._configuration_payload(self._tenant_or_error(options["tenant"]), include_certificate=options["include_certificate"]))

    def _rotate(self, **options):
        tenant = self._tenant_or_error(options["tenant"])
        with transaction.atomic():
            if tenant.signing_certificates.filter(
                state=GoogleWorkspaceSsoSigningCertificate.States.NEXT,
                retired_at__isnull=True,
            ).exists():
                raise CommandError("A next signing certificate already exists; upload it to Google or promote it before rotating again")
            create_signing_certificate(tenant=tenant, state=GoogleWorkspaceSsoSigningCertificate.States.NEXT)
        self._write_payload(self._configuration_payload(tenant, include_certificate=True))

    def _promote(self, **options):
        tenant = self._tenant_or_error(options["tenant"])
        with transaction.atomic():
            retire_and_promote_next_certificate(tenant=tenant)
        self._write_payload(self._configuration_payload(tenant, include_certificate=False))
