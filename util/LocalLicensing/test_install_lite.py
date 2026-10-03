"""Installer control-flow tests with mocked Docker/Git, real files and real issuer crypto.

These do not substitute for building/running actual Docker images.
"""
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


HERE = Path(__file__).resolve().parent
COMPOSE = """services:
  bitwarden:
    image: ghcr.io/bitwarden/lite:latest
    env_file: [.env]
    volumes: [./bwdata:/etc/bitwarden]
  bitwarden-db:
    image: mariadb:10
    volumes: [./db:/var/lib/mysql]
"""


class LiteInstallerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="lite-installer-tests-")
        self.root = Path(self.temp.name)
        (self.root / "bwdata").mkdir()
        (self.root / "db").mkdir()
        (self.root / "bwdata" / "original-data").write_text("preserve vault data")
        (self.root / "db" / "original-db").write_text("preserve database")
        (self.root / "compose.yaml").write_text(COMPOSE)
        (self.root / ".env").write_text("BW_DB_PASSWORD=fake-test-password\n")
        self.bin = self.root / "mock-bin"
        self.bin.mkdir()
        self.env = dict(os.environ, PATH=str(self.bin) + os.pathsep + os.environ["PATH"],
                        MOCK_ROOT=str(self.root), MOCK_GENERATOR=str(HERE / "license.py"))
        self.command("sudo", "#!/bin/sh\n[ \"$1\" = '-v' ] && exit 0\nexec \"$@\"\n")
        self.command("sleep", "#!/bin/sh\nexit 0\n")
        self.command("df", "#!/bin/sh\nprintf 'Filesystem 1024-blocks Used Available Capacity Mounted\\nx 100000000 0 100000000 0%% /\\n'\n")
        self.command("git", """#!/usr/bin/env python3
import os,pathlib,shutil,sys
args=sys.argv[1:]
if args[:2]==['init','-q']:
    source=pathlib.Path(args[2]); target=source/'util/LocalLicensing'
    target.mkdir(parents=True)
    shutil.copyfile(os.environ['MOCK_GENERATOR'],target/'license.py')
elif 'rev-parse' in args:
    print('7f413a929c719d1f55821f64ce39b906670c2701')
""")
        self.command("docker", """#!/usr/bin/env python3
import json,os,pathlib,sys
args=sys.argv[1:]; root=pathlib.Path(os.environ['MOCK_ROOT'])
with (root/'docker-calls.jsonl').open('a') as stream: stream.write(json.dumps(args)+'\\n')
if args[0]=='info':
    if '--format' in args: print('x86_64')
elif args[0]=='inspect':
    if '-f' in args:
        print('sha256:mock-running-image' if '.Image' in args[2] else 'mock-project')
    else:
        print(json.dumps([{'Mounts':[{'Destination':d,'Source':str(root/f),'Type':'bind'}],
                          'Config':{'Labels':{'com.docker.compose.project':'mock-project'},'Env':[]}}
                         for d,f in [('/etc/bitwarden','bwdata'),('/var/lib/mysql','db')]]))
elif args[0]=='exec':
    container=args[2] if args[1]=='-i' else args[1]
    if container=='bitwarden-db':
        query=sys.stdin.read() if '-i' in args else ''
        if 'JSON_OBJECT' in query:
            print(json.dumps({'id':'0e225759-7388-4f38-bcad-81e5de5139c0','email':'kamil.p011@outlook.com','verified':1}))
        elif 'UPDATE `User`' in query:
            (root/'activation.sql').write_text(query)
            print('1')
        else: print('1')
    elif container=='bitwarden':
        if 'sh' in args:
            if os.environ.get('MOCK_FAIL_READY')=='1': sys.exit(1)
        else: print(json.dumps({'version':os.environ.get('MOCK_VERSION','2026.9.2')}))
""")

    def tearDown(self):
        self.temp.cleanup()

    def command(self, name, content):
        path = self.bin / name
        path.write_text(content)
        path.chmod(0o700)

    def run_installer(self):
        return subprocess.run(["bash", str(HERE / "install-lite.sh")],
                              cwd=self.root, env=self.env, capture_output=True, text=True, timeout=40)

    def calls(self):
        return [json.loads(line) for line in (self.root / "docker-calls.jsonl").read_text().splitlines()]

    def test_version_mismatch_stops_before_build_or_maintenance(self):
        self.env["MOCK_VERSION"] = "2026.8.0"
        result = self.run_installer()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("No changes made", result.stderr)
        self.assertFalse(any(call[0] == "build" or "stop" in call for call in self.calls()))
        self.assertEqual((self.root / "compose.yaml").read_text(), COMPOSE)

    def test_install_snapshots_then_updates_entitlement_and_compose(self):
        result = self.run_installer()
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        self.assertIn("local/bitwarden-lite-premium:", (self.root / "compose.yaml").read_text())
        env_text = (self.root / ".env").read_text()
        self.assertIn("BW_DB_PASSWORD=fake-test-password", env_text)
        self.assertIn("globalSettings__selfHostedLicenseCertificateSha256=", env_text)
        self.assertIn("AccountRevisionDate=UTC_TIMESTAMP(6)", (self.root / "activation.sql").read_text())
        license_file = self.root / "bwdata/licenses/user/0e225759-7388-4f38-bcad-81e5de5139c0.json"
        self.assertTrue(json.loads(license_file.read_text())["Premium"])
        snapshots = list((self.root / ".premium-backups").glob("*/snapshot.tar.gz"))
        self.assertEqual(len(snapshots), 1)
        self.assertTrue(snapshots[0].with_name("rollback.sh").exists())
        self.assertEqual((self.root / "db/original-db").read_text(), "preserve database")
        calls = self.calls()
        self.assertLess(next(i for i,c in enumerate(calls) if c[0] == "build"),
                        next(i for i,c in enumerate(calls) if "stop" in c))

    def test_failed_readiness_restores_snapshot_and_original_image(self):
        self.env["MOCK_FAIL_READY"] = "1"
        result = self.run_installer()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Original database, data/config and image restored", result.stdout)
        self.assertEqual((self.root / "compose.yaml").read_text(), COMPOSE)
        self.assertEqual((self.root / ".env").read_text(), "BW_DB_PASSWORD=fake-test-password\n")
        self.assertEqual((self.root / "db/original-db").read_text(), "preserve database")
        self.assertEqual((self.root / "bwdata/original-data").read_text(), "preserve vault data")
        self.assertFalse((self.root / "bwdata/licenses").exists())
        backup = next((self.root / ".premium-backups").iterdir())
        self.assertTrue((backup / "failed-db").is_dir())
        self.assertTrue((backup / "failed-bwdata").is_dir())
        self.assertIn("local/bitwarden-lite-base:", (backup / "rollback-compose.yml").read_text())


if __name__ == "__main__":
    unittest.main()
