# Changelog

All notable changes to this project are documented here. Format loosely
follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

Versions before 0.5.0 predate this changelog, so their history isn't
reconstructed here beyond what's implied by the 0.5.0 entry below.

## [0.7.5] — 2026-08-14

### Added
- **Safe exit button.** A new "Exit" button in the header stops the Anvil
  server itself (not a pacman action) via a new `POST /api/exit`. It
  refuses with a `409` while any pacman/yay transaction is still running —
  the frontend also blocks the click client-side — so it can never kill
  the server out from under a live install/removal. Otherwise it signals
  its own process to shut down cleanly (closing connections via uvicorn's
  normal signal handling rather than a hard kill) and the page swaps to a
  small "Anvil has stopped" screen.

## [0.7.4] — 2026-08-13

### Added
- **Interactive dependency graph.** A new "Dependency graph" tab renders
  an Obsidian-style local graph of installed packages: focus a package (by
  search, or a "Graph" button on any row in Installed) and click nodes to
  expand what they depend on and what depends on them, one hop at a time.
  Nodes are draggable, the canvas is zoomable/pannable, node size scales
  with connection count, and color distinguishes the focused package,
  explicitly-installed packages, and pulled-in dependencies. Expansion is
  capped at 20 neighbors per direction per click so a heavily-depended-on
  package like `glibc` doesn't blow up the layout. Built on `d3-force`
  (vendored locally under `frontend/vendor/`, no CDN at runtime) and reuses
  the existing `/api/package_info` endpoint — no new backend endpoint
  needed.
- **Remove orphaned packages.** The dashboard's "Orphaned packages" stat
  card now shows a "Remove orphaned packages" button whenever the count is
  above zero. `/ws/remove_orphans` re-checks the orphan list live via
  `pacman -Qtdq` right before acting (never trusts a stale count) and
  removes them with `pkexec pacman -Rns`, `-s` so a package that becomes
  orphaned by the same removal is swept up in one run instead of needing a
  second pass.

## [0.7.3] — 2026-08-13

### Changed
- **AUR install password prompt now appears in the browser.** `yay`'s
  internal `sudo` call for an AUR build's final install step used to
  require a system askpass dialog (`ssh-askpass`, `ksshaskpass`, etc.) to
  avoid hanging the launching terminal. Anvil now supplies its own askpass
  helper script per run: it connects back to a Unix socket the server
  opens just for that transaction, relaying the password prompt into the
  same transaction drawer used for pacman's other prompts as a masked
  field, with the typed password written straight back down the socket.
  Nothing is written to disk, logged, or echoed into the transaction log —
  no system askpass helper needs to be installed anymore.

## [0.7.2] — 2026-08-13

### Added
- **Interactive dependency tree.** The Updates view's expandable package
  details now render "Depends On" / "Required By" as a lazily-expandable
  chip tree instead of a flat list: clicking a chip fetches that package's
  own `/api/package_info` on demand and nests its dependencies underneath.
  Fan-out is capped at each level (20 chips at the root, 15 per nested
  level), and cycles are detected via the ancestor chain and rendered as an
  inert chip instead of recursing.
- **Cache cleanup buttons.** `/ws/clean_cache` (`pkexec paccache -rk1`) and
  `/ws/clean_aur_cache` (`yay -Sc`) trim old cached package files for repo
  and AUR packages respectively, matching pamac-aur's "clear old build
  files" option.

### Fixed
- **AUR installs could fail with "a terminal is required... or configure
  an askpass helper" even with a graphical askpass helper installed.**
  `_find_askpass()` only searched `PATH`, but Arch's `x11-ssh-askpass`
  package installs its binary under `/usr/lib/ssh/` instead of a `PATH`
  bin directory, so it was never found and `SUDO_ASKPASS` was never set.
  That fixed location is now checked too.

## [0.7.0] — 2026-08-13

