# Changelog

All notable changes to this project are documented here. Format loosely
follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

Versions before 0.5.0 predate this changelog, so their history isn't
reconstructed here beyond what's implied by the 0.5.0 entry below.

## [0.6.0] — 2026-08-13

### Added
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
