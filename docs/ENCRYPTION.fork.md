# Encryption at rest (fork)

Two independent pieces, both **off until an operator sets them up**:

1. `webui.db` encrypted with SQLCipher.
2. Backups encrypted with [age](https://age-encryption.org) for a public key.

## What this protects, and what it does not

SQLCipher protects the database **file**: a copy of the disk, of the volume or of a backup is
unreadable without the password.

It does **not** protect against:

- an Open WebUI administrator, or the application's own API;
- someone with root on the host while the container runs (the password is available to the container);
- the files next to the database, which stay in clear: `uploads/` (uploaded files), `vector_db/`
  (Chroma: text chunks of those files and of memories), `audit.log`, caches, and the Docker logs;
- the password itself being stored on the same disk. A password file on the same volume or in the
  same disk snapshot protects very little: keep it elsewhere (a secret mounted from outside the
  volume, ideally not captured by the same snapshot) and keep a copy off the machine.
  **If the password is lost, the database is lost.**

## Settings

| Variable | Meaning |
|---|---|
| `DATABASE_TYPE=sqlite+sqlcipher` | Use `DATA_DIR/webui.db` through SQLCipher. Leave `DATABASE_URL` unset. |
| `DATABASE_PASSWORD_FILE=/run/secrets/webui_db_password` | Recommended: file holding the password (surrounding whitespace is ignored). |
| `DATABASE_PASSWORD=…` | Alternative. It is visible in `docker inspect`. |

The image needs `sqlcipher3-wheels` (in `requirements.txt`). An image built as an overlay on an
older one must install it: `pip install sqlcipher3-wheels==0.5.7`.

Use a long random password, for instance `openssl rand -base64 32`.

Cost: opening a connection derives the key (about 50 ms on an Apple M3); connections are pooled. The
effect on query time under real load has not been measured.

## Converting an existing database

Setting the variables on a plaintext database does not convert it: the application refuses to
start. Convert while the application is stopped.

For a database of about 320 MB, a conversion took about 3.5 s on a developer machine (Apple M3).
Expect more on a small virtual machine. With stopping and starting the container, plan a
**2 to 4 minute interruption** (an estimate, not measured on the production host). The conversion
needs free space for twice the database size.

```sh
# 1. Take a backup first (below), and check that you can decrypt it.
# 2. Create the password file outside the data volume, mode 600, and keep a copy off the machine.
docker compose stop open-webui

# 3. Convert. The original is renamed webui.db.plaintext-<date>, never deleted.
docker compose run --rm --no-deps \
  -v /root/openwebui/secrets/webui_db_password:/run/secrets/webui_db_password:ro \
  -e DATABASE_PASSWORD_FILE=/run/secrets/webui_db_password \
  --entrypoint python open-webui \
  /app/backend/open_webui/utils/sqlcipher_tool.py encrypt --db /app/backend/data/webui.db --swap

# 4. In docker-compose.yml, add to the service: the same volume line, and
#      - DATABASE_TYPE=sqlite+sqlcipher
#      - DATABASE_PASSWORD_FILE=/run/secrets/webui_db_password
docker compose up -d --no-deps open-webui
```

The tool refuses to run if the database is open elsewhere, checks the source, compares the schema,
`user_version` and the row count of every table between source and copy, and runs
`cipher_integrity_check`. On any failure it removes the copy and leaves the original in place.

After checking the application (sign in, open an old chat, send a message, search), **delete the
plaintext file** `webui.db.plaintext-<date>`, and the older plaintext backups: as long as they exist
the encryption protects nothing. Deleting does not scrub the blocks on disk or in old snapshots.

`utils/sqlcipher_tool.py status --db …` tells which format a file has.

### Going back

- **Before anything was written** with the encrypted database: stop the application, put
  `webui.db.plaintext-<date>` back as `webui.db` (remove `webui.db-wal` and `webui.db-shm`), remove
  the two variables, start.
- **Later**, to keep what was written since: stop the application, run the tool with
  `decrypt --db … --swap` (same password variable), remove the two variables, start.
- Rolling back to an image **without** this change requires a plaintext database: decrypt first.

## Scripts that open `webui.db` themselves

Anything using the `sqlite3` module or command on this file stops working once it is encrypted
(`file is not a database`). In a script run inside the container, replace the connection with:

```python
import sqlcipher3
db = sqlcipher3.connect('/app/backend/data/webui.db')
db.execute("PRAGMA key = '%s'" % open('/run/secrets/webui_db_password').read().strip().replace("'", "''"))
```

Known users to adapt before switching (check the host for others, e.g. `grep -rl webui.db /root`):

- the deployment kits and `switch-*.sh` scripts on the Hermes host (online backup of the database);
- manual preset and sub-model imports (for instance the Office sub-models);
- the read-only usage and audit counts run through `docker exec -i open-webui python -`;
- the one-off scripts of the preset dumps (`dump*.py`, `srv_apply*.py`, `srv_backup*.py`).

Tools that go through the HTTP API (Wiki.js export, Teams bridge, GitLab bot) are not affected.
The admin "download database" export returns the encrypted file.

## Encrypted backups: `scripts/hermes-backup.sh`

Run on the Docker host. It takes a consistent snapshot inside the container (`VACUUM INTO`, the
application keeps running), streams it with `tar` to `age`, writes
`hermes-webui-<UTC>.tar.age` (mode 600, directory 700) and rotates old files. It works with a
plaintext or an encrypted database.

Requirements: `age` on the host, an image containing `utils/sqlcipher_tool.py`, and free space in
the data volume for one copy of the database.

**The key pair is created on another machine** (`age-keygen -o hermes-backup.key`). Only the public
key (`age1…`) goes to the host; the private key stays offline, with a second copy. The script refuses
a private key.

```sh
AGE_RECIPIENT=age1… INCLUDE_UPLOADS=1 BACKUP_KEEP=7 BACKUP_MAX_AGE_DAYS=30 \
  /root/openwebui/hermes-backup.sh
```

| Variable | Default |
|---|---|
| `AGE_RECIPIENT` or `AGE_RECIPIENTS_FILE` | required |
| `BACKUP_DIR` | `/root/backups/encrypted` |
| `INCLUDE_UPLOADS` | `0` |
| `BACKUP_KEEP` | `7` |
| `BACKUP_MAX_AGE_DAYS` | `30` (`0`: no age limit; the newest backup is always kept) |
| `OWUI_CONTAINER` | `open-webui` |

Copy the files off the host: a backup that stays on the same disk does not survive its loss.
While the script runs, an unencrypted database has a temporary plaintext copy inside the data volume;
it is removed when the script ends (not if the host loses power: check for `.backup-tmp-*`).

Not included: `vector_db/` (can be rebuilt from the files, which takes time), `audit.log`,
`.webui_secret_key` (without it, stored OAuth tokens cannot be read after a restore: people connect
their tools again) and the database password.

### Restoring

```sh
mkdir restore && age -d -i hermes-backup.key hermes-webui-<UTC>.tar.age | tar -xf - -C restore
```

Then stop the application, replace `webui.db` (and `uploads/`) with the restored files, remove
`webui.db-wal` and `webui.db-shm`, and start. If the database was encrypted when it was saved, the
restored copy is still encrypted with the password of that time.

**Test a restore** after setting this up, and after any change of key or password.
