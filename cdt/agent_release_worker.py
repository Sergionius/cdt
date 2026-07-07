import argparse
import subprocess
import sys
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--pipeline", required=True)
    parser.add_argument("--log", required=True)
    parser.add_argument("--exit-file", required=True)
    parser.add_argument("--status-file", required=True)
    parser.add_argument("--id", action="append", default=[])
    args = parser.parse_args()

    cmd = [sys.executable, "-m", "cdt", "run", args.pipeline, "--status-file", args.status_file]
    for task_id in args.id:
        cmd.extend(["--id", task_id])

    log_path = Path(args.log)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("ab") as log:
        process = subprocess.Popen(cmd, stdout=log, stderr=subprocess.STDOUT)
        exit_code = process.wait()

    Path(args.exit_file).write_text(f"{exit_code}\n", encoding="utf-8")
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
