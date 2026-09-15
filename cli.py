# cli.py - command-line client for the plugin lifecycle manager
import argparse
import json
import sys
from pathlib import Path

from config import DEFAULT_CONFIG_PATH, AppConfig
from plugins.loader import discover_contributions
from plugins.manager import PluginManager


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python cli.py")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG_PATH)
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

    mcp = commands.add_parser("mcp", help="manage MCP startup settings")
    mcp_commands = mcp.add_subparsers(dest="mcp_command", required=True)

    mcp_list = mcp_commands.add_parser("list", help="list available MCP plugins")
    mcp_list.add_argument("--json", action="store_true", help="emit JSON")

    preload = mcp_commands.add_parser("preload", help="manage startup preloads")
    preload_commands = preload.add_subparsers(dest="preload_command", required=True)
    for name in ("add", "remove"):
        action = preload_commands.add_parser(name, help=f"{name} an MCP preload")
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
        if args.command == "mcp":
            return _handle_mcp(args, manager)
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


def _handle_mcp(args: argparse.Namespace, manager: PluginManager) -> int:
    available = _available_mcp_names(manager)
    config = AppConfig.load(args.config)
    preloaded = list(config.mcp_preload)

    if args.mcp_command == "list":
        if args.json:
            print(
                json.dumps(
                    {"available": available, "preload": preloaded},
                    ensure_ascii=False,
                    indent=2,
                )
            )
            return 0
        if not available:
            print("no available MCP plugins")
        for name in available:
            marker = "*" if name in preloaded else " "
            print(f"{marker} {name}")
        for name in preloaded:
            if name not in available:
                print(f"! {name} (not available)")
        return 0

    name = args.name
    if args.preload_command == "add":
        if name not in available:
            raise ValueError(f"unknown or disabled MCP plugin: {name}")
        if name in preloaded:
            print(f"{name} is already preloaded")
            return 0
        preloaded.append(name)
        _write_preload(args.config, preloaded)
        print(f"preload enabled: {name}")
        return 0

    if name not in preloaded:
        print(f"{name} is not preloaded")
        return 0
    preloaded.remove(name)
    _write_preload(args.config, preloaded)
    print(f"preload disabled: {name}")
    return 0


def _available_mcp_names(manager: PluginManager) -> list[str]:
    roots: list[Path] = [manager.builtin_dir]
    roots.extend(manager.plugin_path(record.name) for record in manager.enabled_records())
    contributions = discover_contributions(roots)
    return sorted(
        {contribution.manifest.name for contribution in contributions if contribution.kind == "mcp"}
    )


def _write_preload(path: Path, preloaded: list[str]) -> None:
    config_path = path / "config.json" if path.is_dir() else path
    target = config_path.parent / "mcp.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(f".{target.name}.tmp")
    temporary.write_text(
        json.dumps({"preload": preloaded}, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary.replace(target)


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
        desired = "enabled" if record.enabled else "disabled"
        print(
            f"{record.name}\t{record.version or '-'}\t{desired}"
            f"\t{record.runtime_status}\t{record.path}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
