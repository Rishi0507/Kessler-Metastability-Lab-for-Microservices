#!/usr/bin/env python3

from __future__ import annotations

import argparse
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import time
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TOOLS = ROOT / ".tools"
DATA = ROOT / ".devdata"
LOGS = DATA / "logs"
STATE = DATA / "devstack.json"
WIN = os.name == "nt"

PG_PORT, PG_REPLICA_PORT, REDIS_PORT = 5433, 5434, 6380
LB_PORT, EDGE_PORTS = 8080, (8081, 8082)
DB_USER = DB_PASS = DB_NAME = "kessler"


def _find(name: str, *candidates: Path) -> str:
    for c in candidates:
        if c.is_file():
            return str(c)
    found = shutil.which(name)
    if not found:
        sys.exit(f"devstack: cannot find '{name}' (looked in .tools and PATH)")
    return found


def pg(tool: str) -> str:
    exe = f"{tool}.exe" if WIN else tool
    return _find(tool, TOOLS / "postgres" / "pgsql" / "bin" / exe)


def redis_server() -> str:
    return _find("redis-server", *(TOOLS / "redis").glob("*/redis-server.exe"))


def nginx() -> str:
    return _find("nginx", *(TOOLS / "nginx").glob("*/nginx.exe"))


def python() -> str:
    return str(ROOT / ".venv" / ("Scripts/python.exe" if WIN else "bin/python"))


def log(msg: str) -> None:
    print(f"[devstack] {msg}", flush=True)


def port_open(port: int, host: str = "127.0.0.1") -> bool:
    with socket.socket() as s:
        s.settimeout(0.3)
        return s.connect_ex((host, port)) == 0


def http_ok(url: str, timeout: float = 1.0) -> bool:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:  # noqa: S310
            return 200 <= r.status < 300
    except Exception:
        return False


def wait_for(check, what: str, timeout: float = 60.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if check():
            return
        time.sleep(0.3)
    sys.exit(f"devstack: timed out waiting for {what} (see .devdata/logs)")


def run(cmd: list[str], **kw) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, check=True, **kw)


def load_state() -> dict[str, int]:
    try:
        return json.loads(STATE.read_text())
    except (OSError, ValueError):
        return {}


def save_state(state: dict[str, int]) -> None:
    STATE.write_text(json.dumps(state, indent=2))


SPAWN_FLAGS = (subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP) if WIN else 0


def alive(pid: int) -> bool:
    if WIN:
        out = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/NH"], capture_output=True, text=True).stdout
        return str(pid) in out
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def pg_setup() -> None:
    primary, replica = DATA / "pg" / "primary", DATA / "pg" / "replica"
    if not (primary / "PG_VERSION").exists():
        log("initialising Postgres primary")
        primary.mkdir(parents=True, exist_ok=True)
        run(
            [
                pg("initdb"),
                "-D",
                str(primary),
                "-U",
                "postgres",
                "--auth-local=trust",
                "--auth-host=trust",
                "-E",
                "UTF8",
                "--locale=C",
            ],
            stdout=subprocess.DEVNULL,
        )
        with open(primary / "postgresql.conf", "a", encoding="utf-8") as f:
            f.write(
                f"\n# --- kessler devstack ---\nlisten_addresses = '127.0.0.1'\nport = {PG_PORT}\n"
                "max_connections = 200\nshared_buffers = 128MB\nwal_level = replica\nmax_wal_senders = 8\n"
                "wal_keep_size = 256MB\nhot_standby = on\nlog_min_duration_statement = 250\n"
            )
        (primary / "pg_hba.conf").write_text(
            "local all all trust\n"
            "host replication replicator 127.0.0.1/32 trust\n"
            "host all postgres 127.0.0.1/32 trust\n"
            "host all all 127.0.0.1/32 scram-sha-256\n"
        )
    if not port_open(PG_PORT):
        log("starting Postgres primary")
        run(
            [pg("pg_ctl"), "-D", str(primary), "-l", str(LOGS / "pg-primary.log"), "-w", "start"],
            stdout=subprocess.DEVNULL,
        )
    bootstrap = (
        "import asyncio, asyncpg\n"
        "async def m():\n"
        f"    c = await asyncpg.connect(host='127.0.0.1', port={PG_PORT}, user='postgres', database='postgres')\n"
        f"    if not await c.fetchval(\"SELECT 1 FROM pg_roles WHERE rolname='{DB_USER}'\"):\n"
        f"        await c.execute(\"CREATE ROLE {DB_USER} LOGIN PASSWORD '{DB_PASS}'\")\n"
        "    if not await c.fetchval(\"SELECT 1 FROM pg_roles WHERE rolname='replicator'\"):\n"
        "        await c.execute('CREATE ROLE replicator REPLICATION LOGIN')\n"
        f"    if not await c.fetchval(\"SELECT 1 FROM pg_database WHERE datname='{DB_NAME}'\"):\n"
        f"        await c.execute('CREATE DATABASE {DB_NAME} OWNER {DB_USER}')\n"
        "    await c.close()\n"
        "asyncio.run(m())\n"
    )
    run([python(), "-c", bootstrap])
    if not (replica / "PG_VERSION").exists():
        log("cloning streaming replica (pg_basebackup)")
        run(
            [
                pg("pg_basebackup"),
                "-h",
                "127.0.0.1",
                "-p",
                str(PG_PORT),
                "-U",
                "replicator",
                "-D",
                str(replica),
                "-R",
                "-X",
                "stream",
                "-c",
                "fast",
            ],
            stdout=subprocess.DEVNULL,
        )
        with open(replica / "postgresql.auto.conf", "a", encoding="utf-8") as f:
            f.write(f"port = {PG_REPLICA_PORT}\n")
    if not port_open(PG_REPLICA_PORT):
        log("starting Postgres replica")
        run(
            [pg("pg_ctl"), "-D", str(replica), "-l", str(LOGS / "pg-replica.log"), "-w", "start"],
            stdout=subprocess.DEVNULL,
        )


