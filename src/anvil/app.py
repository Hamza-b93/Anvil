"""
Anvil backend — a thin FastAPI wrapper around pacman.

Read-only endpoints (no privilege needed):
  GET  /api/status     system summary (package counts, orphans)
  GET  /api/updates     pending updates, via `checkupdates` (pacman-contrib)
  GET  /api/aur_updates     pending AUR updates, via `yay -Qua`
  GET  /api/search?q=   repo search, via `pacman -Ss`
  GET  /api/aur_search?q=   AUR search, via `yay -Ss`
  GET  /api/installed   installed packages, via `pacman -Q` / `-Qe`
  GET  /api/package_info?name=   description/deps/reverse-deps for one
                                  package, via `pacman -Qi` (falls back to
                                  `-Si`, then `yay -Si` for not-yet-installed
                                  AUR packages)
  GET  /api/history      recent transactions, parsed from /var/log/pacman.log
  POST /api/exit         safely shuts the server down (refuses while a
                          /ws/* transaction is in flight)

Privileged actions (prompt via polkit's pkexec, never store a password):
  WS   /ws/sync                pkexec pacman -Sy
  WS   /ws/refresh_keyrings    pkexec pacman-key --refresh-keys / pacman -Sy archlinux-keyring
  WS   /ws/apply                pkexec pacman -Syu / -S <pkgs>
  WS   /ws/remove              pkexec pacman -R <pkgs>
  WS   /ws/remove_orphans      pkexec pacman -Rns <orphan pkgs, via pacman -Qtdq>
  WS   /ws/aur_install        yay -S <pkgs>
  WS   /ws/aur_remove        yay -R <pkgs>
  WS   /ws/clean_cache        pkexec paccache -rk1 (old repo package cache)
  WS   /ws/clean_aur_cache    yay -Sc (old AUR build/cache files)

Everything here just shells out to the real pacman/yay binary and streams its
real stdout back to the browser line by line — there is no separate
"transaction engine", so behavior always matches the CLI you already trust.

pacman/yay commands run WITHOUT --noconfirm. That means pacman's own
confirmation and conflict-resolution prompts ("Proceed with installation?
[Y/n]", "Remove <pkg>? [y/N]", etc.) as well as yay's own AUR-build menus
("Packages to exclude?", "cleanBuild?", "Diffs to show?", etc.) actually
fire instead of being silently defaulted. stream_process detects these (any
output left unterminated, waiting on stdin) and relays them to the browser
as a {"type": "prompt"} message; the reply comes back over the same socket
and is written straight to the subprocess's stdin.
"""

import asyncio
import os
import re
import shutil
import signal
import subprocess
import tempfile
from pathlib import Path

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.staticfiles import StaticFiles
from fastapi.responses import JSONResponse

app = FastAPI(title="Anvil")

FRONTEND_DIR = Path(__file__).resolve().parent / "frontend"

# Bumped in stream_process for the lifetime of every /ws/* transaction, so
# /api/exit can refuse to shut down mid-pacman-run instead of killing the
# server out from under a live install/removal.
_active_transactions = 0


def run_cmd(cmd: list[str]) -> tuple[int, str, str]:
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        return proc.returncode, proc.stdout, proc.stderr
    except FileNotFoundError:
        return 127, "", f"{cmd[0]}: command not found"
    except subprocess.TimeoutExpired:
        return 124, "", f"{cmd[0]}: timed out"


# ---------------------------------------------------------------- status ---

@app.get("/api/status")
def status():
    rc, out, _ = run_cmd(["pacman", "-Q"])
    total = len(out.strip().splitlines()) if rc == 0 and out.strip() else 0

    rc2, out2, _ = run_cmd(["pacman", "-Qe"])
    explicit = len(out2.strip().splitlines()) if rc2 == 0 and out2.strip() else 0

    rc3, out3, _ = run_cmd(["pacman", "-Qdtq"])
    orphans = len([l for l in out3.strip().splitlines() if l]) if rc3 == 0 else 0

    return {
        "installed_total": total,
        "installed_explicit": explicit,
        "orphans": orphans,
    }


# --------------------------------------------------------------- updates ---

