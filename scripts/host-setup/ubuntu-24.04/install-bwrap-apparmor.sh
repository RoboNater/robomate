#!/usr/bin/env bash
set -euo pipefail
here=$(cd -- "$(dirname -- "$0")" && pwd)
if [[ ${1:-} == --dry-run && $# == 1 ]]; then
  echo 'Would install bubblewrap if missing; inspect profiles attached to /usr/bin/bwrap; back up and unload Ubuntu bwrap-userns-restrict; install and load apparmor/bwrap; verify as invoking user.'
  echo 'Undo: remove /etc/apparmor.d/bwrap and reload the backed-up Ubuntu profile with apparmor_parser.'
  exit 0
fi
[[ $# == 0 ]] || { echo "Usage: $0 [--dry-run]" >&2; exit 2; }
[[ $EUID == 0 && -n ${SUDO_USER:-} && $SUDO_USER != root ]] || { echo 'Run with sudo from the invoking user.' >&2; exit 1; }
if [[ ! -x /usr/bin/bwrap ]]; then apt-get update; apt-get install -y bubblewrap; fi
# Fail closed on attachments that could match /usr/bin/bwrap.
python3 "$here/check-bwrap-profiles.py" /etc/apparmor.d "$here/apparmor/bwrap"
old=/etc/apparmor.d/bwrap-userns-restrict
backup=/var/backups/bwrap-userns-restrict.apparmor
if [[ -f $old ]]; then
  if [[ -e $backup ]]; then
    cmp -s "$old" "$backup" || { echo "Different backup already exists: $backup; resolve manually." >&2; exit 1; }
  else
    cp -p -- "$old" "$backup"
  fi
  profiles=/sys/kernel/security/apparmor/profiles
  [[ -r $profiles ]] || { echo "Cannot inspect loaded AppArmor profiles: $profiles" >&2; exit 1; }
  if grep -Eq '^bwrap \(' "$profiles"; then
    apparmor_parser -R "$old"
  fi
  rm -- "$old"
fi
install -m 0644 "$here/apparmor/bwrap" /etc/apparmor.d/bwrap
apparmor_parser -r /etc/apparmor.d/bwrap
echo "Undo: sudo apparmor_parser -R /etc/apparmor.d/bwrap; sudo rm /etc/apparmor.d/bwrap; restore $backup to $old and load it with sudo apparmor_parser -r $old (if a backup exists)."
sudo -u "$SUDO_USER" -- "$here/verify-sandboxes.sh"