def pg_stop() -> None:
    for name in ("replica", "primary"):
        d = DATA / "pg" / name
        if (d / "postmaster.pid").exists():
            log(f"stopping Postgres {name}")
            subprocess.run([pg("pg_ctl"), "-D", str(d), "-m", "fast", "-w", "stop"], stdout=subprocess.DEVNULL)


def nginx_prefix(name: str, web_root: Path, listen: int, docker: bool = False) -> Path:
    prefix = DATA / "nginx" / name
    conf = prefix / "conf"
    for sub in ("logs", "temp", "cache", "conf/snippets"):
        (prefix / sub).mkdir(parents=True, exist_ok=True)
    src = ROOT / "deploy" / "nginx"
    for f in src.glob("*.conf"):
        shutil.copy(f, conf / f.name)
    for f in (src / "snippets").glob("*.conf"):
        shutil.copy(f, conf / "snippets" / f.name)
    mime = Path(nginx()).parent / "conf" / "mime.types"
    shutil.copy(mime if mime.exists() else src / "mime.types", conf / "mime.types")
    kind = "docker" if docker else "local"
    shutil.copy(src / f"upstreams.{kind}.conf", conf / "upstreams.conf")
    shutil.copy(src / f"l4-upstreams.{kind}.conf", conf / "l4-upstreams.conf")
    (conf / "edge-listen.conf").write_text(f"listen {listen} proxy_protocol;\n")
    (conf / "l4-listen.conf").write_text(f"listen {listen};\n")
    (conf / "web-root.conf").write_text(f'root "{web_root.as_posix()}";\n')
    return prefix


def nginx_start(name: str, conf_file: str, listen: int, web_root: Path) -> None:
    prefix = nginx_prefix(name, web_root, listen)
    args = [nginx(), "-p", str(prefix) + os.sep, "-c", f"conf/{conf_file}"]
    test = subprocess.run([*args, "-t"], capture_output=True, text=True)
    if test.returncode != 0:
        sys.exit(f"devstack: nginx config test failed for {name}:\n{test.stderr}")
    log(f"starting nginx {name} on :{listen}")
    subprocess.Popen(args, cwd=prefix, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=SPAWN_FLAGS)
    wait_for(lambda: port_open(listen), f"nginx {name}")


def nginx_stop(name: str) -> None:
    prefix = DATA / "nginx" / name
    if (prefix / "logs" / "nginx.pid").exists():
        conf = "l4.conf" if name == "lb" else "edge.conf"
        log(f"stopping nginx {name}")
        subprocess.run(
            [nginx(), "-p", str(prefix) + os.sep, "-c", f"conf/{conf}", "-s", "quit"], cwd=prefix, capture_output=True
        )


@dataclass
class Service:
    name: str
    module: str
    args: list[str] = field(default_factory=list)
    ready: str | None = None
    env: dict[str, str] = field(default_factory=dict)


