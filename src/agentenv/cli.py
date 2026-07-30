import argparse
import json
import sys
from pathlib import Path
from typing import Any

if sys.version_info >= (3, 10):
    from .api import serve
    from .orchestrator import AgentEnvError, Orchestrator


def _print(value):
    if hasattr(value, "to_dict"):
        value = value.to_dict()
    elif isinstance(value, list):
        value = [item.to_dict() if hasattr(item, "to_dict") else item for item in value]
    print(json.dumps(value, ensure_ascii=False, indent=2))


def _parser():
    parser = argparse.ArgumentParser(
        prog="aenv-py", description="AgentENV core-flow Python prototype"
    )
    parser.add_argument(
        "--data-dir", default=".agentenv", help="runtime data directory"
    )
    commands = parser.add_subparsers(dest="command", required=True)

    server = commands.add_parser("serve", help="start the HTTP API")
    server.add_argument("--host", default="127.0.0.1")
    server.add_argument("--port", type=int, default=8000)
    server.add_argument("--maintenance-interval", type=float, default=1.0)

    template = commands.add_parser("template-create", help="create a template")
    template.add_argument("name")
    template.add_argument("--source", default="scratch")
    template.add_argument("--base-dir")
    template.add_argument("--workdir", default=".")
    template.add_argument("--env", action="append", default=[], metavar="KEY=VALUE")
    commands.add_parser("template-list", help="list templates")
    template_delete = commands.add_parser(
        "template-delete", help="delete an unused template"
    )
    template_delete.add_argument("template_id")

    start = commands.add_parser("start", help="start a sandbox")
    start.add_argument("template")
    start.add_argument("--timeout", type=int)
    start.add_argument("--env", action="append", default=[], metavar="KEY=VALUE")

    commands.add_parser("list", aliases=["ls"], help="list sandboxes")
    inspect = commands.add_parser("inspect", help="show one sandbox")
    inspect.add_argument("sandbox_id")
    execute = commands.add_parser("exec", help="execute a command")
    execute.add_argument("sandbox_id")
    execute.add_argument("shell_command")
    execute.add_argument("--timeout", type=float)

    for name in ("pause", "resume", "snapshot", "delete"):
        operation = commands.add_parser(name)
        operation.add_argument("sandbox_id")

    fork = commands.add_parser("fork")
    fork.add_argument("sandbox_id")
    fork.add_argument("--count", type=int, default=1)

    commands.add_parser("snapshot-list", help="list snapshots")
    snapshot_delete = commands.add_parser(
        "snapshot-delete", help="delete an unused snapshot"
    )
    snapshot_delete.add_argument("snapshot_id")
    restore = commands.add_parser("restore", help="start from a snapshot")
    restore.add_argument("snapshot_id")
    restore.add_argument("--timeout", type=int)

    timeout = commands.add_parser("timeout", help="set or clear sandbox TTL")
    timeout.add_argument("sandbox_id")
    timeout_group = timeout.add_mutually_exclusive_group(required=True)
    timeout_group.add_argument("--seconds", type=int)
    timeout_group.add_argument("--clear", action="store_true")

    events = commands.add_parser("events", help="show lifecycle audit events")
    events.add_argument("--limit", type=int, default=20)
    commands.add_parser("status", help="show runtime summary")
    commands.add_parser("gc", help="remove expired sandboxes now")
    commands.add_parser("demo", help="run the complete core lifecycle")
    return parser


def _env(values):
    result = {}
    for value in values:
        key, separator, item = value.partition("=")
        if not separator or not key:
            raise AgentEnvError(f"invalid environment variable: {value}")
        result[key] = item
    return result


