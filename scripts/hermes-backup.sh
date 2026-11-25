#!/usr/bin/env bash
# Fork: encrypted backup of Open WebUI's database (and optionally its uploads), run on the Docker host.
#
# A consistent snapshot is taken inside the container, streamed out as a tar and encrypted with age
# for a PUBLIC key. The private key must not be on this machine. See docs/ENCRYPTION.fork.md.
#
#   AGE_RECIPIENT=age1...  ./hermes-backup.sh
#
# Settings (environment):
#   AGE_RECIPIENT            public key(s), space separated        (this or AGE_RECIPIENTS_FILE is required)
#   AGE_RECIPIENTS_FILE      file with one public key per line
#   BACKUP_DIR               default /root/backups/encrypted
#   INCLUDE_UPLOADS          1 to add the uploads directory        (default 0)
#   BACKUP_KEEP              number of backups kept                (default 7)
#   BACKUP_MAX_AGE_DAYS      older backups are removed, 0 for none (default 30); the newest is always kept
#   OWUI_CONTAINER           default open-webui
#   OWUI_DATA_DIR            data directory inside the container   (default /app/backend/data)
#   OWUI_TOOL                default /app/backend/open_webui/utils/sqlcipher_tool.py
#   OWUI_EXEC                command that runs a program next to the data (default "docker exec -i $OWUI_CONTAINER")
set -euo pipefail
umask 077

BACKUP_DIR=${BACKUP_DIR:-/root/backups/encrypted}
INCLUDE_UPLOADS=${INCLUDE_UPLOADS:-0}
BACKUP_KEEP=${BACKUP_KEEP:-7}
BACKUP_MAX_AGE_DAYS=${BACKUP_MAX_AGE_DAYS:-30}
OWUI_CONTAINER=${OWUI_CONTAINER:-open-webui}
OWUI_DATA_DIR=${OWUI_DATA_DIR:-/app/backend/data}
OWUI_TOOL=${OWUI_TOOL:-/app/backend/open_webui/utils/sqlcipher_tool.py}
OWUI_EXEC=${OWUI_EXEC:-docker exec -i $OWUI_CONTAINER}
PREFIX=hermes-webui-

fail() {
	echo "error: $*" >&2
	exit 1
}

command -v age >/dev/null 2>&1 || fail "age is not installed"
[[ $BACKUP_KEEP =~ ^[1-9][0-9]*$ ]] || fail "BACKUP_KEEP must be a positive number"
[[ $BACKUP_MAX_AGE_DAYS =~ ^[0-9]+$ ]] || fail "BACKUP_MAX_AGE_DAYS must be a number"

recipients=()
if [[ -n ${AGE_RECIPIENTS_FILE:-} ]]; then
	[[ -r $AGE_RECIPIENTS_FILE ]] || fail "cannot read $AGE_RECIPIENTS_FILE"
	grep -q 'AGE-SECRET-KEY' "$AGE_RECIPIENTS_FILE" &&
		fail "$AGE_RECIPIENTS_FILE holds a private key: give the public key only, and remove the private key from this machine"
	recipients+=(-R "$AGE_RECIPIENTS_FILE")
fi
for key in ${AGE_RECIPIENT:-}; do
	[[ $key == AGE-SECRET-KEY* ]] && fail "AGE_RECIPIENT is a private key: give the public key (age1...)"
	recipients+=(-r "$key")
done
[[ ${#recipients[@]} -gt 0 ]] || fail "set AGE_RECIPIENT or AGE_RECIPIENTS_FILE (a public key)"

mkdir -p "$BACKUP_DIR"
chmod 700 "$BACKUP_DIR"

# One run at a time: two would fight over the rotation.
lock="$BACKUP_DIR/.lock"
mkdir "$lock" 2>/dev/null || fail "another backup is running (or remove $lock)"

stamp=$(date -u +%Y%m%dT%H%M%SZ)
work="$OWUI_DATA_DIR/.backup-tmp-$stamp-$$"
out="$BACKUP_DIR/$PREFIX$stamp.tar.age"
partial="$out.partial"

cleanup() {
	# The snapshot is plaintext when the database is not encrypted: never leave it behind.
	$OWUI_EXEC rm -rf -- "$work" </dev/null || echo "warning: could not remove $work" >&2
	rm -f -- "$partial"
	rmdir "$lock" 2>/dev/null || true
}
trap cleanup EXIT
trap 'exit 130' INT TERM

$OWUI_EXEC mkdir -m 700 -- "$work" </dev/null
$OWUI_EXEC python "$OWUI_TOOL" snapshot --db "$OWUI_DATA_DIR/webui.db" --out "$work/webui.db" </dev/null

contents=(-C "$work" webui.db)
if [[ $INCLUDE_UPLOADS == 1 ]]; then
	contents+=(-C "$OWUI_DATA_DIR" uploads)
fi
$OWUI_EXEC tar -cf - "${contents[@]}" </dev/null | age "${recipients[@]}" -o "$partial"

[[ $(head -c 21 "$partial") == "age-encryption.org/v1" ]] || fail "the output is not an age file"
chmod 600 "$partial"
mv -- "$partial" "$out"
echo "backup written: $out ($(wc -c <"$out" | tr -d ' ') bytes)"

# Rotation. Names sort by date; the newest (this one) is never removed.
index=0
while IFS= read -r file; do
	index=$((index + 1))
	[[ $index -eq 1 ]] && continue
	if [[ $index -gt $BACKUP_KEEP ]] ||
		{ [[ $BACKUP_MAX_AGE_DAYS -gt 0 ]] && [[ -n $(find "$file" -mtime +"$BACKUP_MAX_AGE_DAYS") ]]; }; then
		rm -f -- "$file"
		echo "removed old backup: $file"
	fi
done < <(find "$BACKUP_DIR" -maxdepth 1 -type f -name "$PREFIX*.tar.age" | sort -r)
