#!/usr/bin/env bash
# Run from the existing Compose directory. Optional argument: exact account email.
set -Eeuo pipefail
umask 077

SOURCE_COMMIT=7f413a929c719d1f55821f64ce39b906670c2701
EXPECTED_VERSION=2026.9.2
ACCOUNT_EMAIL=${1:-kamil.p011@outlook.com}
ROOT=$(pwd -P)
STAMP=$(date -u +%Y%m%dT%H%M%SZ)-$$
WORK=$ROOT/.premium-build/$STAMP
BACKUP=$ROOT/.premium-backups/$STAMP
KEYBASE=$ROOT/.local-licensing
KEYDIR=$KEYBASE/issuer
BASE_IMAGE=local/bitwarden-lite-base:$STAMP
NEW_IMAGE=local/bitwarden-lite-premium:$STAMP
MAINTENANCE=0
SNAPSHOT_COMPLETE=0

fail() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }
for command in docker git python3 openssl sudo tar; do
    command -v "$command" >/dev/null || fail "Missing command: $command"
done
docker compose version >/dev/null
docker info >/dev/null
sudo -v
[[ -f .env && -d bwdata && -d db ]] || fail 'Run inside the Compose directory containing .env, bwdata and db.'
COMPOSE=
for candidate in compose.yaml compose.yml docker-compose.yml docker-compose.yaml; do
    if [[ -f $candidate ]]; then
        [[ -z $COMPOSE ]] || fail 'Multiple Compose files found; keep only the active primary file in this directory.'
        COMPOSE=$candidate
    fi
done
[[ -n $COMPOSE ]] || fail 'No Compose file found.'
PROJECT=$(docker inspect -f '{{index .Config.Labels "com.docker.compose.project"}}' bitwarden)
[[ -n $PROJECT && $PROJECT != '<no value>' ]] || fail 'bitwarden is not managed by Compose.'
DC=(docker compose -p "$PROJECT" -f "$ROOT/$COMPOSE")
"${DC[@]}" config --quiet

# Bind mounts must be the exact directories that this script backs up and restores.
docker inspect bitwarden bitwarden-db | python3 -c '
import json,pathlib,sys
root=pathlib.Path(sys.argv[1]).resolve()
containers=json.load(sys.stdin)
for c, destination, folder in zip(containers,["/etc/bitwarden","/var/lib/mysql"],["bwdata","db"]):
    matches=[m for m in c["Mounts"] if m["Destination"]==destination]
    if len(matches)!=1 or matches[0]["Type"]!="bind" or pathlib.Path(matches[0]["Source"]).resolve()!=root/folder:
        raise SystemExit("Container mount does not match this directory: "+folder)
    if c["Config"]["Labels"].get("com.docker.compose.project")!=sys.argv[2]:
        raise SystemExit("Containers belong to different Compose projects")
    for entry in c["Config"].get("Env",[]):
        if entry in ("BW_ENABLE_SSO=true","BW_ENABLE_SCIM=true"):
            raise SystemExit("This installer does not rebuild enabled SSO/SCIM services")
' "$ROOT" "$PROJECT"

VERSION=$(docker exec bitwarden curl -fsS --max-time 20 http://localhost:8080/api/config |
    python3 -c 'import json,sys; print(json.load(sys.stdin)["version"])')
[[ $VERSION == "$EXPECTED_VERSION" ]] || fail "Running server is $VERSION; this tested fork is $EXPECTED_VERSION. No changes made. Use a version-matched backport first."

sql() {
    docker exec -i bitwarden-db sh -c '
        export MYSQL_PWD="$MARIADB_PASSWORD"
        exec mariadb --user="$MARIADB_USER" --database="$MARIADB_DATABASE" --batch --skip-column-names
    '
}
EMAIL_HEX=$(python3 -c 'import sys; print(sys.argv[1].encode().hex())' "$ACCOUNT_EMAIL")
ACCOUNT=$(printf 'SELECT JSON_OBJECT("id", Id, "email", Email, "verified", EmailVerified) FROM `User` WHERE Email=CONVERT(0x%s USING utf8mb4);\n' "$EMAIL_HEX" | sql)
USER_ID=$(printf '%s' "$ACCOUNT" | python3 -c '
import json,sys,uuid
rows=sys.stdin.read().splitlines()
if len(rows)!=1: raise SystemExit("Expected exactly one account with this email. Pass the correct email as the argument.")
row=json.loads(rows[0])
if not row["verified"]: raise SystemExit("Verify your account email in Bitwarden before running this installer.")
print(uuid.UUID(row["id"]))
')

FREE_KB=$(df -Pk "$ROOT" | awk 'END {print $4}')
[[ $FREE_KB =~ ^[0-9]+$ && $FREE_KB -ge 20971520 ]] || fail 'At least 20 GiB free space is required for source, build layers and backup.'
ARCH=$(docker info --format '{{.Architecture}}')
case "$ARCH" in
    x86_64|amd64) PLATFORM=linux/amd64 ;;
    aarch64|arm64) PLATFORM=linux/arm64 ;;
    *) fail "Unsupported Docker architecture: $ARCH" ;;
