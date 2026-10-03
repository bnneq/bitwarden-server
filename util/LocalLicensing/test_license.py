import base64
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


SCRIPT = Path(__file__).with_name("license.py")


class LocalLicensingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temporary = tempfile.TemporaryDirectory(prefix="issuer-tests-")
        cls.root = Path(cls.temporary.name)
        cls.directory = cls.root / "issuer"
        cls.environment = dict(os.environ, LOCAL_LICENSE_KEY_PASSWORD="local-test-password-123456")
        result = cls.run_cli("init", "--directory", str(cls.directory))
        if result.returncode:
            raise RuntimeError(result.stderr)

    @classmethod
    def tearDownClass(cls):
        cls.temporary.cleanup()

    @classmethod
    def run_cli(cls, *args, environment=None):
        return subprocess.run([sys.executable, str(SCRIPT), *args],
                              capture_output=True, text=True,
                              env=cls.environment if environment is None else environment)

    def issue(self, output, *extra, environment=None):
        return self.run_cli("issue", "--directory", str(self.directory),
                            "--user-id", "0e225759-7388-4f38-bcad-81e5de5139c0",
                            "--email", "local@example.com", "--output", str(output),
                            *extra, environment=environment)

    def test_issue_has_account_bound_claims_and_valid_signature(self):
        output = self.root / "valid.json"
        result = self.issue(output)
        self.assertEqual(result.returncode, 0, result.stderr)
        license_data = json.loads(output.read_text())
        header, payload, signature = license_data["Token"].split(".")
        decode = lambda value: base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
        claims = json.loads(decode(payload))
        self.assertEqual(json.loads(decode(header))["alg"], "RS256")
        self.assertEqual(claims["aud"], "user:" + license_data["Id"])
        self.assertEqual(claims["Premium"], "True")
        self.assertEqual(claims["Email"], license_data["Email"])
        self.assertEqual(claims["LicenseKey"], license_data["LicenseKey"])
        self.assertEqual(claims["exp"] - claims["nbf"], 365 * 86400 + 30)
        self.assertEqual(license_data["LicenseType"], 0)
        pub = self.root / "public.pem"
        pub.write_bytes(subprocess.check_output(["openssl", "x509", "-in",
                         str(self.directory / "issuer.cert.pem"), "-pubkey", "-noout"]))
        sig = self.root / "signature.bin"
        sig.write_bytes(decode(signature))
        verified = subprocess.run(["openssl", "dgst", "-sha256", "-verify", str(pub),
                                  "-signature", str(sig)], input=f"{header}.{payload}".encode(),
                                  capture_output=True)
        self.assertEqual(verified.returncode, 0)
        tampered = subprocess.run(["openssl", "dgst", "-sha256", "-verify", str(pub),
                                  "-signature", str(sig)], input=b"tampered", capture_output=True)
        self.assertNotEqual(tampered.returncode, 0)
        if os.name == "posix":
            self.assertEqual(output.stat().st_mode & 0o777, 0o600)
            self.assertEqual(self.directory.stat().st_mode & 0o777, 0o700)
            self.assertEqual((self.directory / "issuer.key.pem").stat().st_mode & 0o777, 0o600)

    def test_existing_output_is_not_overwritten(self):
        output = self.root / "existing.json"
        output.write_text("keep me")
        self.assertNotEqual(self.issue(output).returncode, 0)
        self.assertEqual(output.read_text(), "keep me")

    def test_wrong_password_creates_no_license(self):
        output = self.root / "wrong-password.json"
        env = dict(self.environment, LOCAL_LICENSE_KEY_PASSWORD="wrong-but-long-password")
        self.assertNotEqual(self.issue(output, environment=env).returncode, 0)
        self.assertFalse(output.exists())

    def test_missing_password_creates_no_issuer(self):
        directory = self.root / "no-password"
        env = dict(self.environment)
        env.pop("LOCAL_LICENSE_KEY_PASSWORD")
        result = self.run_cli("init", "--directory", str(directory), environment=env)
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(directory.exists())

    def test_invalid_uuid_creates_no_license(self):
        output = self.root / "invalid-id.json"
        self.assertNotEqual(self.issue(output, "--user-id", "bad").returncode, 0)
        self.assertFalse(output.exists())

    def test_license_must_not_outlive_certificate(self):
        output = self.root / "too-long.json"
        self.assertNotEqual(self.issue(output, "--days", "3650").returncode, 0)
        self.assertFalse(output.exists())

    def test_init_does_not_replace_existing_keys(self):
        key = self.directory / "issuer.key.pem"
        original = key.read_bytes()
        self.assertNotEqual(self.run_cli("init", "--directory", str(self.directory)).returncode, 0)
        self.assertEqual(key.read_bytes(), original)


if __name__ == "__main__":
    unittest.main()
