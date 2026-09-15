# cli.py - command-line client for the plugin lifecycle manager
import argparse
import json
import sys
from pathlib import Path

from plugins.manager import PluginManager


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m cli")
    parser.add_argument("--registry", type=Path, help="path to plugin-registry.json")
    parser.add_argument("--store-dir", type=Path, help="plugin package store directory")
    parser.add_argument("--plugin-dir", type=Path, help="built-in plugin directory")
    commands = parser.add_subparsers(dest="command", required=True)

    plugin = commands.add_parser("plugin", help="manage plugin packages")
    plugin_commands = plugin.add_subparsers(dest="plugin_command", required=True)

    list_command = plugin_commands.add_parser("list", help="list installed packages")
    list_command.add_argument("--json", action="store_true", help="emit JSON")

    validate = plugin_commands.add_parser("validate", help="validate a package")
    validate.add_argument("source", type=Path)

    install = plugin_commands.add_parser("install", help="install a package")
    install.add_argument("source", type=Path)

    for name in ("enable", "disable", "remove"):
        action = plugin_commands.add_parser(name, help=f"{name} an installed package")
        action.add_argument("name")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        manager = PluginManager(
            registry_path=args.registry,
            store_dir=args.store_dir,
            builtin_dir=args.plugin_dir,
        )
        if args.plugin_command == "list":
            return _list(manager, as_json=args.json)
        if args.plugin_command == "validate":
            inspection = manager.validate(args.source)
            print(f"{inspection.name} {inspection.version or '(no version)'}: valid")
            for contribution in inspection.contributions:
                print(f"  {contribution.kind}: {contribution.contribution_id}")
            return 0
        if args.plugin_command == "install":
            record = manager.install(args.source)
            print(f"installed {record.name} {record.version or '(no version)'} (disabled)")
            return 0
        if args.plugin_command == "enable":
            record = manager.enable(args.name)
            print(f"enabled {record.name}")
            return 0
        if args.plugin_command == "disable":
            record = manager.disable(args.name)
            print(f"disabled {record.name}")
            return 0
        if args.plugin_command == "remove":
            manager.remove(args.name)
            print(f"removed {args.name}")
            return 0
    except (OSError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    return 2


def _list(manager: PluginManager, *, as_json: bool) -> int:
    records = manager.list_installed()
    if as_json:
        print(
            json.dumps(
                [record.to_dict() | {"name": record.name} for record in records],
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0
    if not records:
        print("no installed plugin packages")
        return 0
    for record in records:
        status = "enabled" if record.enabled else "disabled"
        print(f"{record.name}\t{record.version or '-'}\t{status}\t{record.path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
