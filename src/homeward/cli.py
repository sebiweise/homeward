"""Command line interface."""

import argparse
from importlib.metadata import PackageNotFoundError, version


def _version() -> str:
    try:
        return version("homeward")
    except PackageNotFoundError:
        return "0.0.0"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="homeward", description=__doc__)
    parser.add_argument("--version", action="version", version=f"%(prog)s {_version()}")
    parser.parse_args(argv)
    parser.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
