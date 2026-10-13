"""Update the config after a package upgrade (run from ``service_postupgrade``)."""

from homeward.api.store import ConfigStore
from homeward.runtime import Paths


def main() -> int:
    for name in ConfigStore(Paths.from_env()).migrate():
        print(f"Pair {name}: set beta = true (bidirectional sync is in beta)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
