# Anvil

A minimal, self-hosted pacman GUI: a FastAPI backend that shells out to the
real `pacman`/`checkupdates`/`yay` binaries, and a Tailwind-based browser
frontend that talks to it. Nothing here reimplements pacman's logic — every
action you take runs the actual CLI command and streams its actual output
back to you, prompts included.

## What it needs on your system

- **Python 3.10+** (Arch ships something newer by default, so this is almost
  certainly already satisfied)
- **pacman-contrib**, for the `checkupdates` command used to check for
  repo updates without needing root:
  ```
  sudo pacman -S pacman-contrib
  ```
- **yay** (optional), for AUR search/updates/install/remove. Without it,
  the AUR-related panels just report it's missing — everything else still
  works:
  ```
  git clone https://aur.archlinux.org/yay.git && cd yay && makepkg -si
  ```
- **polkit**, with a graphical authentication agent running (this is already
  the case if you're on GNOME, KDE Plasma, or XFCE with its default polkit
  agent). Privileged actions (sync, apply, remove, keyring refresh) run via
  `pkexec`, which will pop your desktop's normal password prompt — Anvil
  never asks for or stores your password itself.
- **A graphical askpass helper** (optional, but recommended if you use AUR
  actions): `yay` shells out to plain `sudo` — not `pkexec` — for the final
  install step of an AUR build. Anvil detects and uses one of
  `ssh-askpass`, `x11-ssh-askpass`, `lxqt-openssh-askpass`, `ksshaskpass`,
  or `seahorse-ssh-askpass` if present, so `sudo`'s password prompt appears
  as a graphical dialog instead of failing outright (see "Interactive
  prompts" below for why this is needed). KDE Plasma ships `ksshaskpass`;
  GNOME users can install `seahorse`.

## Running it

The easiest path once packaged for Arch will be:

```bash
yay -S anvil
anvil
```

Until then, install it from source with pip:

```bash
cd anvil
pip install -e .
anvil
```

This installs FastAPI and uvicorn plus the `anvil` console script, and
starts the server on `http://127.0.0.1:8000`, opening automatically in your
browser.

For a no-install dev loop, `./run.sh` sets up a local `venv/`, installs
Anvil into it in editable mode, and runs `anvil` — useful if you don't want
to touch your system Python environment.

To stop it, press `Ctrl+C` in the terminal it's running in.

## How it works

```
anvil/
├── src/anvil/
│   ├── app.py              FastAPI app — read endpoints + WebSocket actions
│   ├── cli.py               `anvil` console-script entry point
│   └── frontend/
│       └── index.html       Tailwind UI, talks to the backend via fetch/WS
├── packaging/
│   └── PKGBUILD             Arch package build recipe
├── pyproject.toml           Package metadata, dependencies, entry point
├── run.sh                    No-install dev convenience wrapper
├── CHANGELOG.md
└── README.md
```

**Read-only data** (`GET /api/...`) runs unprivileged commands directly:
- `/api/status` → `pacman -Q`, `-Qe`, `-Qdtq` (orphans)
- `/api/updates` → `checkupdates` (repo updates)
- `/api/aur_updates` → `yay -Qua` (AUR updates)
- `/api/search` → `pacman -Ss <query>` (repo search)
- `/api/aur_search` → `yay -Ss <query>` (AUR search)
- `/api/installed` → `pacman -Q` / `pacman -Qe`
- `/api/package_info?name=` → `pacman -Qi` (falls back to `-Si`, then
  `yay -Si`); description, dependencies, and reverse dependencies for one
  package, fetched lazily when you expand a row in the Updates table
- `/api/history` → parses `/var/log/pacman.log`

**Privileged actions** (`WS /ws/...`) run via `pkexec`, streaming stdout back
to the browser line by line as it happens:
- `/ws/sync` → `pkexec pacman -Sy`
- `/ws/refresh_keyrings` → `pkexec pacman-key --refresh-keys`, then
  `pkexec pacman -Sy archlinux-keyring`
- `/ws/apply` → `pkexec pacman -Syu` (if any update is selected) and/or
  `pkexec pacman -S <packages>` (for new installs)
- `/ws/remove` → `pkexec pacman -R <package>`
- `/ws/aur_install` → `yay -S <packages>`
- `/ws/aur_remove` → `yay -R <packages>`

### Interactive prompts

All pacman- and yay-facing actions (`sync`, `refresh_keyrings`, `apply`,
`remove`, `aur_install`, `aur_remove`) run **without** `--noconfirm`.
That's deliberate: with `--noconfirm`, pacman doesn't wait for anything —
it silently takes the default answer to its own prompts and moves on,
which for a package conflict means silently declining to resolve it and
failing outright. `--noconfirm` also doesn't suppress yay's own AUR-build
menus (exclude packages, clean build, show diffs, edit PKGBUILD), so it
just leaves yay blocked on stdin with no TTY to answer it — worse than
not passing the flag at all.

Instead, the backend watches each command's output for text left sitting
with no trailing newline and nothing more coming for a short idle window —
and, to avoid mistaking pacman/yay just thinking (resolving dependencies,
verifying signatures) for an actual unanswered prompt, only treats that as
one if the leftover text also ends in the shape a real prompt uses
(`[Y/n]`, `[y/N]`, yay's `==>` menu marker, or a trailing `:`). A real
prompt is relayed to the browser and rendered inline in the transaction
drawer — Yes/No buttons for pacman's `[Y/n]` / `[y/N]` prompts, a free-text
field for anything else (including yay's own menus). Your answer is
written straight back to the process's stdin, so a "Proceed with
installation?", a "`pkgA` and `pkgB` are in conflict, remove `pkgB`?", or
a "Diffs to show?" from yay is something you can actually answer instead
of a hard failure or a hang.

One prompt this mechanism *can't* see: `yay` shells out to plain `sudo`
(not `pkexec`) for an AUR build's final install step, and `sudo` writes
its password prompt directly to `/dev/tty` rather than stdout/stderr,
bypassing this app's output capture entirely — left alone, that silently
hangs whatever terminal launched the server. Anvil's subprocesses run
detached from that controlling terminal (so `sudo` can't reach it) and set
`SUDO_ASKPASS` to a graphical askpass helper if one is installed, so `sudo`
prompts through that instead. Without one installed, an AUR action needing
`sudo` will fail with a clear "no askpass program specified" error in the
transaction log rather than hanging silently.

## Why "Apply" always does a full upgrade when any update is checked

Arch explicitly does not support partial upgrades — installing some sync-DB
packages while leaving others behind can break dependency resolution in ways
that are hard to undo. So rather than quietly doing something Arch itself
warns against, Anvil always runs a full `pacman -Syu` if you've enabled the
"Upgrade all" toggle for pending repo updates, and handles brand-new package
installs as a separate, explicit transaction alongside it. Updates can't be
picked individually for the same reason — each update section is an
all-or-nothing toggle, not a per-package checklist.

## Notes and known limitations

- This is a prototype built for one machine, one user, listening only on
  `127.0.0.1` — it is **not** hardened for exposing on a network.
- AUR helper support is `yay`-specific; if you use `paru` or another helper
  instead, the AUR panels will report `yay` as missing.
- Manual keyring refresh (`Refresh keyring` button) only covers `pacman-key
  --refresh-keys` + resyncing `archlinux-keyring`. It won't help if your
  keyring is broken badly enough that pacman can't even verify that package
  — at that point you're into manual `pacman-key --init` territory, outside
  what a GUI button can safely automate.

See [CHANGELOG.md](CHANGELOG.md) for version history.
