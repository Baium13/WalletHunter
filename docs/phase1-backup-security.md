# Phase 1 backup and logging boundaries

The runtime backup includes credentials, Telegram sessions, profile state and
SQLite snapshots. **tar.gz is compression, not encryption.** Treat the entire
archive as a secret. Do not upload it to Git, tickets or public artifact stores.

The backup script now refuses symlink inputs/targets and enforces directory
0700 and archive 0600 on POSIX, including an existing backup directory. Archive
members are 0600. Windows requires an operator-managed private NTFS ACL; chmod
does not establish equivalent Windows access control. These changes do not
retroactively rewrite older archives or production files.

Retention remains KEEP ALL; no automatic deletion or rotation is introduced.
An operator must monitor free space and explicitly authorize removal after
verifying a recoverable off-host copy. Older archives remain sensitive.

Encryption at rest/off-host is an OPEN operational requirement: choose key
custody, access/revocation, offline key recovery and restore rehearsal before
adding encryption. Losing the encryption key can make all backups unusable.
No encryption key contract or credential rotation is silently introduced here.

Normal bot exception logging emits operation context, user ID where applicable,
and exception class, not arbitrary exception text or Telegram token-bearing
URLs. Debug/HTTP wire logging must remain disabled in production. Application
logs and backups are not suitable for public telemetry.

Restore still requires the exact source/dependency revision, private .env,
state.json, all SQLite databases (including executions and AI proposals),
Telegram session and service configuration. Stop writers during restoration;
preserve financial provenance and reconcile exchange evidence before permitting
new risk. SQLite snapshots are per-database consistent, not a distributed
transaction across JSON and all databases. Follow the Phase 0 recovery checklist.