@app.get("/api/updates")
def updates():
    rc, out, err = run_cmd(["checkupdates"])
    if rc == 127:
        return JSONResponse(
            {"error": "checkupdates not found. Install it with: sudo pacman -S pacman-contrib"},
            status_code=500,
        )
    # checkupdates exits 2 with empty output when there's nothing to do
    if rc not in (0, 2):
        return JSONResponse({"error": err.strip() or "checkupdates failed"}, status_code=500)

    results = []
    for line in out.strip().splitlines():
        parts = line.split()
        if len(parts) >= 4:
            results.append({
                "name": parts[0],
                "old_version": parts[1],
                "new_version": parts[3],
                "source": "repo" # Indicate source
            })
    return {"updates": results}

# ---------------------------------------------------------- aur updates ---
@app.get("/api/aur_updates")
def aur_updates():
    # Use yay to check for AUR updates
    rc, out, err = run_cmd(["yay", "-Qua"])
    if rc == 127:
        return JSONResponse(
            {"error": "yay not found. Install it with: git clone https://aur.archlinux.org/yay.git && cd yay && makepkg -si"},
            status_code=500,
        )
    # yay exits with code 1 if there are no updates, but the output still lists them.
    # We'll treat rc 1 as success if output is present.
    if rc not in (0, 1):
        return JSONResponse({"error": err.strip() or "yay -Qua failed"}, status_code=500)

    results = []
    for line in out.strip().splitlines():
        # Example line: "package_name old_version -> new_version"
        # Or: "package_name old_version -> new_version [ignored on your request]"
        # We'll split on spaces and assume the first word is the name,
        # the third word (index 2) is the new version, and the second word (index 1) is the old.
        parts = line.split()
        if len(parts) >= 3:
            # Remove '[ignored...' part if present from the new version
            new_ver_part = parts[2]
            if new_ver_part.startswith("["):
                continue # Skip ignored packages for now
            
            # Check if the line matches the expected format (name old -> new)
            # This is a basic check, improve regex if needed
            if "->" in line:
                # Find the name, old_ver, new_ver more robustly
                name_match = re.match(r'^(\S+)\s+', line)
                if name_match:
                    name = name_match.group(1)
                    # Extract versions after the name
                    remaining = line[len(name):].strip()
                    # Match old_version -> new_version
                    ver_match = re.search(r'(\S+)\s+->\s+(\S+)', remaining)
                    if ver_match:
                        old_version = ver_match.group(1)
                        new_version = ver_match.group(2)
                        results.append({
                            "name": name,
                            "old_version": old_version,
                            "new_version": new_version,
                            "source": "aur" # Indicate source
                        })

    return {"updates": results}


# ---------------------------------------------------------------- search ---

_SEARCH_HEADER = re.compile(
    r"^(?P<repo>[\w.\-]+)/(?P<name>[\w.\-@+]+)\s+(?P<ver>\S+)(?P<flags>.*)$"
)

@app.get("/api/search")
def search(q: str = ""):
    q = q.strip()
    if not q:
        return {"results": []}
    rc, out, err = run_cmd(["pacman", "-Ss", q])
    if rc not in (0, 1):
        return JSONResponse({"error": err.strip() or "search failed"}, status_code=500)

    results = []
    lines = out.splitlines()
    i = 0
    while i < len(lines):
        m = _SEARCH_HEADER.match(lines[i])
        if m:
            desc = ""
            if i + 1 < len(lines) and lines[i + 1].startswith((" ", "\t")):
                desc = lines[i + 1].strip()
                i += 1
            results.append({
                "name": m.group("name"),
                "repo": m.group("repo"),
                "version": m.group("ver"),
                "description": desc,
                "installed": "[installed" in m.group("flags"),
                "source": "repo" # Indicate source
            })
        i += 1
    return {"results": results[:80]}

