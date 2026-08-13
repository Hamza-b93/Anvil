# TODO

Nothing outstanding right now.

## Done

- **Interactive dependency graph** — the Updates view's expandable package
  details now render "Depends On" / "Required By" as a lazily-expandable
  chip tree instead of a flat list: clicking a chip fetches that package's
  own `/api/package_info` on demand and nests its dependencies underneath.
  Fan-out is capped at each level (20 chips at the root, 15 per nested
  level, with a "+N more" count), cycles are detected via the ancestor
  chain and rendered as an inert "↺" chip instead of recursing, and it
  reuses the existing chip styling/CSS variables so it stays legible in
  both themes without any new dependencies.
