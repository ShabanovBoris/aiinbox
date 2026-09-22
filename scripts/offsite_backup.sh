#!/usr/bin/env bash
set -euo pipefail

# Off-host replication stays outside the application runtime: the deploy host
# owns SSH credentials, while app.ops owns SQLite consistency and verification.
target="\${OFFSITE_BACKUP_TARGET:-}"
if [[ -z "$target" || "$target" != *:* ]]; then
  echo "OFFSITE_BACKUP_TARGET must be an rsync-over-SSH target, e.g. backup@example:/srv/aiinbox" >&2
  exit 2
fi

for command in docker rsync; do
  if ! command -v "$command" >/dev/null 2>&1; then
    echo "required command not found: $command" >&2
    exit 2
  fi
done

root="$(cd "$(dirname "\${BASH_SOURCE[0]}")/.." && pwd)"
cd "$root"

stage="$(mktemp -d)"
trap 'rm -rf "$stage"' EXIT
mkdir -p "$stage/recovered"

backup_output="$(docker compose exec -T app python -m app.ops backup)"
printf '%s\n' "$backup_output"
backup_path="$(printf '%s\n' "$backup_output" | sed -n 's/^backup=\([^ ]*\) .*/\1/p')"
checksum_path="$(printf '%s\n' "$backup_output" | sed -n 's/.* checksum=\([^ ]*\) .*/\1/p')"
if [[ -z "$backup_path" || -z "$checksum_path" ]]; then
  echo "could not parse backup/checksum paths from app.ops output" >&2
  exit 1
fi

backup_name="$(basename "$backup_path")"
checksum_name="$(basename "$checksum_path")"
docker compose exec -T app cat "$backup_path" > "$stage/$backup_name"
docker compose exec -T app cat "$checksum_path" > "$stage/$checksum_name"

# rsync uses temporary files and rename by default, so interrupted transfers do
# not expose a partially uploaded generation under the final filename.
rsync -a --chmod=F600 "$stage/$backup_name" "$target/"
rsync -a --chmod=F600 "$stage/$checksum_name" "$target/"

# Pull the exact uploaded generation back from the remote failure domain before
# declaring replication successful. This catches transfer/storage corruption.
rsync -a "$target/$backup_name" "$stage/recovered/$backup_name"
rsync -a "$target/$checksum_name" "$stage/recovered/$checksum_name"

host_user="$(id -u):$(id -g)"
docker compose run --rm --no-deps \
  --user "$host_user" \
  -v "$stage/recovered:/recovery:ro" \
  app python -m app.ops verify-copy \
  --database "/recovery/$backup_name" \
  --checksum-file "/recovery/$checksum_name"

# Restore into container tmpfs. restore_database verifies the restored SQLite
# file again, while leaving canonical /data untouched.
docker compose run --rm --no-deps \
  --user "$host_user" \
  -v "$stage/recovered:/recovery:ro" \
  app python -m app.ops restore \
  --backup "/recovery/$backup_name" \
  --target /tmp/aiinbox/offsite-recovery.db

echo "offsite_backup=ok generation=$backup_name recovery_drill=ok"