# ---------------------------------------------------------- aur search ---
@app.get("/api/aur_search")
def aur_search(q: str = ""):
    q = q.strip()
    if not q:
        return {"results": []}
    # Use yay for AUR search
    rc, out, err = run_cmd(["yay", "-Ss", q])
    if rc not in (0, 1):
        return JSONResponse({"error": err.strip() or "AUR search failed"}, status_code=500)

    results = []
    lines = out.splitlines()
    i = 0
    # The format for yay -Ss might differ slightly, adjust regex if needed
    # Typical yay output: aur/package_name N.N.N Description
    _AUR_SEARCH_HEADER = re.compile(r"^(aur|/community)/(\S+)\s+(\S+)\s+(.*)$")
    while i < len(lines):
        m = _AUR_SEARCH_HEADER.match(lines[i])
        if m:
            source = m.group(1) # aur or community
            name = m.group(2)
            version = m.group(3)
            desc = m.group(4)
            
            # Check if installed - yay might mark this differently
            # For now, assume not installed unless explicitly marked
            is_installed = False
            # A simple check could be to run pacman -Q on the name
            # Or rely on yay's output flags if available
            # Let's assume for now it's not installed and check separately if needed
            results.append({
                "name": name,
                "repo": source, # Use 'aur' or 'community'
                "version": version,
                "description": desc,
                "installed": is_installed,
                "source": "aur" # Indicate source
            })
        i += 1
    return {"results": results[:80]}


# -------------------------------------------------------------- installed --

@app.get("/api/installed")
def installed():
    rc, out, err = run_cmd(["pacman", "-Qe"])
    explicit_names = {l.split()[0] for l in out.strip().splitlines() if l}

    rc2, out2, err2 = run_cmd(["pacman", "-Q"])
    if rc2 != 0:
        return JSONResponse({"error": err2.strip() or "pacman -Q failed"}, status_code=500)

    results = []
    for line in out2.strip().splitlines():
        parts = line.split()
        if len(parts) >= 2:
            name, ver = parts[0], parts[1]
            results.append({
                "name": name,
                "version": ver,
                "explicit": name in explicit_names,
            })
    return {"results": results}


# ---------------------------------------------------------- package info --

def _parse_info_block(text: str) -> dict[str, str]:
    """
    Parses the "Field   : value" block `pacman -Qi`/`-Si`/`yay -Si` print.
    Continuation lines (wrapped values, additional Optional Deps entries)
    are indented with no field name, so they're distinguished from a new
    field purely by not starting at column 0.
    """
    fields: dict[str, str] = {}
    key = None
    for line in text.splitlines():
        if not line.strip():
            continue
        if line[:1] != " " and " : " in line:
            key, _, value = line.partition(" : ")
            key = key.strip()
            fields[key] = value.strip()
        elif key:
            fields[key] = fields[key] + "\n" + line.strip()
    return fields


@app.get("/api/package_info")
def package_info(name: str = ""):
    name = name.strip()
    if not name:
        return JSONResponse({"error": "name required"}, status_code=400)

    # Installed metadata is preferred: it's the only source with accurate
    # "Required By" (reverse dependency) data, and it works identically for
    # repo and AUR packages alike once installed.
    rc, out, err = run_cmd(["pacman", "-Qi", name])
    origin = "installed"
    if rc != 0:
        rc, out, err = run_cmd(["pacman", "-Si", name])
        origin = "repo"
    if rc != 0 and shutil.which("yay"):
        rc, out, err = run_cmd(["yay", "-Si", name])
        origin = "aur"
    if rc != 0:
        return JSONResponse(
            {"error": err.strip() or f"no info found for {name}"}, status_code=404
        )

    fields = _parse_info_block(out)

    def get(key: str) -> str | None:
        value = fields.get(key, "").strip()
        return None if value in ("", "None") else value

    def get_list(key: str) -> list[str]:
        value = get(key)
        return value.split() if value else []

    optional_deps = get("Optional Deps")

    return {
        "name": name,
        "origin": origin,
        "version": get("Version"),
        "description": get("Description"),
        "url": get("URL"),
        "licenses": get_list("Licenses"),
        "depends": get_list("Depends On"),
        "optional_deps": optional_deps.splitlines() if optional_deps else [],
        "required_by": get_list("Required By"),
        "provides": get_list("Provides"),
        "installed_size": get("Installed Size"),
        "download_size": get("Download Size"),
    }


# ---------------------------------------------------------------- history --

_LOG_LINE = re.compile(
    r"^\[([^\]]+)\] \[ALPM\] (installed|upgraded|removed|reinstalled) (\S+) \(([^)]+)\)"
)

