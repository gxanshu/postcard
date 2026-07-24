from __future__ import annotations

import logging
import os
import re
import subprocess
import tempfile
from datetime import datetime

from .backend import CryptoBackend
from .types import CertificateInfo, SignatureEnvelope, SignatureResult, SignatureStatus

logger = logging.getLogger(__name__)

_X509_FIELD_RE = re.compile(r"^(subject|issuer)\s*=\s*(.*)$", re.IGNORECASE)
_X509_DATE_RE = re.compile(r"^(notBefore|notAfter)\s*=\s*(.*)$", re.IGNORECASE)
_X509_FPR_RE = re.compile(r"^(?:sha256\s+)?fingerprint\s*=\s*(.*)$", re.IGNORECASE)


class SubprocessBackend(CryptoBackend):
    """Verify S/MIME signatures using the OpenSSL command from the runtime.

    OpenSSL is available in the Freedesktop/GNOME runtime, needs no host
    GnuPG infrastructure, and can validate certificate chains against the
    system CA store.
    """

    def __init__(self, gnupg_home: str | None = None) -> None:
        self._gnupg_home = gnupg_home
        self._openssl = self._find_openssl()
        self._cafile = self._find_ca_bundle()

    def _find_openssl(self) -> str:
        for candidate in ("openssl", "/usr/bin/openssl", "/app/bin/openssl"):
            try:
                subprocess.run(
                    [candidate, "version"],
                    capture_output=True,
                    timeout=5,
                    env=self._env(),
                )
                return candidate
            except (FileNotFoundError, subprocess.TimeoutExpired):
                continue
        raise FileNotFoundError("openssl not found on PATH")

    def _find_ca_bundle(self) -> str | None:
        for candidate in (
            "/etc/ssl/certs/ca-certificates.crt",
            "/etc/pki/tls/certs/ca-bundle.crt",
            "/usr/share/ssl/certs/ca-bundle.crt",
        ):
            if os.path.isfile(candidate):
                return candidate
        return None

    def _env(self) -> dict[str, str]:
        env = os.environ.copy()
        env["LC_ALL"] = "C"
        env["LANGUAGE"] = "C"
        return env

    def verify(self, envelope: SignatureEnvelope) -> SignatureResult:
        if envelope.kind == "detached":
            if envelope.signature is None:
                return SignatureResult(
                    status=SignatureStatus.ERROR,
                    message="Detached signature envelope has no signature data",
                )
            return self._verify_detached(envelope)
        elif envelope.kind == "opaque":
            return self._verify_opaque(envelope)
        else:
            return SignatureResult(
                status=SignatureStatus.ERROR,
                message=f"Unknown signature envelope kind: {envelope.kind}",
            )

    def _verify_detached(self, envelope: SignatureEnvelope) -> SignatureResult:
        with (
            tempfile.NamedTemporaryFile(delete=False, suffix=".p7s") as sig_f,
            tempfile.NamedTemporaryFile(delete=False) as data_f,
        ):
            sig_f.write(envelope.signature)
            data_f.write(envelope.payload)
            sig_path = sig_f.name
            data_path = data_f.name

        try:
            return self._run_verify(sig_path, data_path)
        finally:
            os.unlink(sig_path)
            os.unlink(data_path)

    def _verify_opaque(self, envelope: SignatureEnvelope) -> SignatureResult:
        with tempfile.NamedTemporaryFile(delete=False, suffix=".p7m") as f:
            f.write(envelope.payload)
            payload_path = f.name

        try:
            return self._run_verify(payload_path, None)
        finally:
            os.unlink(payload_path)

    def _run_verify(self, pkcs7_path: str, content_path: str | None) -> SignatureResult:
        signer = self._extract_signer_info(pkcs7_path, content_path)

        # First try to verify the full chain against system CAs.
        rc, output = self._openssl_verify(pkcs7_path, content_path, check_ca=True)
        if rc == 0:
            return SignatureResult(
                status=SignatureStatus.VALID,
                message=_clean_message(output),
                signer=signer,
            )

        # If the chain failed, check whether the signature itself is good.
        rc_noverify, output_noverify = self._openssl_verify(
            pkcs7_path, content_path, check_ca=False
        )
        if rc_noverify == 0:
            status = _map_verify_error(output)
            return SignatureResult(
                status=status,
                message=_clean_message(output),
                signer=signer,
            )

        # Signature is cryptographically invalid.
        return SignatureResult(
            status=SignatureStatus.INVALID,
            message=_clean_message(output_noverify or output),
            signer=signer,
        )

    def _openssl_verify(
        self, pkcs7_path: str, content_path: str | None, check_ca: bool
    ) -> tuple[int, str]:
        cmd = [
            self._openssl,
            "smime",
            "-verify",
            "-in",
            pkcs7_path,
            "-inform",
            "DER",
            "-out",
            "/dev/null",
        ]
        if content_path is not None:
            cmd.extend(["-content", content_path])
        if check_ca:
            if self._cafile:
                cmd.extend(["-CAfile", self._cafile])
        else:
            cmd.append("-noverify")

        try:
            proc = subprocess.run(cmd, capture_output=True, timeout=30, env=self._env())
        except (subprocess.TimeoutExpired, OSError) as exc:
            return 1, str(exc)

        stderr = proc.stderr.decode("utf-8", errors="replace")
        stdout = proc.stdout.decode("utf-8", errors="replace")
        logger.debug("openssl verify rc=%s cmd=%s", proc.returncode, " ".join(cmd))
        logger.debug("openssl verify stdout: %s", stdout)
        logger.debug("openssl verify stderr: %s", stderr)
        return proc.returncode, stdout + "\n" + stderr

    def _extract_signer_info(
        self, pkcs7_path: str, content_path: str | None = None
    ) -> CertificateInfo | None:
        # Try openssl smime -signer first — it reliably extracts the signer cert.
        info = self._extract_signer_via_openssl(pkcs7_path, content_path)
        if info is not None:
            return info
        # Fallback: parse all certs from pkcs7, prefer one with an email address.
        pem_certs = self._extract_certificates(pkcs7_path)
        if not pem_certs:
            return None
        for pem in pem_certs:
            info = self._parse_certificate(pem)
            if info and info.email:
                return info
        return self._parse_certificate(pem_certs[-1])

    def _extract_signer_via_openssl(
        self, pkcs7_path: str, content_path: str | None
    ) -> CertificateInfo | None:
        with tempfile.NamedTemporaryFile(
            delete=False, suffix=".pem"
        ) as tmp:
            signer_path = tmp.name
        try:
            cmd = [
                self._openssl,
                "smime",
                "-verify",
                "-in",
                pkcs7_path,
                "-inform",
                "DER",
                "-noverify",
                "-out",
                "/dev/null",
                "-signer",
                signer_path,
            ]
            if content_path is not None:
                cmd.extend(["-content", content_path])
            proc = subprocess.run(
                cmd, capture_output=True, timeout=15, env=self._env()
            )
            if proc.returncode != 0:
                return None
            with open(signer_path) as f:
                pem = f.read()
            if not pem.startswith("-----BEGIN CERTIFICATE-----"):
                return None
            return self._parse_certificate(pem)
        except (subprocess.TimeoutExpired, OSError):
            return None
        finally:
            if os.path.exists(signer_path):
                os.unlink(signer_path)

    def _extract_certificates(self, pkcs7_path: str) -> list[str]:
        cmd = [
            self._openssl,
            "pkcs7",
            "-in",
            pkcs7_path,
            "-inform",
            "DER",
            "-print_certs",
        ]
        try:
            proc = subprocess.run(cmd, capture_output=True, timeout=15, env=self._env())
        except (subprocess.TimeoutExpired, OSError) as exc:
            logger.debug("certificate extraction failed: %s", exc)
            return []

        if proc.returncode != 0:
            return []

        out = proc.stdout.decode("utf-8", errors="replace")
        certs: list[str] = []
        current: list[str] = []
        for line in out.splitlines():
            if line.startswith("-----BEGIN CERTIFICATE-----"):
                current = [line]
            elif line.startswith("-----END CERTIFICATE-----"):
                current.append(line)
                certs.append("\n".join(current))
                current = []
            elif current:
                current.append(line)
        return certs

    def _parse_certificate(self, pem: str) -> CertificateInfo | None:
        cmd = [
            self._openssl,
            "x509",
            "-in",
            "/dev/stdin",
            "-noout",
            "-subject",
            "-issuer",
            "-startdate",
            "-enddate",
            "-fingerprint",
            "-sha256",
        ]
        try:
            proc = subprocess.run(
                cmd,
                input=pem.encode("utf-8"),
                capture_output=True,
                timeout=15,
                env=self._env(),
            )
        except (subprocess.TimeoutExpired, OSError) as exc:
            logger.debug("certificate parsing failed: %s", exc)
            return None

        if proc.returncode != 0:
            return None

        out = proc.stdout.decode("utf-8", errors="replace")
        info = CertificateInfo()
        for line in out.splitlines():
            m = _X509_FIELD_RE.match(line)
            if m:
                key, value = m.group(1).lower(), m.group(2).strip()
                if key == "subject":
                    info.subject = _format_dn(value)
                    info.email = _extract_email(value)
                elif key == "issuer":
                    info.issuer = _format_dn(value)
                continue

            m = _X509_DATE_RE.match(line)
            if m:
                key, value = m.group(1).lower(), m.group(2).strip()
                parsed = _parse_x509_date(value)
                if key == "notbefore":
                    info.not_before = parsed
                elif key == "notafter":
                    info.not_after = parsed
                continue

            m = _X509_FPR_RE.match(line)
            if m:
                info.fingerprint = m.group(1).replace(":", "")

        info.certificate_pem = pem
        return info


