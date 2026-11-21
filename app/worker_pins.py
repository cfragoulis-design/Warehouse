"""Set kiosk worker PINs from the command line.

The PIN is hashed before it is stored. This command prints the worker's name
and never prints the digits.

    python -m app.worker_pins fabaad68 1234
    python -m app.worker_pins --file pins.txt

Each line of the file is ``<one_employee_ref or id> <4 digits>``.
"""

from __future__ import annotations

import argparse
import sys

try:
    from app.db import SessionLocal
    from app.worker_service import assign_worker_pin, parse_pin_assignments
except ImportError:  # pragma: no cover - flat module layout
    from db import SessionLocal
    from worker_service import assign_worker_pin, parse_pin_assignments


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Set a worker PIN")
    parser.add_argument("ref", nargs="?", help="one_employee_ref or worker id")
    parser.add_argument("pin", nargs="?", help="4 digits")
    parser.add_argument("--file", help="file of ref and PIN lines")
    args = parser.parse_args(argv)
    if args.file:
        with open(args.file, encoding="utf-8") as handle:
            text = handle.read()
    elif args.ref and args.pin:
        text = f"{args.ref} {args.pin}"
    else:
        print("Give a ref and a PIN, or --file.", file=sys.stderr)
        return 2
    try:
        rows = parse_pin_assignments(text)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    db = SessionLocal()
    try:
        names: list[str] = []
        for key, pin in rows:
            worker = assign_worker_pin(db, key, pin)
            names.append(worker.display_name)
        db.commit()
    except ValueError as exc:
        db.rollback()
        message = "Unknown worker" if "Unknown" in str(exc) else "PIN must be 4 digits"
        print(message, file=sys.stderr)
        return 2
    finally:
        db.close()
    for name in names:
        print(f"PIN set for {name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
