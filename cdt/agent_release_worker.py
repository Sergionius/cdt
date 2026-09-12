import argparse
import codecs
import subprocess
import sys
import traceback
from pathlib import Path

from .config import _load_project_env
from .redaction import SecretRedactor, StreamingRedactor
from .runs import RUN_SCHEMA_VERSION, now, read_json, write_exit_code, write_json_atomic


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--pipeline", required=True)
    parser.add_argument("--run-id")
    parser.add_argument("--log", required=True)
    parser.add_argument("--exit-file", required=True)
    parser.add_argument("--status-file", required=True)
    parser.add_argument("--id", action="append", default=[])
    parser.add_argument("--input", action="append", default=[])
    parser.add_argument("--confirm")
    args = parser.parse_args()

    cmd = [sys.executable, "-m", "cdt", "run", args.pipeline, "--status-file", args.status_file]
    if args.run_id is not None:
        cmd.extend(["--run-id", args.run_id])
    for task_id in args.id:
        cmd.extend(["--id", task_id])
    for entry in args.input:
        cmd.extend(["--input", entry])
    if args.confirm is not None:
        cmd.extend(["--confirm", args.confirm])

    log_path = Path(args.log)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    exit_file = Path(args.exit_file)
    exit_file.parent.mkdir(parents=True, exist_ok=True)
    redactor = SecretRedactor.from_env(_load_project_env(Path.cwd()))
    stream = StreamingRedactor(redactor)
    decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
    exit_code = 1
    try:
        with log_path.open("a", encoding="utf-8") as log:
            process = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
            if process.stdout is None:
                raise RuntimeError("Release worker did not receive a subprocess output stream")
            while chunk := process.stdout.read(65536):
                log.write(stream.feed(decoder.decode(chunk)))
                log.flush()
            log.write(stream.feed(decoder.decode(b"", final=True), final=True))
            log.flush()
            exit_code = process.wait()
            status_path = Path(args.status_file)
            payload = read_json(status_path) or {}
            if payload.get("status") not in {"success", "failed", "cancelled", "blocked"}:
                if exit_code == 0:
                    exit_code = 1
                    error = "CDT subprocess exited without writing a terminal status"
                else:
                    error = f"CDT subprocess exited with code {exit_code} before writing a terminal status"
                payload.update(
                    {
                        "schema_version": RUN_SCHEMA_VERSION,
                        "run_id": args.run_id,
                        "pipeline": args.pipeline,
                        "status": "failed",
                        "error": error,
                        "finished_at": now(),
                        "updated_at": now(),
                    }
                )
                write_json_atomic(status_path, redactor.redact_data(payload))
    except Exception:
        message = redactor.redact(
            "\nagent_release_worker failed before or during cdt run startup:\n" + traceback.format_exc()
        )
        with log_path.open("a", encoding="utf-8") as log:
            log.write(message)
        status_path = Path(args.status_file)
        payload = read_json(status_path) or {
            "schema_version": RUN_SCHEMA_VERSION,
            "run_id": args.run_id,
            "pipeline": args.pipeline,
        }
        payload.update({"status": "failed", "error": message.strip(), "finished_at": now(), "updated_at": now()})
        write_json_atomic(status_path, redactor.redact_data(payload))
    finally:
        write_exit_code(exit_file, exit_code)
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
