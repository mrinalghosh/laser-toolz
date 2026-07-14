"""
toolz_nav.py — the little switch-between-frontends nav shared by all three
web UIs (server.py / segment_server.py / epilogue_server.py).

Each server injects nav_html(<its own name>) into its page in place of the
`<!--TOOLZ-NAV-->` marker in the header. The nav styles itself entirely from the
CSS variables the three pages already share (--fg / --mut / --line / --acc), so
it matches each theme in both light and dark with no extra styling.

Sibling URLs default to the servers' standard ports (5001/5002/5003). The
`toolz.py` launcher exports TOOLZ_<NAME>_URL for each child so the links stay
correct even when a port is overridden; run standalone, the defaults apply.
"""

from __future__ import annotations

import os

# name -> (label, default URL). Ports match each server's default.
_TOOLS = {
    "linify":   ("linify",   "http://127.0.0.1:5001"),
    "segment":  ("segment",  "http://127.0.0.1:5002"),
    "epilogue": ("epilogue", "http://127.0.0.1:5003"),
}


def peer_url(name: str) -> str:
    """URL for one tool — TOOLZ_<NAME>_URL if the launcher set it, else default."""
    return os.environ.get(f"TOOLZ_{name.upper()}_URL", _TOOLS[name][1])


_STYLE = """<style>
.toolz-nav{margin-left:auto;display:flex;gap:6px;align-items:center;
  font-family:var(--mono,ui-monospace,monospace);}
.toolz-nav a{font-size:11px;letter-spacing:.04em;text-decoration:none;color:var(--mut);
  padding:4px 10px;border:1px solid var(--line);border-radius:7px;
  transition:color .12s,border-color .12s,background .12s;}
.toolz-nav a:hover{color:var(--fg);
  border-color:color-mix(in srgb,var(--acc) 45%,var(--line));}
.toolz-nav a.toolz-on{color:var(--acc-fg,#fff);background:var(--acc);
  border-color:var(--acc);cursor:default;}
</style>"""


def nav_html(active: str) -> str:
    """Header nav linking the three frontends, marking `active` as current."""
    links = []
    for name, (label, _) in _TOOLS.items():
        on = " toolz-on" if name == active else ""
        links.append(f'<a class="toolz-link{on}" href="{peer_url(name)}">{label}</a>')
    return f'{_STYLE}<nav class="toolz-nav">{"".join(links)}</nav>'