### Added
- **Updates table with expandable package details.** The Updates view's
  repo and AUR sections are now a proper table (package / current version /
  new version) instead of bare rows. Clicking a row expands it and lazily
  fetches (and caches) that package's description, dependencies, and
  reverse dependencies ("Required By") from the new `/api/package_info`
  endpoint — nothing is fetched up front, so this scales fine even with
  many pending updates. A full interactive dependency graph is intentionally
  out of scope for this pass; this lays the data-layer groundwork for one.
- **`GET /api/package_info?name=`**: description/deps/reverse-deps for a
  single package, via `pacman -Qi` (falls back to `-Si`, then `yay -Si` for
  AUR packages not yet installed).
- **Installable package.** Restructured into `src/anvil/` (`app.py` +
  `cli.py`), with a `pyproject.toml` exposing an `anvil` console-script
  entry point. `pip install .` (or `pip install -e .` for development) now
  gets you a working `anvil` command instead of needing to run from a
  checkout with `run.sh`.
- **Arch packaging**: `packaging/PKGBUILD`, building via `python-build`/
  `python-installer` per current Arch Python packaging guidelines, laying
  the groundwork for `yay -S anvil` once published to the AUR.
- **CI** (`.github/workflows/ci.yml`): installs the package and lints the
  PKGBUILD with `namcap` on every push, to catch packaging breakage before
  a release.
- **AUR publish workflow** (`.github/workflows/aur-publish.yml`): on any
  `v*` tag, bumps `pkgver`/`sha256sums` in the PKGBUILD and pushes to the
  AUR automatically.

### Changed
- `run.sh` is now a thin dev convenience wrapper (`pip install -e .` then
  `anvil`) instead of duplicating server-launch logic; the old
  `backend/`/`frontend/` directories are gone, replaced by `src/anvil/`.

### Fixed
- **AUR "Upgrade all" did nothing.** The frontend already sent
  `{"upgrade_all": true, ...}` to `/ws/aur_install`, but the backend
  handler only ever looked at `packages` — with no individual AUR package
  checked, the list was empty and the socket just closed with no action
  taken.
- **AUR actions could hang or silently default instead of prompting.**
  `/ws/aur_install` and `/ws/aur_remove` ran `yay` with `--noconfirm`,
  which only suppresses *pacman's* prompts — yay's own AUR-build menus
  (exclude packages, clean build, show diffs, edit PKGBUILD) aren't
  covered by it and were left blocked on a piped stdin with no TTY to
  answer them. Removed `--noconfirm` from the yay-facing actions so these
  menus route through the same prompt-relay UI already used for pacman's
  conflict prompts.
