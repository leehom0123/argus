#!/usr/bin/env python3
"""Paramiko-based deploy for Argus.

Targets servers reachable only via password auth (no SSH key, no sshpass
installed locally). Reads ``deploy/server.yaml`` for connection config in the
multi-server format (``activate:`` + ``servers:`` list, same shape DeepTS-Flow
uses), tar's the repo, scp's it across, untars on the remote, then runs
``docker compose up -d --build`` against ``deploy/docker-compose.yml``.

The pre-staged ``.env.production`` file on the remote (admin-managed, NEVER
pushed by this script) is copied into ``deploy/.env`` before bringing the
stack up.

Usage::

    python deploy/deploy.py                          # use activate: from yaml
    python deploy/deploy.py --server ali-web1        # override active server
    python deploy/deploy.py --clean                  # rm -rf remote first
    python deploy/deploy.py --no-build               # skip image rebuild
    python deploy/deploy.py --dry-run                # print plan, no I/O

Dependencies (install on the laptop running deploy.py)::

    pip install paramiko pyyaml

The companion ``deploy.sh`` is kept for SSH-key-auth targets; this script is
additive and only handles password-auth servers.
"""
from __future__ import annotations

import argparse
import os
import shlex
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# ── Repo layout ────────────────────────────────────────────────────────────
DEPLOY_DIR = Path(__file__).resolve().parent
REPO_ROOT = DEPLOY_DIR.parent
DEFAULT_CONFIG = DEPLOY_DIR / "server.yaml"
EXAMPLE_CONFIG = DEPLOY_DIR / "server.yaml.example"

ACTIVE_SERVER_ENV = "SERVER"

# Files we always include in the tarball (relative to REPO_ROOT). Missing
# entries are skipped with a warning so partial checkouts still work.
INCLUDE_PATHS: List[str] = [
    "backend",
    "frontend",
    "client",
    "deploy",
    "schemas",
    "docs",
    "README.md",
    "README.zh-CN.md",
    "mkdocs.yml",
    "requirements-docs.txt",
]

# Default exclude patterns (applied in addition to anything the yaml
# specifies under deploy.exclude_dirs). These are tar --exclude globs.
DEFAULT_EXCLUDES: List[str] = [
    "node_modules",
    "__pycache__",
    ".git",
    ".pytest_cache",
    "frontend/dist",
    "deploy/data",
    "deploy/.env",
    "deploy/.env.production",
    "deploy/server.yaml",
    "*.pyc",
    "*.pyo",
]


# ── Config loading ─────────────────────────────────────────────────────────
def _read_yaml(path: Path) -> dict:
    try:
        import yaml  # noqa: WPS433 (lazy import keeps --help fast)
    except ImportError:
        sys.exit(
            "[ERROR] PyYAML is required. Install with: pip install pyyaml"
        )
    if not path.exists():
        sys.exit(
            f"[ERROR] server config missing: {path}\n"
            f"Hint: cp {EXAMPLE_CONFIG.name} {path.name} and fill in the "
            f"connection details + password."
        )
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def _pick_server(raw: dict, override: Optional[str] = None) -> Dict[str, Any]:
    """Select active server from multi-server config; flatten."""
    servers = raw.get("servers")
    if not isinstance(servers, list) or not servers:
        sys.exit(
            "[ERROR] server.yaml must have a non-empty `servers:` list. "
            f"See {EXAMPLE_CONFIG.name} for the expected shape."
        )

    by_name: Dict[str, dict] = {}
    for i, srv in enumerate(servers):
        name = srv.get("name")
        if not name:
            sys.exit(f"[ERROR] servers[{i}] is missing the `name` field")
        if name in by_name:
            sys.exit(f"[ERROR] duplicate server name: {name}")
        by_name[name] = srv

    target = override or raw.get("activate")
    if not target:
        sys.exit(
            "[ERROR] no active server. Set top-level `activate:` in "
            "server.yaml or pass --server <name> / SERVER=<name> env.\n"
            f"Available: {list(by_name)}"
        )
    if target not in by_name:
        src = "--server / SERVER env" if override else "activate"
        sys.exit(
            f"[ERROR] {src}={target!r} not found. Available: {list(by_name)}"
        )

    active = by_name[target]
    return {
        "_active_server": target,
        "ssh": active.get("ssh") or {},
        "remote": active.get("remote") or {},
        "deploy_meta": active.get("deploy") or {},
        "deploy": raw.get("deploy") or {},
    }


def load_config(path: Optional[Path] = None,
                override: Optional[str] = None) -> Dict[str, Any]:
    p = path or DEFAULT_CONFIG
    raw = _read_yaml(p)
    src_override = override or os.environ.get(ACTIVE_SERVER_ENV) or None
    return _pick_server(raw, override=src_override)


