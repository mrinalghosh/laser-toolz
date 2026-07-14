#!/usr/bin/env python3
"""
toolz.py — one command to launch every laser-toolz web frontend.

The repo grew three sibling Flask UIs, each a self-contained single-user app on
its own port:

  linify   (server.py)          http://127.0.0.1:5001   image  -> hairline SVG
  segment  (segment_server.py)  http://127.0.0.1:5002   image  -> segmentation SVG
  epilogue (epilogue_server.py) http://127.0.0.1:5003   SVG    -> Epilog-safe SVG

Each hardcodes host=127.0.0.1 and app.run(), so they can't share one process —
this launcher runs each as its own subprocess (same interpreter), hands it its
port via the PORT env var it already honours, prefixes its logs, and shuts the
whole set down together on Ctrl-C.

  python toolz.py                     # launch all three
  python toolz.py segment epilogue    # just a subset (any order)
  python toolz.py linify=8080         # override a port
  python toolz.py --list              # show the tools and exit
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import threading
import time

ROOT = os.path.dirname(os.path.abspath(__file__))

# name -> (server script, default port, one-line blurb). Aliases below.
TOOLS = {
    "linify":   ("server.py",          5001, "image  -> hairline line-art SVG"),
    "segment":  ("segment_server.py",  5002, "image  -> segmentation-mask SVG"),
    "epilogue": ("epilogue_server.py", 5003, "SVG    -> Epilog-driver-safe SVG"),
}
ALIASES = {
    "laser": "linify", "laser-toolz": "linify", "lines": "linify",
    "seg": "segment", "sam": "segment",
    "epi": "epilogue", "epilog": "epilogue",
}

# Dim per-tool colours so interleaved logs stay readable (skipped when not a tty).
_COLORS = {"linify": "36", "segment": "35", "epilogue": "33"}


def _resolve(tokens):
    """Turn CLI tokens into an ordered, de-duped list of (name, port)."""
    if not tokens:
        tokens = list(TOOLS)
    chosen, seen = [], set()
    for tok in tokens:
        name, _, port_override = tok.partition("=")
        name = ALIASES.get(name.lower(), name.lower())
        if name not in TOOLS:
            raise SystemExit(f"error: unknown tool {tok!r}  (try --list)")
        if port_override:
            try:
                port = int(port_override)
            except ValueError:
                raise SystemExit(f"error: bad port in {tok!r}")
        else:
            port = TOOLS[name][1]
        if name in seen:
            continue
        seen.add(name)
        chosen.append((name, port))
    return chosen


def _pipe_logs(name, proc, use_color):
    """Stream one child's merged output, each line tagged with the tool name."""
    tag = f"[{name}]"
    if use_color:
        tag = f"\033[{_COLORS.get(name, '37')}m{tag}\033[0m"
    for line in proc.stdout:
        sys.stdout.write(f"{tag} {line}")
        sys.stdout.flush()


def _shutdown(procs):
    for name, _, proc in procs:
        if proc.poll() is None:
            proc.terminate()
    deadline = time.monotonic() + 5.0
    for name, _, proc in procs:
        remaining = max(0.0, deadline - time.monotonic())
        try:
            proc.wait(timeout=remaining)
        except subprocess.TimeoutExpired:
            proc.kill()


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if "--list" in argv or "-l" in argv:
        print("laser-toolz frontends:")
        for name, (script, port, blurb) in TOOLS.items():
            print(f"  {name:<9} :{port}  {script:<20} {blurb}")
        return 0
    if "-h" in argv or "--help" in argv:
        print(__doc__.strip())
        return 0

    selected = _resolve(argv)
    use_color = sys.stdout.isatty()

    # Peer URLs for the in-page switch nav: every tool at its default port,
    # overridden by whatever was chosen here, handed to each child so its nav
    # links point at the real ports (see toolz_nav.py).
    peer_ports = {name: TOOLS[name][1] for name in TOOLS}
    peer_ports.update(dict(selected))
    peer_env = {f"TOOLZ_{name.upper()}_URL": f"http://127.0.0.1:{port}"
                for name, port in peer_ports.items()}

    procs = []
    for name, port in selected:
        script = os.path.join(ROOT, TOOLS[name][0])
        if not os.path.exists(script):
            raise SystemExit(f"error: {script} not found")
        env = {**os.environ, **peer_env, "PORT": str(port)}
        proc = subprocess.Popen(
            [sys.executable, script],
            cwd=ROOT, env=env,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, bufsize=1,
        )
        procs.append((name, port, proc))
        threading.Thread(
            target=_pipe_logs, args=(name, proc, use_color), daemon=True
        ).start()

    print("\nlaser-toolz — launched:")
    for name, port, _ in procs:
        print(f"  {name:<9} http://127.0.0.1:{port}")
    print("Ctrl-C to stop all.\n")

    # Keep serving whatever's healthy: if one child dies (missing dep, port in
    # use) report it once and carry on with the rest; only exit when all are gone.
    reported = set()
    try:
        while True:
            alive = [name for name, _, proc in procs if proc.poll() is None]
            for name, _, proc in procs:
                if proc.poll() not in (None, 0) and name not in reported:
                    reported.add(name)
                    print(f"\n[{name}] exited (code {proc.returncode}) — "
                          f"the others keep running.", file=sys.stderr)
            if not alive:
                break
            time.sleep(0.4)
    except KeyboardInterrupt:
        print("\nshutting down…")
    finally:
        _shutdown(procs)
    return 0


if __name__ == "__main__":
    # Route SIGTERM through the same KeyboardInterrupt path as Ctrl-C so a
    # `kill` (or parent teardown) still tears the child servers down instead of
    # orphaning them.
    signal.signal(signal.SIGTERM, lambda *_: (_ for _ in ()).throw(KeyboardInterrupt))
    raise SystemExit(main())
