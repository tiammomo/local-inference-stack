#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PROFILE_FILE="${MODELPORT_BACKUP_PROFILE_FILE:-}"
if [[ -z "$PROFILE_FILE" && -n "${CREDENTIALS_DIRECTORY:-}" \
  && -f "$CREDENTIALS_DIRECTORY/backup.env" ]]; then
  PROFILE_FILE="$CREDENTIALS_DIRECTORY/backup.env"
fi
PROFILE_FILE="${PROFILE_FILE:-$ROOT_DIR/profiles/backup.local.env}"
# shellcheck source=scripts/lib/deployment.sh
source "$ROOT_DIR/scripts/lib/deployment.sh"

if [[ -f "$PROFILE_FILE" ]]; then
  validate_private_env_file "$PROFILE_FILE"
  set -a
  # shellcheck disable=SC1090
  source "$PROFILE_FILE"
  set +a
fi

MODELPORT_DIR="${MODELPORT_PROJECT_DIR:-}"
BACKUP_DIR="${MODELPORT_BACKUP_DIR:-$ROOT_DIR/backups/modelport}"
RETENTION_DAYS="${MODELPORT_BACKUP_RETENTION_DAYS:-14}"

die() {
  printf '[local-inference-backup] ERROR: %s\n' "$*" >&2
  exit 1
}

latest_archive() {
  find "$BACKUP_DIR" -maxdepth 1 -type f -name 'modelport-*.tar.gz' \
    -printf '%T@\t%p\n' 2>/dev/null | sort -nr | head -n 1 | cut -f2-
}

[[ -n "$MODELPORT_DIR" ]] || die "MODELPORT_PROJECT_DIR is required in $PROFILE_FILE"
[[ -x "$MODELPORT_DIR/scripts/backup-compose.sh" ]] \
  || die "ModelPort backup helper is missing or not executable: $MODELPORT_DIR/scripts/backup-compose.sh"

export MODELPORT_BACKUP_DIR="$BACKUP_DIR"
export MODELPORT_BACKUP_RETENTION_DAYS="$RETENTION_DAYS"

case "${1:-}" in
  create)
    mkdir -p "$BACKUP_DIR"
    chmod 700 "$BACKUP_DIR"
    exec "$MODELPORT_DIR/scripts/backup-compose.sh" create
    ;;
  verify|drill)
    archive="${2:-$(latest_archive)}"
    [[ -n "$archive" ]] || die "no completed ModelPort backup was found in $BACKUP_DIR"
    exec "$MODELPORT_DIR/scripts/backup-compose.sh" "$1" "$archive"
    ;;
  latest)
    archive="$(latest_archive)"
    [[ -n "$archive" ]] || die "no completed ModelPort backup was found in $BACKUP_DIR"
    printf '%s\n' "$archive"
    ;;
  -h|--help|help)
    printf 'Usage: %s {create|verify [ARCHIVE]|drill [ARCHIVE]|latest}\n' "$0"
    ;;
  *)
    printf 'Usage: %s {create|verify [ARCHIVE]|drill [ARCHIVE]|latest}\n' "$0" >&2
    exit 2
    ;;
esac