@app.get("/api/history")
def history():
    log_path = Path("/var/log/pacman.log")
    if not log_path.exists():
        return {"entries": []}

    lines = log_path.read_text(errors="ignore").splitlines()[-6000:]
    entries = []
    for line in reversed(lines):
        m = _LOG_LINE.match(line)
        if m:
            ts, action, name, ver = m.groups()
            entries.append({"date": ts, "action": action, "name": name, "version": ver})
        if len(entries) >= 150:
            break
    return {"entries": entries}


@app.post("/api/exit")
async def exit_app():
    """
    Shuts the whole server process down — not a pacman action, just Anvil
    itself exiting. Refuses while any /ws/* transaction is still running so
    a real pacman/yay process is never orphaned mid-install; otherwise
    signals its own process with SIGTERM, which uvicorn's default signal
    handling turns into a clean shutdown (closes connections, then exits).
    """
    if _active_transactions > 0:
        return JSONResponse(
            {"ok": False, "error": "A transaction is still running — wait for it to finish first."},
            status_code=409,
        )

    async def _shutdown():
        await asyncio.sleep(0.3)  # let the response for this request flush first
        os.kill(os.getpid(), signal.SIGTERM)

    asyncio.create_task(_shutdown())
    return {"ok": True}


# ------------------------------------------------------- privileged actions

# Any output pacman/yay leaves sitting without a trailing newline is
# *possibly* a `fputs(...); read stdin` prompt waiting on a pipe — but
# pacman/yay can just as easily go quiet for a while while resolving
# dependencies or verifying signatures, and that thinking time can easily
# exceed this timeout. Requiring the leftover text to also end in a shape
# real prompts use ("[Y/n]", "[y/N]", yay's "==>" menu marker, or a
# trailing ":") avoids misreading that as an unanswered prompt — which
# previously could queue a stray reply into stdin that then got silently
# consumed by the next *real* prompt instead of the one the user answered.
_PROMPT_IDLE_SECONDS = 0.4
_PROMPT_SHAPE_RE = re.compile(r"(\[(?:Y/n|y/N)\]|==>|:)\s*$")

# yay shells out to plain `sudo` (not pkexec) for the final install step of
# an AUR build. sudo's password prompt is written directly to /dev/tty,
# bypassing stdout/stderr entirely — our output capture below can never see
# it, so left alone it just blocks silently on whatever terminal launched
# this server. start_new_session=True detaches the child from that
# controlling terminal so sudo can't reach it. Rather than pointing
# SUDO_ASKPASS at a system askpass dialog (ssh-askpass's UI hasn't changed
# since the 90s), Anvil supplies its own tiny askpass script: sudo invokes
# it, it connects back to a Unix socket this process opens just for the
# run, we relay the password prompt to the browser over the same WebSocket
# used for pacman's Yes/No prompts, and the typed password is written back
# down that socket for the script to hand to sudo. The password only ever
# exists in this process's memory and the browser tab — never on disk,
# never in argv/env of any process ps could see.
_ASKPASS_SCRIPT = """#!/usr/bin/env python3
import os, socket, sys

def main():
    sock_path = os.environ.get("ANVIL_ASKPASS_SOCK")
    if not sock_path:
        sys.exit(1)
    prompt = sys.argv[1] if len(sys.argv) > 1 else "Password:"
    try:
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(5)
        s.connect(sock_path)
        s.settimeout(None)
        s.sendall(prompt.encode() + b"\\n")
        buf = b""
        while not buf.endswith(b"\\n"):
            chunk = s.recv(4096)
            if not chunk:
                break
            buf += chunk
        sys.stdout.write(buf.decode(errors="ignore").rstrip("\\n"))
        sys.stdout.flush()
    except OSError:
        sys.exit(1)

if __name__ == "__main__":
    main()
"""


