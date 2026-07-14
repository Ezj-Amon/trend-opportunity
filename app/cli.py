from __future__ import annotations

import argparse
import asyncio
import json

from .config import Settings
from .db import Database
from .pipeline import Pipeline


async def run_pipeline() -> None:
    settings = Settings.from_env()
    db = Database(settings.database_path)
    db.initialize()
    run_id = await Pipeline(db, settings).run("cli")
    run = db.one("SELECT * FROM pipeline_runs WHERE id=?", (run_id,))
    print(json.dumps(run, ensure_ascii=False, indent=2))


async def rebuild_pipeline() -> None:
    settings = Settings.from_env()
    db = Database(settings.database_path)
    db.initialize()
    db.clear_derived_data()
    run_id = await Pipeline(db, settings).run("rebuild")
    run = db.one("SELECT * FROM pipeline_runs WHERE id=?", (run_id,))
    print(json.dumps(run, ensure_ascii=False, indent=2))


def main() -> None:
    parser = argparse.ArgumentParser(description="Trend opportunity assistant")
    parser.add_argument("command", choices=["run", "rebuild"])
    args = parser.parse_args()
    if args.command == "run":
        asyncio.run(run_pipeline())
    elif args.command == "rebuild":
        asyncio.run(rebuild_pipeline())


if __name__ == "__main__":
    main()