- **PKGBUILD `depends` referenced a nonexistent package.** Arch's uvicorn
  package is named `uvicorn`, not `python-uvicorn` (which doesn't exist) —
  caught by a real `makepkg` build in an `archlinux:base-devel` container
  before it could break the actual AUR package.
- **PKGBUILD didn't install the license file** to the
  `/usr/share/licenses/anvil/` path Arch packaging expects, flagged by
  `namcap`; `package()` now installs it explicitly.
- **`sudo` password prompts could hang the launching terminal instead of
  reaching the UI.** `yay` shells out to plain `sudo` (not `pkexec`) for an
  AUR build's final install step, and `sudo` writes its password prompt
  directly to `/dev/tty`, bypassing stdout/stderr — completely invisible to
  the prompt-relay this app already had. Subprocesses are now started with
  `start_new_session=True`, detaching them from the launching terminal so
  `sudo` can no longer reach it; when a graphical askpass helper
  (`ssh-askpass`, `ksshaskpass`, etc.) is installed, `SUDO_ASKPASS` is set
  so `sudo` prompts through that instead of failing.
- **A false "prompt" could pop up mid-transaction and swallow a later
  answer.** The idle-timeout heuristic that detects an unanswered prompt
  (no output for 0.4s) can't tell "blocked waiting on stdin" apart from
  "pacman/yay is just still computing" (resolving dependencies, verifying
  signatures) — both look identical from the outside. A slow computation
  could trigger a phantom prompt; an Enter sent to dismiss it then queued
  into stdin and got consumed by the *next real* prompt instead ("Proceed
  with installation? [Y/n]"), silently defaulting it to yes. The idle
  timeout now only fires as a genuine prompt if the leftover text also has
  the shape of one (ends in `[Y/n]`, `[y/N]`, yay's `==>` menu marker, or
  `:`); otherwise it keeps waiting.
- **AUR pkgname `anvil` was already taken** by an unrelated project —
  the first real publish attempt (tagged as `v0.6.0`, which is why version
  numbering jumps straight to 0.7.0 here) failed with a permission-denied
  push for exactly this reason. Renamed the AUR package to `anvil-manager`
  (`provides`/`conflicts` on `anvil` so it's still found under the
  project's real name); the `anvil` command and Python package name are
  unaffected.
- **PKGBUILD's `build()`/`package()` assumed the downloaded tarball
  extracts to `$pkgname-$pkgver`**, but GitHub's archive keeps the repo's
  own casing (`Anvil-$pkgver`) regardless of pkgname or the URL's casing —
  a real, previously-undetected bug (an earlier "real build" test used a
  self-made tarball with a matching prefix, which masked it). Caught by
  building against the actual GitHub release tarball for `v0.6.0`; fixed
  via an explicit `_srcdir` variable instead of relying on `$pkgname`.

## [0.5.0] — 2026-08-13

### Added
- **Interactive prompt support** for privileged pacman actions (`sync`,
  `refresh_keyrings`, `apply`, `remove`). Previously these ran with
  `--noconfirm`, which doesn't wait for anything — it silently takes the
  default answer to pacman's own prompts and moves on, so a package
  conflict (`X and Y are in conflict, remove Y?`) failed outright instead
  of being something you could resolve. The backend now detects an
  unanswered prompt in the process output and relays it to the browser,
  which renders it inline in the transaction drawer (Yes/No for `[Y/n]`
  / `[y/N]` prompts, free text otherwise) and writes your answer back to
  the process's stdin.
- **"Refresh keyring" action** (`/ws/refresh_keyrings`): runs
  `pacman-key --refresh-keys` followed by a resync of `archlinux-keyring`,
  for when the local keyring is too stale for a normal upgrade to
  self-heal (the classic "invalid or corrupted package (PGP signature)"
  failure).
- **`/ws/remove` endpoint.** This was already referenced by the frontend
  and listed in the backend's own docstring, but never actually
  implemented — clicking "Remove" on an installed package silently did
  nothing before this.

### Changed
- **Redesigned the frontend UI**: replaced the left sidebar with a
  centered, sticky top navbar; modernized cards, spacing, typography, and
  button/checkbox styling throughout. Layout was left-aligned and dated
  before this pass.
- **Updates view**: replaced per-package checkboxes with a single
  "Upgrade all N" toggle per section (Repository / AUR). The checkboxes
  implied you could pick individual packages to update, which Arch
  doesn't support safely — a full `pacman -Syu` is always what actually
  runs, so the UI now says that instead of pretending otherwise.
- `/ws/apply` and `/ws/refresh_keyrings` no longer pass `--noconfirm`,
  now that prompts are handled interactively instead of being silently
  defaulted (see Added, above).

### Fixed
- The Tailwind CDN script tag pointed at
  `cdnjs.cloudflare.com/.../tailwindcss/3.4.1/tailwind.min.js`, which now
  404s (cdnjs dropped the old standalone browser build). The UI had
  consequently been rendering **completely unstyled** — this, not a
  design choice, was the actual cause of it looking dated and
  left-aligned. Switched to the official `cdn.tailwindcss.com` Play CDN.
- Update checkboxes couldn't be unchecked once more than one was selected:
  the change handler recomputed group state with
  `.some(checkbox => checkbox.checked)`, so as long as any other box in
  the group was still checked, the one you'd just unchecked snapped right
  back. (Superseded by the per-section toggle redesign above, but noting
  the root cause since it was reported as its own bug first.)
