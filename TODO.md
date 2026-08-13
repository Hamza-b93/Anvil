# TODO

## Interactive dependency graph

The Updates view's expandable package details (added in v0.7.0) show
"Depends On" / "Required By" as flat chip lists via `/api/package_info`.
Follow-up: a visual, interactive dependency graph/tree instead of (or in
addition to) the flat lists.

Deferred deliberately when the updates table shipped — "simple list first,
graph later" — so the data layer (`/api/package_info`) already exists;
this is purely a rendering/UX layer on top of it.

Things to work out before implementing:
- **Fan-out.** Common libraries (e.g. `glibc`, `bash`) can have hundreds of
  reverse dependencies (`Required By`) — a naive full graph would be
  unreadable and slow to lay out. Needs a depth/count cap or progressive
  expansion (click a node to load its neighbors) rather than rendering
  everything at once.
- **Cycles.** Dependency relationships can form cycles; layout needs to
  handle that without infinite recursion.
- **Rendering approach.** Options: force-directed graph (e.g. a small D3
  force layout), or a collapsible tree (simpler, avoids force-layout jitter
  but less "networky"). Given the artifact/dataviz guidance already used
  elsewhere in this codebase's tooling, lean toward something that stays
  legible in both light and dark themes without extra dependencies if
  possible.
- **Data cost.** Building a multi-level graph means recursively calling
  `/api/package_info` (or a new batch endpoint) for each node's neighbors —
  needs to stay lazy/on-demand like the current expand-a-row behavior, not
  eagerly fetch the whole transitive closure.
