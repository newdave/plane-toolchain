# 0001: Neutral names for legacy `terrainlink` identifiers

- **Status:** Accepted; partly superseded by Removal (PTC-30)
- **Date:** 2026-11-12
- **Follow-up tasks:** PTC-26 (legacy-names module and guard), PTC-27 (Pages deploy),
  PTC-28 (MCP compat deploy), PTC-29 (prose and docs), PTC-23 (redeploy),
  PTC-30 (remove legacy reads)

## Context

This toolchain was extracted from TerrainLink. Its deploy helpers still write rollback
state under `terrainlink` names: backup containers and files, receipts, an image label, an
image tag and a backend module path. AGENTS.md and `docs/extensions.md` currently say these
names must be preserved, because a live host identifies its own rollback state by them.

The names are now misleading, but a plain rename would break `check`, `revert` and
reconcile on any host deployed before the rename. The change needs a migration policy, not a
silent refactor.

## Decision

### Neutral prefix

Use `plane-toolchain` (hyphenated) for names on the host and in Docker, and
`plane_toolchain` (underscored) for Python module names.

### Mapping

| Kind | Legacy name | Neutral name | Persisted host state? |
|---|---|---|---|
| Pages rollback container | `<name>-before-terrainlink-pages` | `<name>-before-plane-toolchain-pages` | Yes: revert and reconcile find it by name |
| Pages container label | `terrainlink.pages.source` | `plane-toolchain.pages.source` | Yes: `check` and apply detect the deployed digest by it |
| Pages derived image tag | `terrainlink-plane-pages:<digest16>` | `plane-toolchain-pages:<digest16>` | No: only the running container refers to it |
| Pages backend module (in image) | `/code/plane/api/terrainlink_pages.py`, `plane.api.terrainlink_pages` | `/code/plane/api/plane_toolchain_pages.py`, `plane.api.plane_toolchain_pages` | No: each build starts from the upstream image |
| MCP entry-point backup | `server.py.before-terrainlink-ce` | `server.py.before-plane-toolchain-ce` | Yes: the upstream original for revert |
| MCP receipt | `terrainlink-ce-receipt.json` | `plane-toolchain-ce-receipt.json` | Yes: digests, including pending state |
| MCP receipt temp file | `terrainlink-ce-receipt.json.tmp` | `plane-toolchain-ce-receipt.json.tmp` | No: harmless leftover, overwritten by the next write |

Retired-container names `<name>-pages-reverted-*` and `<name>-pages-failed-*` are already
neutral and do not change.

### Migration policy

> Historical: superseded by the Removal (PTC-30) section below; kept for the record.

1. **Read both, write new.** Every operation (`check`, `apply`, `revert`, reconcile,
   repair) accepts state under the legacy or the neutral name. New state is written only
   under the neutral name.
2. **One source of truth for names.** `src/plane_toolchain/legacy.py` is the only module
   that spells a legacy identifier, beside its neutral replacement. The remote programs ship
   these constants in their stdlib-only source; the host does not import `plane_toolchain`.
   A guard in `mise run check` rejects new `terrainlink` references elsewhere (PTC-26).
3. **Migrate on a successful write.** A successful apply or repair moves legacy state to
   the neutral name: the new label and module on the rebuilt container, the legacy Pages
   backup renamed after a successful promote, and for MCP a new receipt written atomically
   before the verified backup is moved and the legacy receipt removed. A kill at any step
   must leave state that `check`, `revert` and `apply` still accept (PTC-27, PTC-28).
4. **Ambiguity is refused.** If legacy and neutral state both exist and disagree (two
   rollback containers, or receipts or backups with different digests), the helper refuses
   with a reconcile message. It never picks one silently.
5. **No tool downgrade across the migration.** A release older than the migration reads
   only legacy names, so it cannot see migrated state. To roll the toolchain back, first
   `revert` the host with the current release, then downgrade.

### Deprecation window

> Historical: superseded by the Removal (PTC-30) section below; kept for the record.

Legacy reads stay until all of these hold, and the owner confirms them on PTC-30:

- a tagged release ships the read-both behaviour;
- the live host has been redeployed with that release (PTC-23) and `check` passes for both
  helpers;
- at least 30 days have passed since that redeploy;
- a read-only host inventory finds no legacy containers, images, labels, backups or
  receipts.