async def stream_process(ws: WebSocket, cmd: list[str]):
    global _active_transactions
    _active_transactions += 1
    await ws.send_json({"type": "start", "cmd": " ".join(cmd)})

    # A single dedicated reader serializes every inbound WebSocket message
    # into one queue, so the two things that can ask the browser a question
    # here — a pacman/yay text prompt, and a sudo password request relayed
    # via the askpass socket below — never race each other calling
    # ws.receive_json() concurrently on the same connection.
    answer_queue: asyncio.Queue = asyncio.Queue()

    async def ws_reader():
        try:
            while True:
                answer_queue.put_nowait(await ws.receive_json())
        except WebSocketDisconnect:
            answer_queue.put_nowait(None)

    reader_task = asyncio.create_task(ws_reader())

    async def get_answer() -> dict:
        msg = await answer_queue.get()
        if msg is None:
            raise WebSocketDisconnect()
        return msg

    askpass_dir: str | None = None
    askpass_server = None
    try:
        env = os.environ.copy()

        # Only yay's own internal `sudo` call needs an askpass helper —
        # pkexec-based commands never consult SUDO_ASKPASS.
        if cmd and cmd[0] == "yay":
            askpass_dir = tempfile.mkdtemp(prefix="anvil-askpass-")
            os.chmod(askpass_dir, 0o700)
            script_path = os.path.join(askpass_dir, "askpass.py")
            with open(script_path, "w") as f:
                f.write(_ASKPASS_SCRIPT)
            os.chmod(script_path, 0o700)
            sock_path = os.path.join(askpass_dir, "askpass.sock")

            async def handle_askpass_conn(reader, writer):
                try:
                    prompt_line = await reader.readline()
                    prompt = prompt_line.decode(errors="ignore").strip() or "Password required for sudo"
                    await ws.send_json({"type": "password_prompt", "text": prompt})
                    reply = await get_answer()
                    password = reply.get("answer") or ""
                    writer.write(password.encode() + b"\n")
                    await writer.drain()
                finally:
                    writer.close()

            askpass_server = await asyncio.start_unix_server(handle_askpass_conn, path=sock_path)
            env["SUDO_ASKPASS"] = script_path
            env["ANVIL_ASKPASS_SOCK"] = sock_path

        proc = await asyncio.create_subprocess_exec(
            *cmd,
            env=env,
            start_new_session=True,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )

        # Regex patterns for parsing progress
        # Download progress: [####################] 100%
        download_progress_pattern = re.compile(r'\[(#+|\.+)\]\s*(\d+%)')
        # Installation progress: (1/3) package_name: installing package_version...
        # Or general progress: processing package_name...
        # Or specific package actions: checking keys...
        # We'll focus on the "(X/Y)" pattern and "installing" for now
        install_progress_pattern = re.compile(r'\((\d+)/(\d+)\)\s*(.+?):\s*(installing|checking|downloading)')
        # Generic package processing: Processing package_name...
        generic_package_pattern = re.compile(r'(processing|checking keys|loading packages|resolving dependencies|looking for conflicting packages|installing|checking|arming|upgrading|removing|reinstalling)\s+(.+?)\.{3}')

        current_download_pkg = None
        current_install_pkg = None
        total_to_install = 0

        async def handle_line(line: str):
            nonlocal current_download_pkg, current_install_pkg, total_to_install

            # Attempt to parse download progress
            dl_match = download_progress_pattern.search(line)
            inst_match = install_progress_pattern.search(line)
            gen_match = generic_package_pattern.search(line.lower())

            # Send a progress update message if we detect a change
            progress_update = {"type": "progress"}

            if dl_match:
                progress_update["download"] = {"current": dl_match.group(1), "percent": dl_match.group(2)}
                if gen_match and gen_match.group(2):  # Capture the package name if available
                     current_download_pkg = gen_match.group(2)
                     progress_update["download"]["package"] = current_download_pkg

            elif inst_match:
                num_current = int(inst_match.group(1))
                num_total = int(inst_match.group(2))
                pkg_name = inst_match.group(3)
                action_type = inst_match.group(4)

                # Update total count if not already set and this is a start
                if total_to_install == 0 and num_total > 0:
                    total_to_install = num_total

                current_install_pkg = pkg_name
                progress_update["install"] = {
                    "current_num": num_current,
                    "total_num": total_to_install,
                    "package": pkg_name,
                    "action": action_type
                }

            elif gen_match:
                 # Generic package action, useful for showing what's happening
                 action_type = gen_match.group(1)
                 pkg_name = gen_match.group(2)
                 if action_type in ["installing", "upgrading", "removing", "reinstalling"]:
                      # Assume this is the next package in sequence if we don't have specific (X/Y) info yet
                      if not current_install_pkg or current_install_pkg != pkg_name:
                          current_install_pkg = pkg_name
                          # Estimate progress if we don't have exact numbers
                          if total_to_install == 0:
                              progress_update["install"] = {
                                  "current_num": "?",
                                  "total_num": "?",
                                  "package": pkg_name,
                                  "action": action_type,
                                  "estimated": True
                              }
                          else:
                              # Try to estimate current number based on the package name order
                              # This is a simplification, (X/Y) from logs is better
                              progress_update["install"] = {
                                  "current_num": "estimating...",
                                  "total_num": total_to_install,
                                  "package": pkg_name,
                                  "action": action_type,
                                  "estimated": True
                              }

            # If we captured any progress info, send the update
            if len(progress_update) > 1:  # More than just the type
                 await ws.send_json(progress_update)

            # Always send the raw line as well for full output
            await ws.send_json({"type": "line", "text": line})

        buffer = b""
        while True:
            try:
                chunk = await asyncio.wait_for(proc.stdout.read(4096), timeout=_PROMPT_IDLE_SECONDS)
            except asyncio.TimeoutError:
                # Nothing arrived in time. That alone doesn't mean pacman/yay
                # is blocked on a read() of its own waiting for us to answer
                # — it may just still be computing (resolving dependencies,
                # verifying signatures). Only treat it as a real prompt if
                # the leftover text also has the shape of one; otherwise
                # keep waiting instead of misreading thinking time as an
                # unanswered question.
                if not buffer or not _PROMPT_SHAPE_RE.search(
                    buffer.decode(errors="ignore").rstrip()
                ):
                    continue
                prompt_text = buffer.decode(errors="ignore")
                buffer = b""
                await ws.send_json({"type": "prompt", "text": prompt_text})
                try:
                    reply = await get_answer()
                except WebSocketDisconnect:
                    proc.kill()
                    await proc.wait()
                    return
                answer = (reply.get("answer") or "").strip()
                proc.stdin.write((answer + "\n").encode())
                await proc.stdin.drain()
                await handle_line(prompt_text + answer)
                continue

            if not chunk:
                break
            buffer += chunk
            while b"\n" in buffer:
                line_bytes, buffer = buffer.split(b"\n", 1)
                await handle_line(line_bytes.decode(errors="ignore").rstrip("\r"))

        if buffer:
            await handle_line(buffer.decode(errors="ignore").rstrip("\r"))

        rc = await proc.wait()
        await ws.send_json({"type": "done", "returncode": rc})
    except FileNotFoundError:
        await ws.send_json({"type": "line", "text": f"error: command not found: {cmd[0]}"})
        await ws.send_json({"type": "done", "returncode": 127})
    except Exception as exc:  # noqa: BLE001 — surface any failure to the UI
        await ws.send_json({"type": "line", "text": f"error: {exc}"})
        await ws.send_json({"type": "done", "returncode": 1})
    finally:
        _active_transactions -= 1
        reader_task.cancel()
        if askpass_server is not None:
            askpass_server.close()
        if askpass_dir is not None:
            shutil.rmtree(askpass_dir, ignore_errors=True)


