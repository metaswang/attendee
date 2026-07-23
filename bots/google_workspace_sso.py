from __future__ import annotations

import hashlib
import hmac
import os
from dataclasses import dataclass
from datetime import timedelta
from urllib.parse import urlparse

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID
from django.core.exceptions import ValidationError
from django.utils import timezone

from bots.models import GoogleMeetBotLogin, GoogleWorkspaceSsoSigningCertificate, GoogleWorkspaceSsoTenant


class GoogleWorkspaceSsoConfigurationError(ValueError):
    """Raised when a Workspace tenant cannot safely issue a SAML assertion."""


@dataclass(frozen=True)
class GoogleWorkspaceSsoSessionContext:
    login: GoogleMeetBotLogin
    tenant: GoogleWorkspaceSsoTenant
    signing_certificate: GoogleWorkspaceSsoSigningCertificate


def normalize_workspace_domain(value: str) -> str:
    domain = str(value or "").strip().lower().rstrip(".")
    if not domain or "." not in domain or "/" in domain or "@" in domain or any(char.isspace() for char in domain):
        raise ValidationError("Workspace domain must be a valid DNS domain")
    return domain


def public_sso_facade_url(path: str) -> str:
    base_url = os.getenv("GOOGLE_MEET_SSO_FACADE_BASE_URL", "").strip().rstrip("/")
    if not base_url:
        raise GoogleWorkspaceSsoConfigurationError("GOOGLE_MEET_SSO_FACADE_BASE_URL is not configured")
    parsed = urlparse(base_url)
    if parsed.scheme != "https" or not parsed.netloc:
        raise GoogleWorkspaceSsoConfigurationError("GOOGLE_MEET_SSO_FACADE_BASE_URL must be an absolute HTTPS URL")
    return f"{base_url}/{path.lstrip('/')}"


def validate_tenant_configuration(tenant: GoogleWorkspaceSsoTenant) -> GoogleWorkspaceSsoSigningCertificate:
    if not tenant.is_active:
        raise GoogleWorkspaceSsoConfigurationError("Workspace SSO tenant is inactive")

    required_values = {
        "idp_entity_id": tenant.idp_entity_id,
        "google_sp_entity_id": tenant.google_sp_entity_id,
        "google_acs_url": tenant.google_acs_url,
    }
    missing = [name for name, value in required_values.items() if not str(value or "").strip()]
    if missing:
        raise GoogleWorkspaceSsoConfigurationError(f"Workspace SSO tenant is missing: {', '.join(missing)}")

    idp_entity_id = urlparse(tenant.idp_entity_id)
    if idp_entity_id.scheme != "https" or not idp_entity_id.netloc:
        raise GoogleWorkspaceSsoConfigurationError("IdP Entity ID must be an absolute HTTPS URL")

    acs_url = urlparse(tenant.google_acs_url)
    if acs_url.scheme != "https" or acs_url.hostname != "accounts.google.com":
        raise GoogleWorkspaceSsoConfigurationError("Google ACS URL must be an HTTPS accounts.google.com URL")

    certificate = tenant.active_signing_certificate()
    if certificate is None or certificate.is_expired() or not certificate.certificate_pem or not certificate.private_key_pem:
        raise GoogleWorkspaceSsoConfigurationError("Workspace SSO tenant has no active signing certificate")
    try:
        parsed_certificate = x509.load_pem_x509_certificate(certificate.certificate_pem.encode("ascii"))
        parsed_private_key = serialization.load_pem_private_key(certificate.private_key_pem.encode("ascii"), password=None)
    except (TypeError, ValueError) as exc:
        raise GoogleWorkspaceSsoConfigurationError("Workspace SSO tenant has an invalid signing certificate or private key") from exc
    certificate_public_key = parsed_certificate.public_key()
    private_public_key = parsed_private_key.public_key()
    if not hasattr(certificate_public_key, "public_numbers") or not hasattr(private_public_key, "public_numbers"):
        raise GoogleWorkspaceSsoConfigurationError("Workspace SSO signing key type is unsupported")
    if certificate_public_key.public_numbers() != private_public_key.public_numbers():
        raise GoogleWorkspaceSsoConfigurationError("Workspace SSO signing certificate does not match its private key")
    if parsed_certificate.not_valid_after_utc <= timezone.now():
        raise GoogleWorkspaceSsoConfigurationError("Workspace SSO signing certificate has expired")
    return certificate


