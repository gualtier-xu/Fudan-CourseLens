"""Offline, synthetic CourseLens performance baseline.

The harness never starts the production server and never reads the user's data
root. Every measurement uses a generated SQLite database or loopback-only HTTP
server under an explicit output directory.
"""

from __future__ import annotations

import argparse
from contextlib import closing
import json
import math
import os
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from statistics import median
from typing import Callable
from urllib.request import urlopen

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


DEFAULT_BUDGETS_MS = {
    "cold_start": 1500.0,
    "warm_start": 1000.0,
    "empty_database": 50.0,
    "synthetic_database": 100.0,
    "search": 100.0,
    "course_switch": 50.0,
    "media_first_byte": 300.0,
    "exit": 1000.0,
}


def _elapsed_ms(call: Callable[[], object]) -> float:
    started = time.perf_counter_ns()
    call()
    return (time.perf_counter_ns() - started) / 1_000_000


def _percentile(values: list[float], fraction: float) -> float:
    ordered = sorted(values)
    return ordered[max(0, math.ceil(len(ordered) * fraction) - 1)]


def _summary(values: list[float]) -> dict[str, object]:
    return {
        "runs": len(values),
        "median_ms": round(median(values), 3),
        "p95_ms": round(_percentile(values, 0.95), 3),
        "samples_ms": [round(value, 3) for value in values],
    }


def _create_database(path: Path, rows: int) -> None:
    with closing(sqlite3.connect(path)) as database:
        database.executescript(
            """
            CREATE TABLE courses(
              course_id TEXT PRIMARY KEY, title TEXT NOT NULL, synthetic_text TEXT NOT NULL
            );
            CREATE INDEX courses_title ON courses(title);
            """
        )
        database.executemany(
            "INSERT INTO courses VALUES(?,?,?)",
            (
                (f"synthetic-{index:05d}", f"Synthetic Course {index:05d}",
                 f"generated offline token {index % 97}")
                for index in range(rows)
            ),
        )
        database.commit()


def _query(path: Path, sql: str, parameters: tuple[object, ...] = ()) -> None:
    with closing(sqlite3.connect(path)) as database:
        database.execute(sql, parameters).fetchall()


class _MediaHandler(BaseHTTPRequestHandler):
    payload = b"x" * 64 * 1024

    def do_GET(self) -> None:  # noqa: N802
        self.send_response(200)
        self.send_header("Content-Length", str(len(self.payload)))
        self.end_headers()
        self.wfile.write(self.payload)

    def log_message(self, format: str, *args: object) -> None:
        return


def _read_first_byte(url: str) -> None:
    with urlopen(url, timeout=2) as response:
        response.read(1)


def _subprocess_ms(mode: str, data_root: Path) -> float:
    command = [
        sys.executable, str(Path(__file__).resolve()), "--child", mode,
        "--child-data-root", str(data_root),
    ]
    environment = {
        **os.environ,
        "COURSELENS_DATA_ROOT": str(data_root),
        "PYTHONDONTWRITEBYTECODE": "1",
    }
    return _elapsed_ms(lambda: subprocess.run(
        command, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        env=environment, timeout=15,
    ))


def _child(mode: str, data_root: Path) -> int:
    if mode == "startup":
        import src.update.service  # noqa: F401
    elif mode == "exit":
        data_root.mkdir(parents=True, exist_ok=True)
    else:
        raise ValueError(mode)
    return 0