@app.websocket("/ws/sync")
async def ws_sync(ws: WebSocket):
    await ws.accept()
    await stream_process(ws, ["pkexec", "pacman", "-Sy"])
    await ws.close()


@app.websocket("/ws/refresh_keyrings")
async def ws_refresh_keyrings(ws: WebSocket):
    """
    A plain -Syu only picks up new trusted keys as a side effect of
    archlinux-keyring itself having a pending version. This pulls current
    key data from the keyservers directly, then re-syncs archlinux-keyring
    so its hook re-populates the local keyring — the fix for a keyring
    stale enough to fail signature checks outright.
    """
    await ws.accept()
    await stream_process(ws, ["pkexec", "pacman-key", "--refresh-keys"])
    await stream_process(ws, ["pkexec", "pacman", "-Sy", "archlinux-keyring"])
    await ws.close()


@app.websocket("/ws/apply")
async def ws_apply(ws: WebSocket):
    """
    Arch does not support safely upgrading a subset of packages while
    leaving the rest of the sync database ahead of them (a "partial
    upgrade"), so ticking any update runs a full `pacman -Syu`. Selected
    new packages are then installed as a separate, explicit transaction.

    Neither command passes --noconfirm: pacman's own prompts (dependency
    review, conflict resolution) surface as {"type": "prompt"} messages over
    this socket instead of being silently defaulted, so a conflict like
    "X and Y are in conflict, remove Y?" is something you can actually
    answer instead of a hard failure.
    """
    await ws.accept()
    try:
        msg = await ws.receive_json()
    except WebSocketDisconnect:
        return

    if msg.get("upgrade_all"):
        await stream_process(ws, ["pkexec", "pacman", "-Syu"])

    install_pkgs = [p for p in msg.get("install", []) if p]
    if install_pkgs:
        await stream_process(ws, ["pkexec", "pacman", "-S", *install_pkgs])

    await ws.close()


