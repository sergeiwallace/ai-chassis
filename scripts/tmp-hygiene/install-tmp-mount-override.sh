#!/usr/bin/env bash
# Guarded, root-owned host mitigation for tmpfs inode exhaustion.

set -euo pipefail

CEILING="${AIH_TMP_NR_INODES:-4194304}"
DROPIN_DIR="${AIH_TMP_MOUNT_DROPIN_DIR:-/etc/systemd/system/tmp.mount.d}"
DROPIN="$DROPIN_DIR/override.conf"
STATE_DIR="${AIH_TMP_MOUNT_STATE_DIR:-/var/lib/ai-harness/tmp-mount-override}"
BACKUP="$STATE_DIR/prior-override.conf"
PRIOR_EXISTS="$STATE_DIR/prior-override-existed"
MANAGED_HASH="$STATE_DIR/managed-content.cksum"
PENDING="$STATE_DIR/pending-verification"

usage() {
  echo "usage: $0 apply [--apply reboot|remount] [--force] | rollback [--apply reboot|remount] | verify" >&2
}

need_root_linux() {
  if [[ "$(uname -s)" != "Linux" ]]; then
    echo "tmp.mount override is supported only on Linux; no mount change made" >&2
    exit 0
  fi
  if [[ "$EUID" -ne 0 && "${AIH_TMP_MOUNT_TEST_ALLOW_UNPRIVILEGED:-}" != 1 ]]; then
    echo "root is required for tmp.mount override; no mount change made" >&2
    exit 0
  fi
}

vendor_options() {
  local fragment options count
  fragment="$(systemctl show --value --property=FragmentPath tmp.mount 2>/dev/null || true)"
  [[ -n "$fragment" && -r "$fragment" ]] || return 1
  count="$(grep -c '^Options=' "$fragment" || true)"
  [[ "$count" == 1 ]] || return 1
  options="$(sed -n 's/^Options=//p' "$fragment")"
  [[ ",$options," == *,nr_inodes=*,* ]] || return 1
  printf '%s\n' "$options"
}

replace_nr_inodes() {
  sed -E "s/(^|,)nr_inodes=[^,]*/\\1nr_inodes=${CEILING}/" <<<"$1"
}

content_hash() { cksum < "$1"; }

save_prior() {
  install -d -m 0700 "$STATE_DIR"
  if [[ -e "$DROPIN" ]]; then
    install -m 0600 "$DROPIN" "$BACKUP"
    printf 'yes\n' > "$PRIOR_EXISTS"
  else
    rm -f "$BACKUP"
    printf 'no\n' > "$PRIOR_EXISTS"
  fi
}

managed_content_is_current() {
  [[ -f "$MANAGED_HASH" && -f "$DROPIN" ]] || return 1
  [[ "$(content_hash "$DROPIN")" == "$(<"$MANAGED_HASH")" ]]
}

restore_prior() {
  [[ -f "$PRIOR_EXISTS" ]] || return 0
  if [[ "$(<"$PRIOR_EXISTS")" == yes ]]; then
    install -d -m 0755 "$DROPIN_DIR"
    install -m 0644 "$BACKUP" "$DROPIN"
  else
    rm -f "$DROPIN"
  fi
  rm -f "$MANAGED_HASH" "$PENDING"
}

write_dropin() {
  local options temp force="$1"
  options="$(vendor_options)" || {
    echo "cannot safely read exactly one vendor tmp.mount Options= with nr_inodes; no mount change made" >&2
    return 1
  }
  if [[ -f "$MANAGED_HASH" ]] && ! managed_content_is_current; then
    if [[ "$force" != 1 ]]; then
      echo "tmp.mount drop-in was modified outside this tool; refusing to overwrite without --force" >&2
      return 1
    fi
    save_prior
  elif [[ ! -f "$PRIOR_EXISTS" ]]; then
    save_prior
  fi
  install -d -m 0755 "$DROPIN_DIR"
  temp="$(mktemp "$DROPIN_DIR/.override.conf.XXXXXX")"
  printf '[Mount]\nOptions=%s\n' "$(replace_nr_inodes "$options")" > "$temp"
  chmod 0644 "$temp"
  mv -f "$temp" "$DROPIN"
  content_hash "$DROPIN" > "$MANAGED_HASH"
}