def run(output_dir: Path, repetitions: int, rows: int) -> dict[str, object]:
    output_dir = output_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="courselens-perf-", dir=output_dir) as raw:
        data_root = Path(raw)
        empty_db = data_root / "empty.sqlite3"
        populated_db = data_root / "synthetic.sqlite3"
        _create_database(empty_db, 0)
        _create_database(populated_db, rows)

        server = ThreadingHTTPServer(("127.0.0.1", 0), _MediaHandler)
        server_thread = threading.Thread(target=server.serve_forever, daemon=True)
        server_thread.start()
        media_url = f"http://127.0.0.1:{server.server_port}/synthetic-media"
        try:
            measurements: dict[str, list[float]] = {
                "cold_start": [
                    _subprocess_ms("startup", data_root / f"cold-{index}")
                    for index in range(repetitions)
                ],
                "warm_start": [
                    _subprocess_ms("startup", data_root / "warm")
                    for _ in range(repetitions)
                ],
                "empty_database": [
                    _elapsed_ms(lambda: _query(empty_db, "SELECT COUNT(*) FROM courses"))
                    for _ in range(repetitions)
                ],
                "synthetic_database": [
                    _elapsed_ms(lambda: _query(
                        populated_db, "SELECT COUNT(*), MAX(course_id) FROM courses"
                    ))
                    for _ in range(repetitions)
                ],
                "search": [
                    _elapsed_ms(lambda: _query(
                        populated_db,
                        "SELECT course_id FROM courses WHERE title LIKE ? LIMIT 25",
                        ("%Course 000%",),
                    ))
                    for _ in range(repetitions)
                ],
                "course_switch": [
                    _elapsed_ms(lambda index=index: _query(
                        populated_db,
                        "SELECT * FROM courses WHERE course_id=?",
                        (f"synthetic-{index % rows:05d}",),
                    ))
                    for index in range(repetitions)
                ],
                "media_first_byte": [
                    _elapsed_ms(lambda: _read_first_byte(media_url))
                    for _ in range(repetitions)
                ],
                "exit": [
                    _subprocess_ms("exit", data_root / "exit")
                    for _ in range(repetitions)
                ],
            }
        finally:
            server.shutdown()
            server.server_close()
            server_thread.join(timeout=5)
            if server_thread.is_alive():
                raise RuntimeError("local_media_server_did_not_stop")
        return {
            "schema": "courselens.local-performance-baseline.v1",
            "isolation": {
                "synthetic_only": True,
                "network": "loopback_only",
                "data_root": "temporary_child_of_output_dir",
                "rows": rows,
            },
            "metrics": {name: _summary(values) for name, values in measurements.items()},
        }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--repetitions", type=int, default=5)
    parser.add_argument("--synthetic-rows", type=int, default=5000)
    parser.add_argument("--budgets", type=Path)
    parser.add_argument("--compare", type=Path)
    parser.add_argument("--regression-threshold", type=float, default=0.20)
    parser.add_argument("--enforce", action="store_true")
    parser.add_argument("--child", choices=("startup", "exit"))
    parser.add_argument("--child-data-root", type=Path)
    args = parser.parse_args(argv)
    if args.child:
        if args.child_data_root is None:
            parser.error("--child-data-root is required with --child")
        return _child(args.child, args.child_data_root)
    if args.output_dir is None:
        parser.error("--output-dir is required")
    if args.repetitions < 5:
        parser.error("--repetitions must be at least 5")
    if args.synthetic_rows < 1:
        parser.error("--synthetic-rows must be positive")
    budgets = dict(DEFAULT_BUDGETS_MS)
    if args.budgets:
        budgets.update(json.loads(args.budgets.read_text(encoding="utf-8")))
    result = run(args.output_dir, args.repetitions, args.synthetic_rows)
    regressions: dict[str, object] = {}
    previous = {}
    if args.compare:
        previous = json.loads(args.compare.read_text(encoding="utf-8")).get("metrics", {})
    for name, metric in result["metrics"].items():
        p95 = float(metric["p95_ms"])
        budget = float(budgets[name])
        old = float(previous.get(name, {}).get("p95_ms") or 0)
        regressions[name] = {
            "soft_budget_ms": budget,
            "budget_exceeded": p95 > budget,
            "comparison_p95_ms": old or None,
            "regression": bool(old and p95 > old * (1 + args.regression_threshold)),
        }
    result["policy"] = {
        "regression_threshold": args.regression_threshold,
        "enforced": args.enforce,
        "metrics": regressions,
    }
    destination = args.output_dir.resolve() / "performance-baseline.json"
    destination.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(destination)
    failed = any(
        value["budget_exceeded"] or value["regression"] for value in regressions.values()
    )
    return 1 if args.enforce and failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
