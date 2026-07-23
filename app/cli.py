from __future__ import annotations

import argparse
import asyncio
import json

import uvicorn

from .config import Settings
from .db import Database
from .pipeline import Pipeline


async def collect_pipeline() -> None:
    settings = Settings.from_env()
    db = Database(settings.database_path)
    db.initialize()
    run_id = await Pipeline(db, settings).run("cli")
    run = db.one("SELECT * FROM pipeline_runs WHERE id=?", (run_id,))
    print(json.dumps(run, ensure_ascii=False, indent=2))


def run_server() -> None:
    """Start the web application without triggering a Pipeline collection."""
    uvicorn.run("app.main:app", host="127.0.0.1", port=8000)


async def rebuild_pipeline() -> None:
    settings = Settings.from_env()
    db = Database(settings.database_path)
    db.initialize()
    db.clear_derived_data()
    run_id = await Pipeline(db, settings).run("rebuild")
    run = db.one("SELECT * FROM pipeline_runs WHERE id=?", (run_id,))
    print(json.dumps(run, ensure_ascii=False, indent=2))


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Trend opportunity assistant")
    parser.add_argument("command", choices=["run", "collect", "rebuild"])
    args = parser.parse_args(argv)
    if args.command == "run":
        run_server()
    elif args.command == "collect":
        asyncio.run(collect_pipeline())
    elif args.command == "rebuild":
        asyncio.run(rebuild_pipeline())


if __name__ == "__main__":
    main()