verify_effective() {
  local expected="$1" options
  options="$(findmnt -no OPTIONS /tmp 2>/dev/null || true)"
  if [[ ! ",$options," =~ ,nr_inodes=${expected}, ]]; then
    echo "effective /tmp mount does not show exact nr_inodes=${expected}: ${options:-unreadable}" >&2
    return 1
  fi
  echo "verified effective /tmp mount: $options"
}

apply_path() {
  local path="$1"
  systemctl daemon-reload || return
  if [[ "$path" == reboot ]]; then
    printf 'apply %s\n' "$CEILING" > "$PENDING"
    echo "PENDING: drop-in staged; reboot, then run '$0 verify' to verify the effective /tmp mount" >&2
    return 3
  fi
  mount -o "remount,nr_inodes=${CEILING}" /tmp || return
  verify_effective "$CEILING" || return
}

apply() {
  local path="$1" force="$2" status
  write_dropin "$force" || return
  if apply_path "$path"; then
    return
  else
    status=$?
  fi
  if [[ "$path" == reboot && "$status" == 3 ]]; then
    return 3
  fi
  if [[ "$status" != 0 ]]; then
    restore_prior
    return "$status"
  fi
}

restored_ceiling() {
  local restored_options
  if [[ "$(<"$PRIOR_EXISTS")" == yes ]]; then
    restored_options="$(sed -n 's/^Options=//p' "$BACKUP")"
  else
    restored_options="$(vendor_options)"
  fi
  # `-E` deliberately, matching `replace_nr_inodes` above: `\|` alternation inside a BRE
  # group is a GNU extension, and BSD sed matches it as the literal three bytes `^|,`.
  # The group then never matches, this returns empty, and `rollback` refuses with
  # "cannot safely determine restored nr_inodes" -- a fail-closed refusal, but one that
  # makes rollback unavailable rather than wrong.
  sed -nE 's/.*(^|,)nr_inodes=([^,]*).*/\2/p' <<<"$restored_options"
}

rollback() {
  local path="$1" ceiling
  [[ -f "$PRIOR_EXISTS" ]] || { echo "no saved tmp.mount override state; no mount change made" >&2; return 1; }
  ceiling="$(restored_ceiling)"
  [[ -n "$ceiling" ]] || { echo "cannot safely determine restored nr_inodes; no mount change made" >&2; return 1; }
  restore_prior
  systemctl daemon-reload
  if [[ "$path" == reboot ]]; then
    printf 'rollback %s\n' "$ceiling" > "$PENDING"
    echo "PENDING: prior drop-in restored; reboot, then run '$0 verify' to verify the effective /tmp mount" >&2
    return 3
  fi
  mount -o "remount,nr_inodes=${ceiling}" /tmp
  verify_effective "$ceiling"
}

verify_pending() {
  local action expected
  [[ -f "$PENDING" ]] || { echo "no pending reboot verification" >&2; return 1; }
  read -r action expected < "$PENDING"
  verify_effective "$expected"
  rm -f "$PENDING"
  echo "verified pending $action operation"
}

action="${1:-}"; apply_path_name=reboot; force=0
[[ $# -ge 1 ]] && shift
while [[ $# -gt 0 ]]; do
  case "$1" in
    --apply) apply_path_name="${2:-}"; shift 2 ;;
    --force) force=1; shift ;;
    *) usage; exit 2 ;;
  esac
done
[[ "$action" == verify || ( ( "$action" == apply || "$action" == rollback ) && ( "$apply_path_name" == reboot || "$apply_path_name" == remount ) ) ]] || { usage; exit 2; }

need_root_linux
case "$action" in
  apply) apply "$apply_path_name" "$force" ;;
  rollback) rollback "$apply_path_name" ;;
  verify) verify_pending ;;
esac