def app_env(name: str, replica: bool) -> dict[str, str]:
    env = dict(os.environ)
    env.update(
        {
            "PYTHONUTF8": "1",
            "PYTHONPATH": str(ROOT / "backend"),
            "KESSLER_ENV": "dev",
            "KESSLER_INSTANCE": name,
            "KESSLER_PUBLIC_URL": f"http://localhost:{LB_PORT}",
            "KESSLER_DATABASE_URL": f"postgresql://{DB_USER}:{DB_PASS}@127.0.0.1:{PG_PORT}/{DB_NAME}",
            "KESSLER_REDIS_URL": f"redis://127.0.0.1:{REDIS_PORT}/0",
        }
    )
    if replica:
        env["KESSLER_DATABASE_REPLICA_URL"] = f"postgresql://{DB_USER}:{DB_PASS}@127.0.0.1:{PG_REPLICA_PORT}/{DB_NAME}"
    return env


def start_service(svc: Service, replica: bool, state: dict[str, int]) -> None:
    if svc.name in state and alive(state[svc.name]):
        log(f"{svc.name} already running (pid {state[svc.name]})")
        return
    logf = open(LOGS / f"{svc.name}.log", "ab")  # noqa: SIM115
    env = app_env(svc.name, replica) | svc.env
    p = subprocess.Popen(
        [python(), "-m", svc.module, *svc.args],
        cwd=ROOT / "backend",
        env=env,
        stdout=logf,
        stderr=subprocess.STDOUT,
        creationflags=SPAWN_FLAGS,
    )
    state[svc.name] = p.pid
    save_state(state)
    log(f"started {svc.name} (pid {p.pid})")


_CTRL_BREAK_HELPER = """
import ctypes, sys
k = ctypes.windll.kernel32
pid = int(sys.argv[1])
k.FreeConsole()
if not k.AttachConsole(pid):
    sys.exit(1)
ignore = ctypes.WINFUNCTYPE(ctypes.c_int, ctypes.c_uint)(lambda event: 1)
k.SetConsoleCtrlHandler(ignore, 1)
sys.exit(0 if k.GenerateConsoleCtrlEvent(1, pid) else 2)
"""


def request_stop(pid: int) -> bool:
    if not WIN:
        os.kill(pid, signal.SIGTERM)
        return True
    r = subprocess.run(
        [python(), "-I", "-c", _CTRL_BREAK_HELPER, str(pid)],
        capture_output=True,
        creationflags=subprocess.CREATE_NO_WINDOW,
    )
    return r.returncode == 0


def stop_pid(name: str, pid: int) -> None:
    if not alive(pid):
        return
    log(f"stopping {name} (pid {pid})")
    if not request_stop(pid):
        log(f"{name} could not be asked to drain; stopping it hard")
    deadline = time.time() + 15
    while time.time() < deadline and alive(pid):
        time.sleep(0.3)
    if alive(pid):
        subprocess.run(
            ["taskkill", "/F", "/T", "/PID", str(pid)] if WIN else ["kill", "-9", str(pid)], capture_output=True
        )


def services(args: argparse.Namespace) -> list[Service]:
    out = [
        Service(f"api-{i + 1}", "kessler.api", ["--port", str(9001 + i)], f"http://127.0.0.1:{9001 + i}/readyz")
        for i in range(args.api)
    ]
    out += [
        Service(f"gateway-{i + 1}", "kessler.gateway", ["--port", str(9101 + i)], f"http://127.0.0.1:{9101 + i}/readyz")
        for i in range(args.gateways)
    ]
    out += [
        Service(
            f"scheduler-{i + 1}",
            "kessler.scheduler",
            ["--metrics-port", str(9201 + i)],
            f"http://127.0.0.1:{9201 + i}/metrics",
        )
        for i in range(2)
    ]
    out += [
        Service(
            f"worker-{i + 1}",
            "kessler.worker",
            ["--threads", str(args.threads), "--metrics-port", str(9301 + i)],
            f"http://127.0.0.1:{9301 + i}/metrics",
        )
        for i in range(args.workers)
    ]
    return out