esac
mkdir -p "$WORK" "$BACKUP" "$KEYBASE"
chmod 700 "$WORK" "$BACKUP" "$KEYBASE"
docker image tag "$(docker inspect -f '{{.Image}}' bitwarden)" "$BASE_IMAGE"

on_failure() {
    local status=$1
    trap - ERR INT TERM
    set +e
    [[ $status -ne 0 ]] || status=1
    if [[ $MAINTENANCE -eq 1 ]]; then
        if [[ $SNAPSHOT_COMPLETE -eq 1 ]]; then
            printf '\nInstallation failed; restoring the stopped snapshot and original image.\n' >&2
            if ! bash "$BACKUP/rollback.sh"; then
                printf 'Automatic restore failed. Keep both services stopped and inspect: %s\n' "$BACKUP" >&2
            fi
        else
            "${DC[@]}" start bitwarden-db
            "${DC[@]}" start bitwarden
        fi
    fi
    printf 'Installer failed. Work directory: %s\n' "$WORK" >&2
    exit "$status"
}
trap 'on_failure $?' ERR
trap 'on_failure 130' INT TERM

printf 'Downloading reviewed fork commit %s...\n' "$SOURCE_COMMIT"
SOURCE=$WORK/source
git init -q "$SOURCE"
git -C "$SOURCE" remote add origin https://github.com/bnneq/bitwarden-server.git
git -C "$SOURCE" fetch --depth 1 origin "$SOURCE_COMMIT"
git -C "$SOURCE" checkout -q --detach FETCH_HEAD
[[ $(git -C "$SOURCE" rev-parse HEAD) == "$SOURCE_COMMIT" ]] || fail 'Unexpected source revision.'

# Keep Lite packaging/Web Vault from the exact running image; rebuild all six standard services.
cat > "$WORK/Dockerfile" <<'DOCKERFILE'
ARG BASE_IMAGE
FROM node:24-alpine3.21 AS admin-assets
WORKDIR /assets
COPY src/Admin/package*.json ./
RUN npm ci
COPY src/Admin/ ./
RUN npm run build

FROM mcr.microsoft.com/dotnet/sdk:10.0-alpine3.23 AS build
ARG TARGETARCH
WORKDIR /source
COPY . ./
RUN set -eu; \
    case "$TARGETARCH" in amd64) RID=linux-musl-x64;; arm64) RID=linux-musl-arm64;; *) exit 1;; esac; \
    for APP in Admin Api Events Icons Identity Notifications; do \
      dotnet publish "src/$APP/$APP.csproj" -c Release -r "$RID" \
        --self-contained true -p:PublishSingleFile=true -p:UseSharedCompilation=false \
        -m:1 -o "/out/$APP"; \
    done

FROM ${BASE_IMAGE}
RUN rm -rf /app/Admin /app/Api /app/Events /app/Icons /app/Identity /app/Notifications
COPY --from=build /out/ /app/
COPY --from=admin-assets /assets/wwwroot/ /app/Admin/wwwroot/
RUN ln -sf /etc/bitwarden/identity.pfx /app/Identity/identity.pfx
ENV ASPNETCORE_ENVIRONMENT=Production
DOCKERFILE

printf 'Building the Lite image. The existing service stays online during the build.\n'
docker build --platform "$PLATFORM" --build-arg "BASE_IMAGE=$BASE_IMAGE" \
    -f "$WORK/Dockerfile" -t "$NEW_IMAGE" "$SOURCE"

