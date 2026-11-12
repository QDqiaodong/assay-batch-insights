import argparse
import json
from pathlib import Path
from .report import run


def main():
    parser = argparse.ArgumentParser(description="Analyze synthetic calibrated instrument batches")
    parser.add_argument("--calibrations", type=Path, default=Path("data/calibrations.csv"))
    parser.add_argument("--results", type=Path, default=Path("data/results.csv"))
    parser.add_argument("--policy", type=Path, default=Path("data/policy.json"))
    parser.add_argument("--output", type=Path, default=Path("run/report"))
    args = parser.parse_args()
    try:
        result = run(args.calibrations, args.results, args.policy, args.output)
    except (OSError, ValueError) as exc:
        parser.exit(2, f"analysis failed: {exc}\n")
    print(json.dumps(result["summary"], sort_keys=True))


if __name__ == "__main__":
    main()