def validate_authn_request_for_tenant(
    *,
    tenant: GoogleWorkspaceSsoTenant,
    sp_entity_id: str | None,
    acs_url: str | None,
) -> None:
    expected_sp_entity_id = tenant.google_sp_entity_id.strip()
    expected_acs_url = tenant.google_acs_url.strip()
    if not sp_entity_id or not hmac.compare_digest(sp_entity_id, expected_sp_entity_id):
        raise GoogleWorkspaceSsoConfigurationError("SAML AuthnRequest issuer does not match the configured Google SP entity ID")
    if not acs_url or not hmac.compare_digest(acs_url, expected_acs_url):
        raise GoogleWorkspaceSsoConfigurationError("SAML AuthnRequest ACS URL does not match the configured Google ACS URL")


def generate_signing_certificate(*, workspace_domain: str, validity_days: int | None = None) -> dict[str, object]:
    domain = normalize_workspace_domain(workspace_domain)
    days = validity_days if validity_days is not None else int(os.getenv("GOOGLE_WORKSPACE_SSO_CERT_VALIDITY_DAYS", "730"))
    if not 30 <= days <= 3650:
        raise ValidationError("Certificate validity must be between 30 and 3650 days")

    private_key = rsa.generate_private_key(public_exponent=65537, key_size=3072)
    now = timezone.now()
    subject = issuer = x509.Name(
        [
            x509.NameAttribute(NameOID.ORGANIZATION_NAME, "VoxStudio"),
            x509.NameAttribute(NameOID.COMMON_NAME, f"VoxStudio Google Workspace SSO ({domain})"),
        ]
    )
    certificate = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(private_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=5))
        .not_valid_after(now + timedelta(days=days))
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(
            x509.KeyUsage(
                digital_signature=True,
                content_commitment=False,
                key_encipherment=False,
                data_encipherment=False,
                key_agreement=False,
                key_cert_sign=False,
                crl_sign=False,
                encipher_only=False,
                decipher_only=False,
            ),
            critical=True,
        )
        .sign(private_key=private_key, algorithm=hashes.SHA256())
    )
    certificate_pem = certificate.public_bytes(serialization.Encoding.PEM).decode("ascii")
    private_key_pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode("ascii")
    return {
        "certificate_pem": certificate_pem,
        "private_key_pem": private_key_pem,
        "fingerprint_sha256": certificate.fingerprint(hashes.SHA256()).hex(),
        "not_before": certificate.not_valid_before_utc,
        "not_after": certificate.not_valid_after_utc,
    }


def create_signing_certificate(
    *,
    tenant: GoogleWorkspaceSsoTenant,
    state: GoogleWorkspaceSsoSigningCertificate.States,
) -> GoogleWorkspaceSsoSigningCertificate:
    generated = generate_signing_certificate(workspace_domain=tenant.workspace_domain)
    certificate = GoogleWorkspaceSsoSigningCertificate.objects.create(
        tenant=tenant,
        fingerprint_sha256=str(generated["fingerprint_sha256"]),
        state=state,
        not_before=generated["not_before"],
        not_after=generated["not_after"],
        activated_at=timezone.now() if state == GoogleWorkspaceSsoSigningCertificate.States.ACTIVE else None,
    )
    certificate.set_credentials(
        {
            "certificate_pem": generated["certificate_pem"],
            "private_key_pem": generated["private_key_pem"],
        }
    )
    return certificate


def retire_and_promote_next_certificate(*, tenant: GoogleWorkspaceSsoTenant) -> GoogleWorkspaceSsoSigningCertificate:
    next_certificate = tenant.signing_certificates.filter(
        state=GoogleWorkspaceSsoSigningCertificate.States.NEXT,
        retired_at__isnull=True,
    ).order_by("created_at", "id").first()
    if next_certificate is None:
        raise GoogleWorkspaceSsoConfigurationError("Workspace SSO tenant has no next signing certificate")

    now = timezone.now()
    tenant.signing_certificates.filter(
        state=GoogleWorkspaceSsoSigningCertificate.States.ACTIVE,
        retired_at__isnull=True,
    ).update(state=GoogleWorkspaceSsoSigningCertificate.States.RETIRED, retired_at=now)
    next_certificate.state = GoogleWorkspaceSsoSigningCertificate.States.ACTIVE
    next_certificate.activated_at = now
    next_certificate.save(update_fields=["state", "activated_at", "updated_at"])
    return next_certificate


def certificate_fingerprint_from_pem(certificate_pem: str) -> str:
    certificate = x509.load_pem_x509_certificate(certificate_pem.encode("ascii"))
    return hashlib.sha256(certificate.public_bytes(serialization.Encoding.DER)).hexdigest()