if [[ ! -d $KEYDIR ]]; then
    [[ ! -e $KEYBASE/issuer-password ]] || fail 'Issuer password exists but issuer directory is missing.'
    openssl rand -base64 48 > "$KEYBASE/issuer-password"
    chmod 600 "$KEYBASE/issuer-password"
    export LOCAL_LICENSE_KEY_PASSWORD
    LOCAL_LICENSE_KEY_PASSWORD=$(cat "$KEYBASE/issuer-password")
    python3 "$SOURCE/util/LocalLicensing/license.py" init --directory "$KEYDIR"
else
    [[ -f $KEYBASE/issuer-password && -f $KEYDIR/issuer.key.pem && -f $KEYDIR/issuer.cer ]] || fail 'Incomplete existing issuer; recover its files before continuing.'
    export LOCAL_LICENSE_KEY_PASSWORD
    LOCAL_LICENSE_KEY_PASSWORD=$(cat "$KEYBASE/issuer-password")
fi
PIN=$(python3 -c 'import hashlib,sys; print(hashlib.sha256(open(sys.argv[1],"rb").read()).hexdigest().upper())' "$KEYDIR/issuer.cer")
LICENSE=$KEYBASE/premium-$STAMP.json
python3 "$SOURCE/util/LocalLicensing/license.py" issue --directory "$KEYDIR" \
    --user-id "$USER_ID" --email "$ACCOUNT_EMAIL" --days 365 --output "$LICENSE"
unset LOCAL_LICENSE_KEY_PASSWORD

# Stage config changes before stopping anything; never print expanded Compose/.env contents.
python3 - "$ROOT/$COMPOSE" "$ROOT/.env" "$WORK" "$NEW_IMAGE" "$PIN" <<'PY'
import pathlib,re,sys
compose,env,work,image,pin=sys.argv[1:]
text=pathlib.Path(compose).read_text()
pattern=r'''(?m)^(\s*image:\s*)['"]?ghcr\.io/bitwarden/lite:[^\s'"#]+['"]?(\s*(?:#.*)?)$'''
text,count=re.subn(pattern,lambda m:m[1]+image+m[2],text)
if count!=1: raise SystemExit("Expected one ghcr.io/bitwarden/lite image in the original Compose file")
pathlib.Path(work,"compose.patched.yml").write_text(text)
keys={"globalSettings__selfHostedLicenseCertificatePath":"/etc/bitwarden/local-licensing/issuer.cer",
      "globalSettings__selfHostedLicenseCertificateSha256":pin}
lines=[line for line in pathlib.Path(env).read_text().splitlines() if line.split("=",1)[0] not in keys]
pathlib.Path(work,"env.patched").write_text("\n".join(lines)+"\n"+"\n".join(k+"="+v for k,v in keys.items())+"\n")
PY
docker compose -p "$PROJECT" --project-directory "$ROOT" -f "$WORK/compose.patched.yml" config --quiet

# The snapshot is a physical cold backup: both application and database are stopped.
{
    printf '#!/usr/bin/env bash\nset -Eeuo pipefail\numask 077\n'
    printf 'ROOT=%q\nBACKUP=%q\nPROJECT=%q\nCOMPOSE=%q\nBASE_IMAGE=%q\n' "$ROOT" "$BACKUP" "$PROJECT" "$COMPOSE" "$BASE_IMAGE"
    cat <<'ROLLBACK'
cd "$ROOT"
DC=(docker compose -p "$PROJECT" -f "$ROOT/$COMPOSE")
[[ ! -e $BACKUP/failed-db && ! -e $BACKUP/failed-bwdata ]] || { echo 'This snapshot has already been restored.' >&2; exit 1; }
sudo -v
"${DC[@]}" stop -t 120 bitwarden bitwarden-db
sudo mv "$ROOT/db" "$BACKUP/failed-db"
sudo mv "$ROOT/bwdata" "$BACKUP/failed-bwdata"
sudo tar -xzpf "$BACKUP/snapshot.tar.gz" -C "$ROOT"
chmod 600 "$ROOT/.env"
printf 'services:\n  bitwarden:\n    image: %s\n' "$BASE_IMAGE" > "$BACKUP/rollback-compose.yml"
"${DC[@]}" start bitwarden-db
DB_READY=0
for attempt in $(seq 1 90); do
    if docker exec bitwarden-db sh -c 'MYSQL_PWD="$MARIADB_PASSWORD" mariadb --user="$MARIADB_USER" --database="$MARIADB_DATABASE" -e "SELECT 1"' >/dev/null 2>&1; then DB_READY=1; break; fi
    sleep 2
done
[[ $DB_READY -eq 1 ]] || { echo 'Database restore readiness failed; application remains stopped.' >&2; exit 1; }
docker compose -p "$PROJECT" -f "$ROOT/$COMPOSE" -f "$BACKUP/rollback-compose.yml" up -d --no-deps --pull never bitwarden
echo 'Original database, data/config and image restored. The failed directories remain in the backup folder.'
ROLLBACK
} > "$BACKUP/rollback.sh"
chmod 700 "$BACKUP/rollback.sh"