Removal (PTC-30) deletes the legacy read paths and `legacy.py`, tightens the guard to
CHANGELOG history only, and marks this ADR as superseded.

### Removal (PTC-30)

On 2026-11-13 the owner chose "Override the ADR now: remove the legacy reads and merge". This waived the deprecation window gates, which required that:

- a tagged release ships the read-both behaviour;
- the live host has been redeployed with that release (PTC-23) and `check` passes for both helpers;
- at least 30 days have passed since that redeploy;
- a read-only host inventory finds no legacy containers, images, labels, backups or receipts.

At that time no tagged release shipped the read-both code (v0.1.0 predates it), the host had not been redeployed, the 30-day window had not started, and the host still had legacy labels, receipts and backups.

The helpers now read and write only neutral names. One deviation from the plan above: `legacy.py` is kept, with four refuse-only names (the Pages backup suffix, the Pages source label, the MCP backup file and the MCP receipt). The Pages and MCP helpers refuse, before any change, when they find any of them (Pages `build` only creates an image and is not checked). The reason is safety. An unmigrated host must be refused, not misread as a clean host. The refusal points to commit `1cb6153`, the last read-both commit, whose owner tasks migrate such a host.

The tripwire fails closed. It lists containers with one `docker ps -a --filter name=<api>` and matches names exactly, and it inspects the API and the neutral backup only when listed. A failed `ps` or inspect raises, so the run refuses. It does not filter on the label across all containers: retired `-pages-reverted-*` containers keep the legacy label and stay allowed.

Migrate a refused host with plane-toolchain at commit `1cb6153`:

- Pages: run `revert` (it restores the original, consumes the legacy backup and parks the legacy derived container as an inert `<api>-pages-reverted-<ts>`). Run `apply` there only if `check` passes there; it refuses when the deployed digest differs. Then run `apply` with this release.
- MCP: run `apply` (it migrates the receipt and backup). MCP `revert` there does not migrate.
- Retired `-pages-reverted-*` containers keep the legacy label, are not checked and are left for owner cleanup.
- A Saltbox host's next build, from this release or the Saltbox role, must write the neutral label, or every operation refuses there.

The guard now allows legacy names only in `legacy.py`, this ADR, the CHANGELOG, the guard itself and the tripwire test fixtures.

### Provenance line

The README line "Extracted from TerrainLink under the MIT licence" is removed from the
README. The origin is recorded in the CHANGELOG, which the guard already exempts as
history. The MIT licence and its copyright notice in `LICENSE` are unchanged, so no
attribution is lost.

### Agent rule

> Historical: superseded by the Removal (PTC-30) section below; kept for the record.

The AGENTS.md rule "Preserve ... legacy rollback receipt names" was replaced by a rule
that treated legacy `terrainlink` names as read-only compatibility inputs, kept in
`src/plane_toolchain/legacy.py` and changed only through the migration policy above. The
rule also stated that the helpers still wrote legacy names until PTC-27 and PTC-28
landed, so nobody renamed them ahead of the migration.

## Consequences

- Deploy digests change, because `pages_backend.py`, `ce_tools.py` and the Dockerfile
  text change. The migration rides on the same owner redeploy as the review queue (PTC-23).
- Host state is mixed for a time: older retained containers and images keep legacy names
  until a later apply or an owner cleanup with explicit authority.
- The helpers gained read-both and refuse-on-ambiguity paths. These needed FakeDocker and
  kill-step tests for every legacy, mixed and neutral layout before any redeploy.
  Superseded by the Removal (PTC-30) section: the read paths are gone.
- Until PTC-26 landed, `legacy.py` did not exist and this ADR was the list of legacy
  names. Superseded by the Removal (PTC-30) section: `legacy.py` exists and holds the
  four refused names.

## Alternatives considered

- **Keep the legacy names forever.** No migration risk, but the misleading names stay in
  every receipt and container. Rejected.
- **Rename in one step, no legacy reads.** Breaks revert on any host deployed before the
  rename. Rejected.
- **Neutral prefix `ptc` or `plane-ce`.** Shorter, but `ptc` is the Plane task prefix and
  `plane-ce` suggests an upstream Plane artefact. `plane-toolchain` matches the package and
  repository name.
