#!/usr/bin/env python3
"""motion_actionctl — the board-side admin CLI the GUI drives over SSH.

Reads (`list`) go straight to the action directory, so the catalogue is
readable even when base_host is down. Everything that RUNS or MUTATES goes
through base_host's local control socket, because base_host is the only thing
that knows what is playing right now — that is what makes "refuse to delete or
overwrite an action that is playing" one rule instead of two racing ones.

Always prints exactly one JSON object on stdout and exits 0 when it got an
answer: `{"ok": false, "error": ...}` is a normal answer, not a crash. Exit 2
means no answer at all (base_host unreachable, bad usage).

    motion_actionctl list --json
    motion_actionctl record-start --scope full
    motion_actionctl record-stop
    motion_actionctl save yes --label "点头 / Yes" --description "..."
    motion_actionctl preview
    motion_actionctl play yes
    motion_actionctl stop
    motion_actionctl status
    motion_actionctl delete yes
    motion_actionctl restore yes.20260731T103500.json
    motion_actionctl purge-trash --days 30
"""
from __future__ import annotations

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from motion_actions import control, store


def _emit(payload):
    print(json.dumps(payload, ensure_ascii=False))
    return 0


def _call(request, timeout=control.DEFAULT_TIMEOUT_S):
    try:
        return _emit(control.call(request, timeout=timeout))
    except control.ControlError as exc:
        _emit({"ok": False, "error": "host_unreachable", "detail": str(exc)})
        return 2


def build_parser():
    parser = argparse.ArgumentParser(prog="motion_actionctl", description=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)

    listing = sub.add_parser("list", help="read the action directory")
    listing.add_argument("--json", action="store_true",
                         help="accepted for symmetry; output is always JSON")

    rec = sub.add_parser("record-start", help="start the canonical recorder")
    rec.add_argument("--scope", default="arm", choices=("arm", "base", "full"))

    sub.add_parser("record-stop", help="stop recording into an in-memory draft")
    sub.add_parser("record-discard", help="throw the draft away")

    save = sub.add_parser("save", help="save the draft as an action file")
    save.add_argument("action_id")
    save.add_argument("--label", default="")
    save.add_argument("--description", default="")
    save.add_argument("--overwrite", action="store_true")

    sub.add_parser("preview", help="play the unsaved draft")

    play = sub.add_parser("play", help="play a saved action (blocks)")
    play.add_argument("action_id")

    sub.add_parser("stop", help="stop the running action (idempotent)")
    sub.add_parser("status", help="recorder / player / lease state")

    delete = sub.add_parser("delete", help="move an action into .trash/")
    delete.add_argument("action_id")

    restore = sub.add_parser("restore", help="undo a delete")
    restore.add_argument("trash_name")

    purge = sub.add_parser("purge-trash", help="drop old trash (explicit only)")
    purge.add_argument("--days", type=int, default=store.TRASH_KEEP_DAYS)
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    cmd = args.cmd

    if cmd == "list":
        root = store.actions_root()
        return _emit({"ok": True, "actions_dir": root,
                      **store.list_actions(root), "trash": store.list_trash(root)})
    if cmd == "record-start":
        return _call({"op": "record_start", "scope": args.scope})
    if cmd == "record-stop":
        return _call({"op": "record_stop"})
    if cmd == "record-discard":
        return _call({"op": "record_discard"})
    if cmd == "save":
        return _call({"op": "record_save", "action_id": args.action_id,
                      "label": args.label, "description": args.description,
                      "overwrite": args.overwrite})
    if cmd == "preview":
        return _call({"op": "play", "preview": True}, control.PLAY_TIMEOUT_S)
    if cmd == "play":
        return _call({"op": "play", "action_id": args.action_id},
                     control.PLAY_TIMEOUT_S)
    if cmd == "stop":
        return _call({"op": "stop", "by": "gui"})
    if cmd == "status":
        return _call({"op": "status"})
    if cmd == "delete":
        return _call({"op": "delete", "action_id": args.action_id})
    if cmd == "restore":
        return _call({"op": "restore", "trash_name": args.trash_name})
    if cmd == "purge-trash":
        return _call({"op": "purge_trash", "keep_days": args.days})
    return _emit({"ok": False, "error": "unknown_command", "detail": cmd}) or 2


if __name__ == "__main__":
    sys.exit(main())
