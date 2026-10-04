"""Command-line interface: the `python3 -m todo` entry point, built on argparse."""

from __future__ import annotations

import argparse
import sys
from typing import Sequence, TextIO

from todo import commands
from todo.format import format_task, format_tasks
from todo.storage import TaskStore


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="todo", description="A tiny command-line todo manager.")
    sub = parser.add_subparsers(dest="command", required=True)
    add = sub.add_parser("add", help="add a task")
    add.add_argument("title")
    listing = sub.add_parser("list", help="list open tasks")
    listing.add_argument("--all", action="store_true", help="include completed tasks")
    for name, help_text in (("done", "mark a task as completed"), ("remove", "delete a task")):
        command = sub.add_parser(name, help=help_text)
        command.add_argument("id", type=int)
    return parser


def main(
    argv: Sequence[str] | None = None,
    *,
    store: TaskStore | None = None,
    out: TextIO | None = None,
    err: TextIO | None = None,
) -> int:
    out = out or sys.stdout
    err = err or sys.stderr
    try:
        args = build_parser().parse_args(argv)
    except SystemExit as exc:
        return 0 if exc.code == 0 else 2
    store = store or TaskStore()
    try:
        if args.command == "add":
            print(f"Added {format_task(commands.add(store, args.title))}", file=out)
        elif args.command == "list":
            print(format_tasks(commands.list_tasks(store, include_done=args.all)), file=out)
        elif args.command == "done":
            print(f"Completed {format_task(commands.done(store, args.id))}", file=out)
        elif args.command == "remove":
            print(f"Removed {format_task(commands.remove(store, args.id))}", file=out)
    except commands.TaskNotFound as exc:
        print(f"error: {exc}", file=err)
        return 1
    except ValueError as exc:
        print(f"error: {exc}", file=err)
        return 2
    return 0
