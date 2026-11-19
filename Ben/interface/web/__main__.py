"""Start the React scan console: python -m Ben.interface.web."""

from __future__ import annotations

import argparse
import time
from pathlib import Path

from Ben.core.paths import latest_run_dir, runs_base_dir
from Ben.interface.viewer.launcher import ScanLauncher
from Ben.interface.viewer.server import authorized_url, bundle_is_built, serve


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--no-open", action="store_true")
    args = parser.parse_args()
    if not bundle_is_built():
        parser.error(
            "Build React first: cd Ben/interface/viewer/frontend && npm ci && npm run build"
        )
    root = Path.cwd()
    launcher = ScanLauncher(root)
    run = latest_run_dir() or runs_base_dir(cwd=root) / "_empty"
    server, url, token = serve(
        run, port=args.port, open_browser=not args.no_open, launcher=launcher
    )
    print(authorized_url(url, token), flush=True)
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        server.shutdown()
        server.server_close()


if __name__ == "__main__":
    main()