# ── Tarball ────────────────────────────────────────────────────────────────
def build_tarball(cfg: Dict[str, Any], tarball_path: str,
                  *, dry_run: bool = False) -> Tuple[int, float]:
    """tar czf with exclude rules. Returns (size_bytes, elapsed_sec).

    On dry-run the command is printed but not executed; returns (0, 0.0).
    """
    extra_excludes = list((cfg.get("deploy") or {}).get("exclude_dirs") or [])
    excludes = list(DEFAULT_EXCLUDES) + [
        e for e in extra_excludes if e not in DEFAULT_EXCLUDES
    ]

    # Only include paths that exist (partial checkouts still tar successfully).
    includes: List[str] = []
    for rel in INCLUDE_PATHS:
        if (REPO_ROOT / rel).exists():
            includes.append(rel)
        else:
            print(f"[tar] skip (not present): {rel}")

    excl_args: List[str] = []
    for pat in excludes:
        excl_args.extend(["--exclude", pat])

    # --force-local: WSL/Windows paths with drive letters (`C:`) confuse tar
    # into thinking the archive is on a remote host.
    cmd = (
        ["tar", "--force-local"]
        + excl_args
        + ["-czf", tarball_path, "-C", str(REPO_ROOT)]
        + includes
    )

    print(f"[tar] {' '.join(shlex.quote(c) for c in cmd[:6])} ... "
          f"(+{len(excl_args)//2} excludes, {len(includes)} include paths)")

    if dry_run:
        return 0, 0.0

    t0 = time.time()
    rc = subprocess.call(cmd)
    if rc != 0:
        sys.exit(f"[ERROR] tar failed (rc={rc})")
    return os.path.getsize(tarball_path), time.time() - t0


# ── Remote connection (paramiko) ───────────────────────────────────────────
class _Remote:
    """Tiny paramiko wrapper. Real connect on first use; idempotent close."""

    def __init__(self, ssh_cfg: dict):
        self.host = ssh_cfg["host"]
        self.port = int(ssh_cfg.get("port", 22))
        self.user = ssh_cfg["user"]
        self.password = ssh_cfg.get("password")
        self.timeout = int(ssh_cfg.get("timeout", 20))
        self._client = None
        self._sftp = None

    def __enter__(self):
        self.connect()
        return self

    def __exit__(self, exc_type, exc, tb):
        self.close()

    def connect(self) -> None:
        if self._client is not None:
            return
        try:
            import paramiko  # noqa: WPS433
        except ImportError:
            sys.exit(
                "[ERROR] paramiko is required. Install with: "
                "pip install paramiko"
            )
        c = paramiko.SSHClient()
        c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        c.connect(
            self.host, port=self.port, username=self.user,
            password=self.password, timeout=self.timeout,
            look_for_keys=False, allow_agent=False,
        )
        self._client = c

    def close(self) -> None:
        if self._sftp is not None:
            try:
                self._sftp.close()
            except Exception:
                pass
            self._sftp = None
        if self._client is not None:
            try:
                self._client.close()
            except Exception:
                pass
            self._client = None

    def run(self, cmd: str, *, stream: bool = True,
            timeout: int = 600) -> Tuple[int, str]:
        self.connect()
        print(f"  $ {cmd}")
        stdin, stdout, stderr = self._client.exec_command(cmd, timeout=timeout)
        chunks: List[str] = []
        if stream:
            for line in iter(lambda: stdout.readline(2048), ""):
                print(f"  | {line}", end="")
                chunks.append(line)
        else:
            chunks.append(stdout.read().decode(errors="replace"))
        err = stderr.read().decode(errors="replace")
        if err:
            print(f"  ! {err}", end="")
        rc = stdout.channel.recv_exit_status()
        return rc, "".join(chunks) + err

    def run_ok(self, cmd: str, **kw) -> str:
        rc, out = self.run(cmd, **kw)
        if rc != 0:
            raise RuntimeError(f"remote cmd failed (rc={rc}): {cmd}")
        return out

    def put(self, local: str, remote: str) -> None:
        self.connect()
        if self._sftp is None:
            self._sftp = self._client.open_sftp()
        self._sftp.put(local, remote)


# ── Plan / execute ─────────────────────────────────────────────────────────
def _build_remote_commands(project_root: str, remote_tar: str,
                           env_file: str,
                           *, clean: bool, no_build: bool) -> List[str]:
    """The ordered list of remote shell commands."""
    build_flag = "" if no_build else " --build"
    cmds: List[str] = []
    if clean:
        # Keep deploy/data (the SQLite volume) when wiping; the user can pass
        # --clean to nuke unconditionally though, so be honest about it.
        cmds.append(f"rm -rf {shlex.quote(project_root)}")
    cmds.extend([
        f"mkdir -p {shlex.quote(project_root)}",
        f"tar -xzf {shlex.quote(remote_tar)} -C {shlex.quote(project_root)}",
        f"rm -f {shlex.quote(remote_tar)}",
        # Stage the env file from the admin-managed location. -f overwrite
        # ensures rotated secrets reach the container on next deploy.
        f"cp {shlex.quote(env_file)} "
        f"{shlex.quote(os.path.join(project_root, 'deploy', '.env'))}",
        f"cd {shlex.quote(project_root)}/deploy && docker compose pull || true",
        f"cd {shlex.quote(project_root)}/deploy && "
        f"docker compose up -d{build_flag}",
        "sleep 5",
        "curl -fsS http://127.0.0.1:8000/health",
    ])
    return cmds