def up(args: argparse.Namespace) -> None:
    LOGS.mkdir(parents=True, exist_ok=True)
    pg_setup()
    if not port_open(REDIS_PORT):
        log("starting Redis")
        logf = open(LOGS / "redis.log", "ab")  # noqa: SIM115
        p = subprocess.Popen(
            [
                redis_server(),
                "--port",
                str(REDIS_PORT),
                "--bind",
                "127.0.0.1",
                "--save",
                "",
                "--appendonly",
                "no",
                "--maxmemory",
                "512mb",
                "--maxmemory-policy",
                "noeviction",
            ],
            stdout=logf,
            stderr=subprocess.STDOUT,
            creationflags=SPAWN_FLAGS,
        )
        state = load_state()
        state["redis"] = p.pid
        save_state(state)
        wait_for(lambda: port_open(REDIS_PORT), "redis")
    log("applying migrations")
    run([python(), "-m", "kessler.migrate"], cwd=ROOT / "backend", env=app_env("migrate", False))
    state = load_state()
    svcs = services(args)
    for svc in svcs:
        start_service(svc, replica=not args.no_replica, state=state)
    for svc in svcs:
        if svc.ready:
            wait_for(lambda u=svc.ready: http_ok(u), svc.name, timeout=90)
    web_root = ROOT / "web" / "dist"
    web_root.mkdir(parents=True, exist_ok=True)
    if not (web_root / "index.html").exists():
        (web_root / "index.html").write_text(
            "<!doctype html><title>Kessler</title><p>Build the web app: npm run build</p>"
        )
    for i, port in enumerate(EDGE_PORTS):
        nginx_start(f"edge{i + 1}", "edge.conf", port, web_root)
    nginx_start("lb", "l4.conf", LB_PORT, web_root)
    wait_for(lambda: http_ok(f"http://127.0.0.1:{LB_PORT}/edge-health"), "load balancer")
    log(f"ready: http://localhost:{LB_PORT}  (api docs: http://localhost:{LB_PORT}/api/docs)")
    if args.detach:
        return
    log("attached: press Ctrl+C to stop the stack")
    reported: set[str] = set()
    try:
        while True:
            time.sleep(5)
            state = load_state()
            for name, pid in state.items():
                if not alive(pid) and name not in reported:
                    reported.add(name)
                    log(f"{name} (pid {pid}) exited; see .devdata/logs/{name}.log")
                elif alive(pid) and name in reported:
                    reported.discard(name)
                    log(f"{name} is running again (pid {pid})")
    except KeyboardInterrupt:
        log("stopping")
        down(args)


def down(_: argparse.Namespace) -> None:
    nginx_stop("lb")
    for i in range(len(EDGE_PORTS)):
        nginx_stop(f"edge{i + 1}")
    state = load_state()
    order = sorted(state, key=lambda n: (n.startswith("redis"), not n.startswith("api"), n))
    for name in order:
        if name != "redis":
            stop_pid(name, state[name])
    if "redis" in state:
        stop_pid("redis", state["redis"])
    pg_stop()
    STATE.unlink(missing_ok=True)
    log("stopped")


def restart(args: argparse.Namespace) -> None:
    picked = [s for s in services(args) if not args.name or args.name in (s.name, s.name.rsplit("-", 1)[0])]
    if not picked:
        sys.exit(f"no app process matches {args.name!r}")
    for svc in picked:
        state = load_state()
        if svc.name in state:
            stop_pid(svc.name, state[svc.name])
        start_service(svc, replica=not args.no_replica, state=state)
        if svc.ready:
            wait_for(lambda u=svc.ready: http_ok(u), svc.name, timeout=90)
    log(f"restarted {len(picked)} process(es)")


def status(args: argparse.Namespace) -> None:
    state = load_state()
    rows = [
        ("postgres-primary", port_open(PG_PORT)),
        ("postgres-replica", port_open(PG_REPLICA_PORT)),
        ("redis", port_open(REDIS_PORT)),
    ]
    for svc in services(args):
        rows.append(
            (svc.name, bool(svc.ready and http_ok(svc.ready)) or (svc.name in state and alive(state[svc.name])))
        )
    rows += [(f"edge{i + 1}", port_open(p)) for i, p in enumerate(EDGE_PORTS)]
    rows.append(("lb", http_ok(f"http://127.0.0.1:{LB_PORT}/edge-health")))
    for name, ok in rows:
        print(f"  {'UP  ' if ok else 'DOWN'}  {name}")


def logs(args: argparse.Namespace) -> None:
    path = LOGS / f"{args.name}.log"
    if not path.exists():
        sys.exit(f"no log for {args.name}")
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    print("\n".join(lines[-args.n :]))


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("command", choices=["up", "down", "status", "logs", "restart"])
    ap.add_argument("name", nargs="?", help="process (or group, for `restart`) name")
    ap.add_argument("-n", type=int, default=60, help="lines for `logs`")
    ap.add_argument("--api", type=int, default=3)
    ap.add_argument("--gateways", type=int, default=2)
    ap.add_argument("--workers", type=int, default=2)
    ap.add_argument("--threads", type=int, default=2, help="simulation slots per worker")
    ap.add_argument("--no-replica", action="store_true")
    ap.add_argument("-d", "--detach", action="store_true", help="start everything and return immediately")
    args = ap.parse_args()
    {"up": up, "down": down, "status": status, "logs": logs, "restart": restart}[args.command](args)


if __name__ == "__main__":
    main()