def _demo(orchestrator):
    try:
        template = orchestrator.create_template("demo", source="python:local")
    except AgentEnvError:
        template = orchestrator.store.get_template("demo")
        assert template
    sandbox = orchestrator.create_sandbox(template.id, env={"MESSAGE": "hello"})
    first = orchestrator.execute(
        sandbox.id,
        "printf '%s\\n' \"$MESSAGE\" > result.txt && cat result.txt",
    )
    orchestrator.pause(sandbox.id)
    orchestrator.resume(sandbox.id)
    snapshot = orchestrator.snapshot(sandbox.id)
    child = orchestrator.create_sandbox(snapshot_id=snapshot.id)
    second = orchestrator.execute(child.id, "cat result.txt")
    forked = orchestrator.fork(sandbox.id, count=2)
    return {
        "template": template.to_dict(),
        "sandbox": orchestrator.get_sandbox(sandbox.id).to_dict(),
        "first_command": first.to_dict(),
        "snapshot": snapshot.to_dict(),
        "restored_sandbox": child.to_dict(),
        "restored_command": second.to_dict(),
        "forked_sandboxes": [item.to_dict() for item in forked],
        "status": orchestrator.status(),
        "recent_events": [
            event.to_dict() for event in orchestrator.list_events(limit=10)
        ],
    }


def main(argv=None):
    if sys.version_info < (3, 10):
        print(
            "error: agentenv-py requires Python 3.10 or newer; "
            "this interpreter is Python {}.{}. Use: python3.10 -m agentenv ...".format(
                sys.version_info[0], sys.version_info[1]
            ),
            file=sys.stderr,
        )
        return 2
    args = _parser().parse_args(argv)
    orchestrator = Orchestrator(Path(args.data_dir))
    try:
        if args.command == "serve":
            if args.maintenance_interval <= 0:
                raise AgentEnvError("maintenance interval must be greater than zero")
            serve(
                orchestrator,
                args.host,
                args.port,
                maintenance_interval=args.maintenance_interval,
            )
        elif args.command == "template-create":
            _print(
                orchestrator.create_template(
                    args.name,
                    args.source,
                    args.base_dir,
                    _env(args.env),
                    args.workdir,
                )
            )
        elif args.command == "template-list":
            _print(orchestrator.list_templates())
        elif args.command == "template-delete":
            orchestrator.delete_template(args.template_id)
            _print({"deleted": args.template_id})
        elif args.command == "start":
            _print(
                orchestrator.create_sandbox(
                    args.template,
                    env=_env(args.env),
                    timeout_seconds=args.timeout,
                )
            )
        elif args.command in ("list", "ls"):
            _print(orchestrator.list_sandboxes())
        elif args.command == "inspect":
            _print(orchestrator.get_sandbox(args.sandbox_id))
        elif args.command == "exec":
            result = orchestrator.execute(
                args.sandbox_id, args.shell_command, args.timeout
            )
            _print(result)
            return result.exit_code
        elif args.command == "pause":
            _print(orchestrator.pause(args.sandbox_id))
        elif args.command == "resume":
            _print(orchestrator.resume(args.sandbox_id))
        elif args.command == "snapshot":
            _print(orchestrator.snapshot(args.sandbox_id))
        elif args.command == "snapshot-list":
            _print(orchestrator.list_snapshots())
        elif args.command == "snapshot-delete":
            orchestrator.delete_snapshot(args.snapshot_id)
            _print({"deleted": args.snapshot_id})
        elif args.command == "restore":
            _print(
                orchestrator.create_sandbox(
                    snapshot_id=args.snapshot_id,
                    timeout_seconds=args.timeout,
                )
            )
        elif args.command == "fork":
            _print(orchestrator.fork(args.sandbox_id, args.count))
        elif args.command == "delete":
            orchestrator.delete(args.sandbox_id)
            _print({"deleted": args.sandbox_id})
        elif args.command == "timeout":
            _print(
                orchestrator.update_timeout(
                    args.sandbox_id, None if args.clear else args.seconds
                )
            )
        elif args.command == "events":
            _print(orchestrator.list_events(args.limit))
        elif args.command == "status":
            _print(orchestrator.status())
        elif args.command == "gc":
            _print({"evicted": orchestrator.evict_expired()})
        elif args.command == "demo":
            _print(_demo(orchestrator))
        return 0
    except (AgentEnvError, OSError, ValueError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
