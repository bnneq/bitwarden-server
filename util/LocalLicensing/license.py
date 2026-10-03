#!/usr/bin/env python3
"""Offline license issuer for the operator-owned trust option in this fork."""

import argparse
import base64
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import uuid


PASSWORD_ENV = "LOCAL_LICENSE_KEY_PASSWORD"


def openssl(*args, data=None):
    result = subprocess.run(
        ["openssl", *map(str, args)], input=data, capture_output=True, check=False
    )
    if result.returncode:
        raise ValueError("OpenSSL operation failed; check the key, certificate and password.")
    return result.stdout


def require_password():
    if len(os.environ.get(PASSWORD_ENV, "")) < 16:
        raise ValueError(f"Set {PASSWORD_ENV} to a password of at least 16 characters.")


def write_new(path, data):
    # Exclusive creation prevents silently replacing an existing license or key.
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as stream:
        stream.write(data)


def init(args):
    require_password()
    directory = Path(args.directory).resolve()
    directory.mkdir(mode=0o700, parents=True, exist_ok=False)
    # OpenSSL output remains inside an owner-only directory from the moment of creation.
    try:
        key = directory / "issuer.key.pem"
        pem = directory / "issuer.cert.pem"
        openssl("genpkey", "-algorithm", "RSA", "-pkeyopt", "rsa_keygen_bits:3072",
                "-aes-256-cbc", "-pass", f"env:{PASSWORD_ENV}", "-out", key)
        key.chmod(0o600)
        openssl("req", "-new", "-x509", "-sha256", "-days", "3650", "-key", key,
                "-passin", f"env:{PASSWORD_ENV}", "-subj", "/CN=Local Self-Hosted License Issuer",
                "-out", pem)
        pem.chmod(0o600)
        der = openssl("x509", "-in", pem, "-outform", "DER")
        write_new(directory / "issuer.cer", der)
        print("Public certificate: issuer.cer")
        print("SHA-256 pin:", hashlib.sha256(der).hexdigest().upper())
    except Exception:
        # Do not delete partially generated keys automatically; the caller can inspect them.
        raise


def b64url(value):
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def invariant_date(value):
    # ISO 8601 also parses correctly when the server's current culture is not en-US.
    return value.isoformat()


def issue(args):
    require_password()
    user_id = str(uuid.UUID(args.user_id))
    email = args.email.strip()
    if "@" not in email or any(char.isspace() for char in email):
        raise ValueError("Provide the exact email address of the verified account.")
    if not 1 <= args.days <= 3650:
        raise ValueError("License lifetime must be between 1 and 3650 days.")

    directory = Path(args.directory).resolve()
    key = directory / "issuer.key.pem"
    pem = directory / "issuer.cert.pem"
    # The issuer certificate must remain valid for the entire license lifetime.
    openssl("x509", "-in", pem, "-checkend", str(args.days * 86400 + 60), "-noout")
    now = dt.datetime.now(dt.timezone.utc).replace(microsecond=0)
    expires = now + dt.timedelta(days=args.days)
    license_key = str(uuid.uuid4())
    claims = {
        "iss": "bitwarden", "aud": f"user:{user_id}",
        "nbf": int(now.timestamp()) - 30, "exp": int(expires.timestamp()),
        "jti": str(uuid.uuid4()), "LicenseType": "User", "Id": user_id,
        "Email": email, "Name": args.name, "LicenseKey": license_key,
        "Premium": "True", "MaxStorageGb": "10240", "Trial": "False",
        "Issued": invariant_date(now), "Expires": invariant_date(expires),
        "Refresh": invariant_date(expires),
    }
    header = {"alg": "RS256", "typ": "JWT"}
    encode = lambda obj: b64url(json.dumps(obj, separators=(",", ":")).encode("utf-8"))
    message = f"{encode(header)}.{encode(claims)}".encode("ascii")
    signature = openssl("dgst", "-sha256", "-sign", key,
                        "-passin", f"env:{PASSWORD_ENV}", data=message)
    # Verify the generated signature before exporting anything. A mismatched key/cert fails here.
    with tempfile.TemporaryDirectory(prefix="local-license-") as temporary:
        pub = Path(temporary) / "public.pem"
        sig = Path(temporary) / "signature.bin"
        write_new(pub, openssl("x509", "-in", pem, "-pubkey", "-noout"))
        write_new(sig, signature)
        openssl("dgst", "-sha256", "-verify", pub, "-signature", sig, data=message)
    license_data = {
        "LicenseType": 0, "Version": 1, "Id": user_id, "Email": email,
        "Name": args.name, "LicenseKey": license_key, "Premium": True,
        "MaxStorageGb": 10240, "Trial": False,
        "Issued": now.isoformat(), "Expires": expires.isoformat(),
        "Refresh": expires.isoformat(),
        "Token": message.decode("ascii") + "." + b64url(signature),
    }
    output = Path(args.output).resolve()
    write_new(output, (json.dumps(license_data, indent=2) + "\n").encode("utf-8"))
    print(f"License written to {output}; expires {expires.isoformat()}.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    initialize = commands.add_parser("init", help="Create an encrypted RSA issuer key and public certificate")
    initialize.add_argument("--directory", required=True)
    initialize.set_defaults(run=init)
    issuer = commands.add_parser("issue", help="Issue a signed Premium license for an existing account")
    issuer.add_argument("--directory", required=True)
    issuer.add_argument("--user-id", required=True)
    issuer.add_argument("--email", required=True)
    issuer.add_argument("--name", default="Local user")
    issuer.add_argument("--days", type=int, default=365)
    issuer.add_argument("--output", required=True)
    issuer.set_defaults(run=issue)
    args = parser.parse_args()
    try:
        args.run(args)
    except (ValueError, OSError) as error:
        print(f"Error: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