def _print_plan(cfg: Dict[str, Any], remote_cmds: List[str],
                tarball: str) -> None:
    ssh = cfg["ssh"]
    rem = cfg["remote"]
    target = f"{ssh.get('user')}@{ssh.get('host')}:{ssh.get('port')}"
    print("─" * 64)
    print("PLAN (dry-run)")
    print("─" * 64)
    print(f"  active server:  {cfg['_active_server']}")
    print(f"  target:         {target}  (auth=password)")
    print(f"  remote root:    {rem.get('project_root')}")
    print(f"  local tarball:  {tarball}")
    print()
    print("  Remote commands (in order):")
    for i, cmd in enumerate(remote_cmds, 1):
        print(f"    {i:2d}. {cmd}")
    print("─" * 64)
    print("(no SSH/SCP performed — dry-run)")


def deploy(cfg: Dict[str, Any], *, clean: bool, no_build: bool,
           dry_run: bool) -> int:
    ssh = cfg["ssh"]
    rem = cfg["remote"]
    deploy_meta = cfg.get("deploy_meta") or {}

    auth = ssh.get("auth") or "password"
    if auth != "password":
        sys.exit(
            f"[ERROR] this script only supports auth=password. Got "
            f"auth={auth!r}. For SSH-key servers use deploy.sh instead."
        )
    for required in ("host", "port", "user", "password"):
        if not ssh.get(required):
            sys.exit(f"[ERROR] ssh.{required} missing in server.yaml")

    project_root = rem.get("project_root")
    if not project_root:
        sys.exit("[ERROR] remote.project_root missing in server.yaml")

    env_file = deploy_meta.get("env_file") or \
        os.path.join(project_root, "deploy", ".env.production")

    # Build the tarball locally (or skip on dry-run).
    fd, tarball = tempfile.mkstemp(suffix=".tar.gz", prefix="argus-deploy.")
    os.close(fd)
    try:
        size, dt = build_tarball(cfg, tarball, dry_run=dry_run)
        if not dry_run:
            print(f"[tar] {size/1e6:.2f} MB in {dt:.1f}s -> {tarball}")

        remote_tar = f"/tmp/argus-deploy.{os.getpid()}.tar.gz"
        remote_cmds = _build_remote_commands(
            project_root, remote_tar, env_file,
            clean=clean, no_build=no_build,
        )

        if dry_run:
            _print_plan(cfg, remote_cmds, tarball)
            return 0

        target = f"{ssh['user']}@{ssh['host']}:{ssh['port']}"
        print(f"[deploy] target={target} -> {project_root}")

        with _Remote(ssh) as conn:
            print(f"[scp] {tarball}  ->  {ssh['host']}:{remote_tar}")
            conn.put(tarball, remote_tar)

            for cmd in remote_cmds:
                rc, _ = conn.run(cmd, timeout=900)
                if rc != 0:
                    raise RuntimeError(
                        f"remote cmd failed (rc={rc}): {cmd}"
                    )

        print()
        print("[done] deploy complete")
        public = deploy_meta.get("public_url")
        if public:
            print(f"[hint] verify externally: curl -k {public}/health")
        return 0
    finally:
        if os.path.exists(tarball):
            try:
                os.unlink(tarball)
            except OSError:
                pass


# ── CLI ────────────────────────────────────────────────────────────────────
def _parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="deploy.py",
        description=(
            "Paramiko-based deploy for Argus (password-auth servers). "
            "See deploy/server.yaml.example for config shape."
        ),
    )
    p.add_argument(
        "--server", metavar="NAME",
        help="Override the active server (otherwise read from "
             "server.yaml `activate:` or SERVER env var).",
    )
    p.add_argument(
        "--clean", action="store_true",
        help="rm -rf the remote project_root before extracting (DESTRUCTIVE — "
             "wipes deploy/data SQLite volume too).",
    )
    p.add_argument(
        "--no-build", action="store_true",
        help="Skip --build on `docker compose up` (use cached image).",
    )
    p.add_argument(
        "--dry-run", action="store_true",
        help="Print the plan and exit without touching the network.",
    )
    p.add_argument(
        "--config", metavar="PATH", default=None,
        help=f"Path to server.yaml (default: {DEFAULT_CONFIG}).",
    )
    return p.parse_args(argv)


def main(argv: Optional[List[str]] = None) -> int:
    args = _parse_args(argv)
    cfg_path = Path(args.config) if args.config else None
    cfg = load_config(cfg_path, override=args.server)
    return deploy(
        cfg,
        clean=args.clean,
        no_build=args.no_build,
        dry_run=args.dry_run,
    )


if __name__ == "__main__":
    sys.exit(main())