@app.websocket("/ws/remove")
async def ws_remove(ws: WebSocket):
    await ws.accept()
    try:
        msg = await ws.receive_json()
    except WebSocketDisconnect:
        return

    pkgs = [p for p in msg.get("packages", []) if p]
    if pkgs:
        await stream_process(ws, ["pkexec", "pacman", "-R", *pkgs])

    await ws.close()


@app.websocket("/ws/remove_orphans")
async def ws_remove_orphans(ws: WebSocket):
    """
    Removes every "orphan" (a dependency-installed package nothing else
    requires anymore, per `pacman -Qtdq`) in one shot via -Rns: -n also
    drops now-unneeded config files, -s recurses so removing one orphan
    that itself frees up further orphans doesn't need a second run.
    """
    await ws.accept()
    rc, out, _ = run_cmd(["pacman", "-Qtdq"])
    orphans = [l.strip() for l in out.strip().splitlines() if l.strip()] if rc == 0 else []
    if orphans:
        await stream_process(ws, ["pkexec", "pacman", "-Rns", *orphans])
    else:
        await ws.send_json({"type": "start", "cmd": "pacman -Rns (no orphans)"})
        await ws.send_json({"type": "line", "text": "No orphaned packages to remove."})
        await ws.send_json({"type": "done", "returncode": 0})
    await ws.close()

# ----------------------------------------------- aur privileged actions
@app.websocket("/ws/aur_install")
async def ws_aur_install(ws: WebSocket):
    """
    Install/upgrade AUR packages using yay.
    """
    await ws.accept()
    try:
        msg = await ws.receive_json()
    except WebSocketDisconnect:
        return

    if msg.get("upgrade_all"):
        await stream_process(ws, ["yay", "-Sua"])

    pkgs = [p for p in msg.get("packages", []) if p]
    if pkgs:
        await stream_process(ws, ["yay", "-S", *pkgs])

    await ws.close()

@app.websocket("/ws/aur_remove")
async def ws_aur_remove(ws: WebSocket):
    """
    Remove AUR packages using yay.
    """
    await ws.accept()
    try:
        msg = await ws.receive_json()
    except WebSocketDisconnect:
        return

    pkgs = [p for p in msg.get("packages", []) if p]
    if pkgs:
        # Use yay for AUR removal
        await stream_process(ws, ["yay", "-R", *pkgs])

    await ws.close()


@app.websocket("/ws/clean_cache")
async def ws_clean_cache(ws: WebSocket):
    """
    Trims /var/cache/pacman/pkg via pacman-contrib's paccache, keeping only
    the single most recent cached version of each package (uninstalled
    packages' cached files are dropped entirely) — the repo-package
    equivalent of pamac's "clear old build files" option.
    """
    await ws.accept()
    await stream_process(ws, ["pkexec", "paccache", "-rk1"])
    await ws.close()


@app.websocket("/ws/clean_aur_cache")
async def ws_clean_aur_cache(ws: WebSocket):
    """
    Clears yay's AUR build cache (~/.cache/yay), the source of the stale
    "reinstalling instead of upgrading" bug a leftover clone can cause.
    """
    await ws.accept()
    await stream_process(ws, ["yay", "-Sc"])
    await ws.close()


# Static frontend last, so it never shadows the /api and /ws routes above.
app.mount("/", StaticFiles(directory=str(FRONTEND_DIR), html=True), name="frontend")