def _format_dn(dn: str) -> str:
    parts = [part.strip() for part in dn.split(",") if part.strip()]
    return ", ".join(parts)


def _extract_email(dn: str) -> str:
    for part in dn.split(","):
        part = part.strip()
        if part.lower().startswith("emailaddress="):
            return part.split("=", 1)[1].strip()
        if part.lower().startswith("1.2.840.113549.1.9.1="):
            return part.split("=", 1)[1].strip()
    return ""


def _parse_x509_date(value: str) -> str:
    # OpenSSL default format: "Jul  6 04:53:37 2026 GMT"
    try:
        dt = datetime.strptime(value.strip(), "%b %d %H:%M:%S %Y %Z")
        return dt.strftime("%Y-%m-%d")
    except ValueError:
        pass
    # Some locales/lines may have a single-digit day without extra space.
    try:
        dt = datetime.strptime(value.strip(), "%b %d %H:%M:%S %Y %Z")
        return dt.strftime("%Y-%m-%d")
    except ValueError:
        return value


def _map_verify_error(output: str) -> SignatureStatus:
    lower = output.lower()
    if "certificate has expired" in lower:
        return SignatureStatus.EXPIRED
    if "certificate is not yet valid" in lower:
        return SignatureStatus.EXPIRED
    return SignatureStatus.UNTRUSTED


def _clean_message(output: str) -> str:
    lines = []
    for line in output.splitlines():
        stripped = line.strip()
        if stripped and not stripped.startswith("Verification"):
            lines.append(stripped)
    return "\n".join(lines)
