"""Where the board listens.

Several boards run at once — one per repo — so the port has to be predictable
per repo rather than a shared default that collides.  And a board is worth
looking at from a phone, which on a tailnet means binding to the Tailscale
address rather than loopback.
"""
from __future__ import annotations

import hashlib
import json
import os
import socket
import subprocess
from typing import Any

#: `auto` ports live here — high, unprivileged, and out of the way
AUTO_PORT_BASE = 7777
AUTO_PORT_SPAN = 100


def auto_port(root: str) -> int:
    """A stable port per repo. The same checkout always gets the same number,
    so a bookmark keeps working and two boards do not fight."""
    digest = hashlib.sha256(os.path.realpath(root).encode()).digest()
    return AUTO_PORT_BASE + (int.from_bytes(digest[:4], "big") % AUTO_PORT_SPAN)


def resolve_port(cfg: dict[str, Any], root: str,
                 override: int | None = None) -> int:
    if override:
        return int(override)
    configured = cfg.get("server", {}).get("port", AUTO_PORT_BASE)
    if isinstance(configured, str) and configured.strip().lower() == "auto":
        return auto_port(root)
    try:
        return int(configured)
    except (TypeError, ValueError):
        return auto_port(root)


def tailscale_ip() -> str | None:
    try:
        out = subprocess.run(["tailscale", "ip", "-4"], capture_output=True,
                             text=True, timeout=10)
    except Exception:
        return None
    if out.returncode != 0:
        return None
    for line in out.stdout.split():
        if line.count(".") == 3:
            return line.strip()
    return None


def tailscale_name() -> str | None:
    """The MagicDNS name, which is far nicer to type than 100.x.y.z."""
    try:
        out = subprocess.run(["tailscale", "status", "--json"],
                             capture_output=True, text=True, timeout=10)
        if out.returncode != 0:
            return None
        name = (json.loads(out.stdout).get("Self") or {}).get("DNSName") or ""
    except Exception:
        return None
    return name.rstrip(".") or None


def resolve_host(cfg: dict[str, Any],
                 override: str | None = None) -> tuple[str, list[str]]:
    """Returns (bind address, warnings).

    `local` loopback only (default)
    `tailscale` the tailnet address — reachable from your other devices,
                    and nothing else
    `any` every interface, including the local network
    """
    spec = (override or cfg.get("server", {}).get("host") or "local").strip()
    low = spec.lower()

    if low in ("local", "localhost", "loopback", "127.0.0.1"):
        return "127.0.0.1", []

    if low in ("tailscale", "ts", "tailnet"):
        ip = tailscale_ip()
        if not ip:
            return "127.0.0.1", [
                "server.host is 'tailscale' but no tailnet address was found "
                "(is tailscaled running, and are you logged in?) — falling back "
                "to loopback"]
        return ip, [
            "the board is on your tailnet and has no login of its own — "
            "your tailnet ACLs are the only thing gating access to it"]

    if low in ("any", "all", "0.0.0.0", "*"):
        return "0.0.0.0", [
            "the board is bound to every interface with no login of its own — "
            "anyone who can reach this machine can read and change the board"]

    return spec, ([] if spec.startswith("127.") else [
        f"the board is bound to {spec} and has no login of its own"])


def display_urls(host: str, port: int) -> list[str]:
    """What to actually tell someone to open."""
    if host == "0.0.0.0":
        return [f"http://127.0.0.1:{port}", f"http://{local_ip()}:{port}"]
    urls = [f"http://{host}:{port}"]
    if host == tailscale_ip():
        name = tailscale_name()
        if name:
            urls.insert(0, f"http://{name}:{port}")
    return urls


def local_ip() -> str:
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("192.0.2.1", 1))     # TEST-NET-1: routes nowhere, just reveals the interface
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"


def port_is_free(host: str, port: int) -> bool:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        s.bind((host, port))
        return True
    except OSError:
        return False
    finally:
        s.close()
