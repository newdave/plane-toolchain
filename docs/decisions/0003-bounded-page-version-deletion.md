# 0003: Bounded `PageVersion` deletion in Pages update

- **Status:** Accepted
- **Date:** 2026-11-14
- **Follow-up tasks:** PTC-108 (this record), PTC-86 (parent review item)

## Context

`ce_page` update replaces a page body. Before it writes, the Pages backend
(`src/plane_toolchain/pages_backend.py`, `patch()`) saves the body being replaced as a
`PageVersion` row. Plane keeps a bounded history per page, and the backend mirrors that
bound by deleting the oldest rows of the same page after each snapshot.

AGENTS.md says not to add deletion silently. This deletion is part of the deployed payload
and had no decision record. This ADR records it.

## Decision

The update path may delete `PageVersion` rows, and only under these limits:

- **Bound:** `VERSION_LIMIT = 20`. After the snapshot is created, at most 20 versions of
  the page remain.
- **Scope:** only versions of the page being updated. The query filters on
  `page_id=row.id`. The backend never selects or deletes versions of any other page.
- **Order:** newest first by `last_saved_at`. Versions beyond the 20th, that is the oldest,
  are deleted.
- **Trigger and atomicity:** only during an update that has just created a snapshot, inside
  the same transaction as the page write, after the row lock. A refused or failed update
  rolls back, so it neither adds nor deletes a version.
- **Nothing else:** no other model, page or code path deletes. Deleting pages, deleting
  versions on request, and deleting relations stay unsupported.

## Why this matches upstream

The code comment on `VERSION_LIMIT` says "Plane's track_page_version keeps at most this
many versions per page", and the comment in `patch()` says "Same cap as Plane's
track_page_version, oldest first". `track_page_version` is Plane's own background task that
snapshots a page and trims its history. The helper does not queue that task: in CE v1.4.3 it
reads a missing `Page.description` attribute and, if fixed, would overwrite the snapshot in
place (`docs/extensions.md`). Without its own cap, the explicit snapshots would grow the
history past the bound Plane applies to every other edit path. The helper therefore applies
the same cap itself.

The figure 20 and the oldest-first order come from those code comments. They were not
re-verified against the upstream Plane source for this record. Verify them there before
changing `VERSION_LIMIT`.

## Consequences

- The behaviour is unchanged. This record needs no redeploy.
- `tests/test_pages_backend_patch.py` covers the cap: it prunes to 20 oldest first inside the
  transaction, keeps versions under the cap, and never deletes another page's versions (the
  test seeds older versions of a second page and fails if the page filter is removed).
- Changing `VERSION_LIMIT`, the scope or the order is a deletion change. It needs a new
  decision record and a redeploy of the Pages payload.

## Alternatives considered

- **No cap.** No deletion, but history grows without bound on every update. Rejected.
- **Queue Plane's `track_page_version`.** Breaks on CE v1.4.3 and could overwrite the
  snapshot. Rejected (`docs/extensions.md`).
- **Cap by age instead of count.** Differs from Plane's per-page count cap. Rejected.
