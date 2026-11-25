"""Configure or inspect the local source modes without loading provider credentials."""
import argparse
from contextlib import closing
from http.client import HTTPConnection, HTTPException
import json
import sys

from provider_modes import MODES


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8001)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("status")
    configure = commands.add_parser("configure")
    configure.add_argument("mode", choices=MODES)
    configure.add_argument("--revision", required=True, type=int, help="Current revision from status; prevents stale edits")
    sync = commands.add_parser("sync")
    sync.add_argument("--from", dest="first")
    sync.add_argument("--to", dest="last")
    args = parser.parse_args()
    if not 1 <= args.port <= 65535:
        parser.error("Port must be between 1 and 65535.")
    path, body = "/api/providers", None
    if args.command == "configure":
        path, body = "/api/providers/config", {"mode": args.mode, "expected_revision": args.revision}
    elif args.command == "sync":
        path, body = "/api/providers/import", {}
        if args.first is not None or args.last is not None:
            body = {"from": args.first, "to": args.last}
    try:
        with closing(HTTPConnection("127.0.0.1", args.port, timeout=20)) as connection:
            connection.request("GET" if body is None else "POST", path,
                               body=None if body is None else json.dumps(body), headers={"Content-Type": "application/json"})
            with connection.getresponse() as response:
                result = json.load(response)
                if not 200 <= response.status < 300:
                    print(f"Local source settings: HTTP {response.status}. Check status and request values.", file=sys.stderr)
                    return 1
                print(json.dumps(result, indent=2))
    except (OSError, HTTPException, ValueError):
        print("Could not read the local source API. Check that the local app is running.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
