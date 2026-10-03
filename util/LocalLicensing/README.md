# Operator-owned Premium licensing for this fork

For the specific MariaDB-backed Lite Compose layout (`bitwarden`,
`bitwarden-db`, `./bwdata`, `./db`, `.env`) see `install-lite.sh`. Run it from
the Compose directory, optionally passing the exact verified account email.
It requires running server version **2026.9.2**, builds against the reviewed
source commit, preserves the running image's Lite packaging/Web Vault, takes
a cold snapshot of both bind mounts and configuration, and automatically
applies the signed license plus only the account's entitlement/revision
fields using an administrative SQL transaction. It does not use the Web Vault
import endpoint or access master passwords/vault encryption keys.

The installer stores the encrypted issuer key and a 0600 password file under
the host's owner-only `.local-licensing` directory. Only the public certificate
and account license enter the application container. It provides automatic
rollback on errors during maintenance plus a one-time `rollback.sh` in the
snapshot directory. **Rollback restores the entire database and bwdata to the
snapshot time**, retaining the failed directories for investigation; do not
use an old rollback snapshot after subsequently saving new vault data without
accounting for those changes. Default license validity is 365 days. Real
Docker image construction and deployment of the installer have not been
tested in this execution environment.

This fork can verify individual Premium licenses signed by an operator-owned RSA
key in **Production** self-hosted deployments. Existing license import, account
updates, JWT signature validation, expiration checks and client synchronization
remain in place. There is no global `return true`, database migration, cloud
request, payment integration, or change to vault cryptography.

This changes technical licensing enforcement, not the software license terms.
The original repository's `LICENSE.txt`, `LICENSE_BITWARDEN.txt` and
`LICENSE_FAQ.md` continue to apply. This is an independent, unsupported fork.

## Scope

The issued license enables **individual Premium** through the existing
`UserService.UpdateLicenseAsync` path. It does not grant organization Enterprise,
SSO, SCIM, Secrets Manager, PAM, or external paid services. Those are separate
products and entitlements. Personal TOTP, attachments, Premium reports and
Emergency Access still use their existing implementations and dependencies.
For example, email delivery and any required external report API credentials
must be configured independently. No client source changes are needed for
features gated on the account's Premium entitlement.

## 1. Generate the issuer offline

Requires Python 3.10+ and OpenSSL 3. Run from the repository root on a trusted
machine. The key is encrypted and never committed or baked into images.

```bash
read -rsp 'Issuer key password (at least 16 characters): ' LOCAL_LICENSE_KEY_PASSWORD
echo
export LOCAL_LICENSE_KEY_PASSWORD
python3 util/LocalLicensing/license.py init --directory .local-licensing
```

Keep the printed SHA-256 pin. Back up the issuer key, encrypted password and
public certificate separately. Generated files are excluded from both Git and
the Docker build context. Only `issuer.cer` goes onto the server; **never mount
`issuer.key.pem` or a PFX with a private key into server containers**.

## 2. Build and configure the fork

Use a source revision compatible with your current deployed database, web vault
and clients. The initial implementation is based on server 2026.9.2, upstream
commit `3ff73a538c91f1440a4392bf50c34f3e30015412`.
Do not replace older servers with this version without the normal upgrade path.

For the standard multi-container distribution, build the services from this
fork, at minimum API and Identity. Rebuild any other service using
`LicensingService` if it runs user/organization validation:

```bash
docker build --platform linux/amd64 -f src/Api/Dockerfile -t local/password-api:2026.9.2 .
docker build --platform linux/amd64 -f src/Identity/Dockerfile -t local/password-identity:2026.9.2 .
```

Keep `ASPNETCORE_ENVIRONMENT=Production` and the existing self-host settings.
Mount the public certificate read-only at, for example,
`/etc/local-licensing/issuer.cer`. Ensure the container's application user can
read the public file (a server copy may have mode 0644). Configure both API and
Identity, and every other process that resolves `LicensingService`, with:

```text
globalSettings__selfHostedLicenseCertificatePath=/etc/local-licensing/issuer.cer
globalSettings__selfHostedLicenseCertificateSha256=<64-hex-character-SHA-256-pin>
```

Example Compose additions for an existing API/Identity deployment:

```yaml
services:
  api:
    image: local/password-api:2026.9.2
    environment:
      globalSettings__selfHostedLicenseCertificatePath: /etc/local-licensing/issuer.cer
      globalSettings__selfHostedLicenseCertificateSha256: ${LOCAL_LICENSE_CERT_SHA256:?Set the public certificate SHA-256 pin}
    volumes:
      - /absolute/server/path/issuer.cer:/etc/local-licensing/issuer.cer:ro
  identity:
    image: local/password-identity:2026.9.2
    environment:
      globalSettings__selfHostedLicenseCertificatePath: /etc/local-licensing/issuer.cer
      globalSettings__selfHostedLicenseCertificateSha256: ${LOCAL_LICENSE_CERT_SHA256:?Set the public certificate SHA-256 pin}
    volumes:
      - /absolute/server/path/issuer.cer:/etc/local-licensing/issuer.cer:ro
```

Merge these additions into the existing Compose configuration; this snippet is
not a complete deployment. Unified/Lite images require rebuilding their own
packaging with this fork's assemblies; switching only an environment variable
on an official image cannot activate the new settings.

Missing/malformed pins, private-key certificates, non-RSA/weak keys, expired
certificates and cloud mode are rejected. With neither new setting present,
the original Bitwarden trust behavior remains unchanged.

## 3. Issue and import a license

Find your exact account UUID with `bw status` after logging in against your
self-hosted server (the `userId` field). Use the exact verified account email.
Issuing a license is offline and does not access or modify vault contents.

```bash
python3 util/LocalLicensing/license.py issue \
  --directory .local-licensing \
  --user-id 'YOUR-ACCOUNT-UUID' \
  --email 'YOUR-VERIFIED-ACCOUNT-EMAIL' \
  --name 'Local account' \
  --days 365 \
  --output .local-licensing/premium.json
unset LOCAL_LICENSE_KEY_PASSWORD
```

Upload `premium.json` using the Web Vault's existing Premium license import.
The official import handler applies Premium and storage limits, saves the
license under the current account ID and updates account revision state.
The existing self-hosted storage maximum remains 10240 GB; this is a configured
quota, not provisioned disk space. Sync clients, or log out and back in if their
cached entitlement does not refresh. Email verification remains required.

Check TOTP with a test item, an attachment upload/download round trip, available
reports, and Emergency Access invitation/recovery with separate test accounts.
Do not treat a Premium badge as verification of every feature. This change does
not include completed end-to-end testing of those feature workflows.

## Renewal, rollback and operations

Tokens expire after `--days` (365 by default), and cannot outlive the issuer
certificate. Generate a fresh output filename and import a replacement before
expiration. A different account UUID or email requires a new license. The
public certificate is loaded at startup; changing it or its pin requires
restarting every affected service.

Before deployment, preserve a recoverable backup of the database, license
directory, attachments, config and previous image tags. Removing the two new
settings or reverting images removes operator-owned trust; locally licensed
Premium accounts will fail validation and may have Premium disabled. Existing
vault entries are not deleted by these patches. Update this fork alongside
upstream security releases; no automatic deployment is supplied.

## Validation

```bash
python3 -m unittest discover -s util/LocalLicensing -p 'test_*.py' -v
dotnet test test/Core.Test/Core.Test.csproj --configuration Release \
  --filter 'FullyQualifiedName~SelfHostedLicenseCertificateTests|FullyQualifiedName~LicensingServiceTests'
```

Python tests cover real signing and verification, account-bound claims,
tampering, encrypted-key password errors, output permissions and overwrite
protection. C# tests cover Production verification, opt-in trust, bad pins,
private/expired certificates, token expiration/audience/signature validation,
and normal user-license validation.

Implementation validation on 2026-10-03: all 18 targeted C# cases and all 7
Python cases passed; API and Identity built successfully in Release. An
additional compatibility smoke test accepted an actual Python-issued license
in the Production `LicensingService`, passed `UserLicense.CanUse`, verified
Premium/storage/expiration claim parsing with en-US, pl-PL and de-DE cultures,
and passed validation of the stored license for the Premium account.
Docker image builds, deployment to a real installation and full feature
workflows have not been tested in this environment.