printf 'Stopping Bitwarden and MariaDB for a consistent backup...\n'
MAINTENANCE=1
"${DC[@]}" stop -t 120 bitwarden bitwarden-db
sudo tar --numeric-owner -czf "$BACKUP/snapshot.tar.gz" -C "$ROOT" "$COMPOSE" .env bwdata db
sudo chmod 600 "$BACKUP/snapshot.tar.gz"
sudo tar -tzf "$BACKUP/snapshot.tar.gz" >/dev/null
SNAPSHOT_COMPLETE=1
"${DC[@]}" start bitwarden-db
DB_READY=0
for attempt in $(seq 1 90); do
    if printf 'SELECT 1;\n' | sql >/dev/null 2>&1; then DB_READY=1; break; fi
    sleep 2
done
[[ $DB_READY -eq 1 ]] || { printf 'Database did not start.\n' >&2; on_failure 1; }

sudo install -D -m 644 "$KEYDIR/issuer.cer" "$ROOT/bwdata/local-licensing/issuer.cer"
sudo install -D -m 600 "$LICENSE" "$ROOT/bwdata/licenses/user/$USER_ID.json"
sudo chown --reference="$ROOT/bwdata" "$ROOT/bwdata/local-licensing/issuer.cer" "$ROOT/bwdata/licenses/user/$USER_ID.json"
install -m 600 "$WORK/env.patched" "$ROOT/.env"
cat "$WORK/compose.patched.yml" > "$ROOT/$COMPOSE"
"${DC[@]}" config --quiet

# Administrative entitlement update only. No password, KDF, cipher or vault key fields are modified.
# Equivalent stored fields to the license import handler, plus its account revision bump.
python3 - "$LICENSE" <<'PY' > "$WORK/activate.sql"
import datetime,json,sys,uuid
license=json.load(open(sys.argv[1]))
user=str(uuid.UUID(license["Id"]))
key=str(uuid.UUID(license["LicenseKey"]))
email=license["Email"].encode().hex()
expires=datetime.datetime.fromisoformat(license["Expires"]).astimezone(datetime.timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
print("START TRANSACTION;")
print(f"UPDATE `User` SET Premium=1, LicenseKey='{key}', PremiumExpirationDate='{expires}', MaxStorageGb=10240, RevisionDate=UTC_TIMESTAMP(6), AccountRevisionDate=UTC_TIMESTAMP(6) WHERE Id='{user}' AND Email=CONVERT(0x{email} USING utf8mb4) AND EmailVerified=1;")
print("SELECT ROW_COUNT(); COMMIT;")
PY
UPDATED=$(sql < "$WORK/activate.sql")
[[ $UPDATED == 1 ]] || { printf 'Account update did not affect exactly one verified user.\n' >&2; on_failure 1; }
"${DC[@]}" up -d --no-deps --pull never bitwarden

READY=0
for attempt in $(seq 1 120); do
    if docker exec bitwarden sh -c 'curl -fsS --max-time 5 http://localhost:8080/api/config >/dev/null && curl -fsS --max-time 5 http://localhost:5005/.well-known/openid-configuration >/dev/null' >/dev/null 2>&1; then READY=1; break; fi
    sleep 2
done
[[ $READY -eq 1 ]] || { printf 'API/Identity readiness checks failed.\n' >&2; on_failure 1; }
MAINTENANCE=0
trap - ERR INT TERM
printf '\nPremium entitlement and its signed license are installed for %s.\n' "$ACCOUNT_EMAIL"
printf 'Sync clients, or log out and back in to refresh cached account status.\n'
printf 'License lifetime: 365 days. Issuer keys/password: %s (outside the container).\n' "$KEYBASE"
printf 'Backup and one-time rollback script: %s\n' "$BACKUP"
printf 'Rollback command: bash %q\n' "$BACKUP/rollback.sh"
printf 'API/Identity responded; individual feature workflows still need testing in your clients.\n'
