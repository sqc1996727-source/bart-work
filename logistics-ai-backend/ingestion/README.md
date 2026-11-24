# Workbench ingestion package

The host application owns HTTP routing, session authentication, multipart
decoding, and the SQLite connection. After initializing the host database, it
must call:

```python
from ingestion import configure

configure(
    db_factory=db,
    data_dir=DATA_DIR,
    audit_fn=audit,
    state_keys=STATE_KEYS,
    now_fn=now_iso,
)
```

Handlers fail closed with `503 ingestion_unavailable` before configuration.
Only the authenticated host user mapping is passed to a handler. The package
does not run a server, read credentials, or trust user supplied identities.

## Waybill spreadsheet import

The flow is preview followed by an explicit, idempotent commit:

1. `handle_preview(user, filename, content_type, data)` validates and parses a
   `.csv` or `.xlsx` file. It returns a short lived `preview_id`, source SHA-256,
   row count, and a preview of the parsed V9 rows.
2. `handle_commit(user, preview_id, idempotency_key)` commits the entire batch
   in one SQLite transaction. It checks the preview owner, tenant, expiry,
   saved state revision, current waybill numbers, and the idempotency key.

`user.role` must be `owner_finance` for both calls. The normalized input must
provide all V9-required fields: `number`, `date`, `receiver`, `phone`,
`address`, `destination`, `cargo`, `qty`, `weight`, `amount`, and `carrier`.
`remark` and `feeWarehouse` are optional. Chinese V9 field names and common
English aliases are accepted. Rows with missing values, invalid dates,
non-finite/negative amounts, invalid weights, duplicate numbers, unknown or
duplicate columns, or collisions with existing waybills fail the whole batch.
The legacy nine-column settlement export lacks required recipient/contact and
carrier fields, so it is preview-rejected and cannot be imported as a complete
waybill.

CSV is UTF-8/UTF-8-BOM. XLSX parsing reads the first worksheet using Python's
standard library; macros, formulas, encrypted workbooks, unsafe archive paths,
and expansion beyond the configured limits are rejected. Source files are not
stored. Parsed preview rows expire after 20 minutes and are deleted at commit
or when expired. Commit audit records the original source SHA-256, row numbers,
and count without copying source cell contents into the audit log.

## Credential images

The host passes the already authenticated user, waybill number, sanitized file
metadata, and image bytes to the attachment handlers. Only JPEG, PNG, and WebP
are allowed, with a 10 MiB file limit and signature/structure checks. Files
are written to `$DATA_DIR/attachments/<random-id>.<ext>` with private directory
and file permissions. User filenames are metadata only and never form storage
paths. SHA-256, byte count, type, owner, tenant, and waybill association are
stored in the host SQLite database and recorded in the audit trail.

Employees may upload, list, download, and delete attachments only for their
own `created_by` waybills. Dispatchers and finance owners may operate on any
waybill in their authenticated tenant. Missing and unauthorized tickets or
attachments use the same not-found response.

The host HTTP contract agreed with the backend is:

- `POST /api/waybills/import/preview`, multipart `file`
- `POST /api/waybills/import/commit`, JSON `{ "preview_id": "..." }` and
  `Idempotency-Key`
- `POST /api/waybills/{number}/attachments`, multipart `file`
- `GET /api/waybills/{number}/attachments`
- `GET /api/attachments/{id}`
- `DELETE /api/attachments/{id}`

The HTTP layer must allow at least 10 MiB plus multipart framing overhead;
the regular JSON request limit is not sufficient for these routes. Binary
attachments are included in `scripts/backup_demo.py` under `attachments/`;
the manifest records each file's SHA-256 and byte size. The matching
`scripts/restore_demo_to_new_dir.py` verifies those values and restores into a
new, isolated data directory together with the SQLite metadata database.

## Isolated checks

Run from the candidate root:

```sh
python3 -m unittest ingestion.test_ingestion -v
```

Tests create a temporary SQLite database and filesystem root with synthetic
users and waybills. The host HTTP tests launch `app.py` with their own temporary
`DATA_DIR` and random loopback port. They do not read or modify the persistent
demo/production data tree.
