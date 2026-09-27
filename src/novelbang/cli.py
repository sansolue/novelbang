import argparse
import threading
import webbrowser
from pathlib import Path

import uvicorn


def main() -> None:
    parser = argparse.ArgumentParser(prog="novelbang", description="Local novel writing studio")
    parser.add_argument(
        "workspace", nargs="?", default=str(Path.home() / "Novels"),
        help="folder that holds your novel projects (default: ~/Novels)",
    )
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument(
        "--allow-host", action="append", default=[], metavar="NAME",
        help="extra host name the UI may be reached at, e.g. this machine's LAN name (repeatable)",
    )
    parser.add_argument("--no-browser", action="store_true", help="don't open a browser tab")
    args = parser.parse_args()

    from .server import create_app

    workspace = Path(args.workspace).expanduser().resolve()
    # Never allow any Host header: that would re-open DNS rebinding on an app with
    # no authentication. A wildcard bind address isn't a name clients use, so
    # remote access needs the real name passed with --allow-host.
    wildcard = args.host in ("0.0.0.0", "::", "")
    extra = tuple(args.allow_host) + (() if wildcard else (args.host,))
    app = create_app(workspace, extra_hosts=extra)
    url = f"http://{'127.0.0.1' if wildcard else args.host}:{args.port}"
    if wildcard and not args.allow_host:
        print("NovelBang: listening on all interfaces, but only localhost is allowed;"
              " pass --allow-host <name> to reach it from another machine.")
    print(f"NovelBang: workspace {workspace}\n           open {url}")
    if not args.no_browser:
        threading.Timer(1.0, webbrowser.open, [url]).start()
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")
