# cli.py - command-line client for the plugin lifecycle manager
import argparse
import json
import sys
from dataclasses import asdict
from pathlib import Path

from config import DEFAULT_CONFIG_PATH, AppConfig
from gateways.skill_gateway import SkillGateway, SkillPermissionPolicy
from plugins.loader import SkillPlugin, discover_contributions, load_skill_plugin
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

    skill = commands.add_parser("skill", help="inspect and manually invoke Skills")
    skill_commands = skill.add_subparsers(dest="skill_command", required=True)

    skill_list = skill_commands.add_parser("list", help="list visible Skills")
    skill_list.add_argument("--agent", help="evaluate permissions as this Agent")
    skill_list.add_argument("--json", action="store_true", help="emit JSON")

    skill_show = skill_commands.add_parser("show", help="read one visible Skill")
    skill_show.add_argument("name")
    skill_show.add_argument("--resource", help="read one declared Skill resource")
    skill_show.add_argument("--agent", help="evaluate permissions as this Agent")
    skill_show.add_argument(
        "--approve",
        action="store_true",
        help="approve an ask decision for this manual invocation",
    )
    skill_show.add_argument("--json", action="store_true", help="emit JSON")

    skill_search = skill_commands.add_parser("search", help="evaluate Skill triggers")
    skill_search.add_argument("query")
    skill_search.add_argument("--agent", help="evaluate permissions as this Agent")
    skill_search.add_argument("--limit", type=int, default=5)
    skill_search.add_argument("--json", action="store_true", help="emit JSON")
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
        if args.command == "skill":
            return _handle_skill(args, manager)
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


def _handle_skill(args: argparse.Namespace, manager: PluginManager) -> int:
    config = AppConfig.load(args.config)
    skills = _load_visible_skill_plugins(manager, config)
    agent = args.agent or config.skill_agent
    gateway = SkillGateway(
        skills,
        max_content_bytes=config.skill_max_content_bytes,
        max_resource_bytes=config.skill_max_resource_bytes,
        max_resource_total_bytes=config.skill_max_resource_total_bytes,
        max_listing_bytes=config.skill_max_listing_bytes,
        policy=SkillPermissionPolicy(
            global_rules=config.skill_permissions,
            agent_rules=config.skill_agent_permissions,
            agent=agent,
        ),
        name_only=config.skill_name_only,
    )

    if args.skill_command == "list":
        entries = gateway.entries()
        if args.json:
            print(
                json.dumps(
                    [
                        {
                            "name": entry.name,
                            "description": entry.description,
                            "whenToUse": entry.when_to_use,
                            "tags": list(entry.tags),
                            "compatibility": entry.compatibility,
                            "priority": entry.priority,
                            "listing": entry.listing,
                            "access": gateway.permission(
                                entry.name,
                                agent=agent,
                            ).access,
                            "usage": asdict(gateway.usage(entry.name)),
                        }
                        for entry in entries
                    ],
                    ensure_ascii=False,
                    indent=2,
                )
            )
            return 0
        for entry in entries:
            decision = gateway.permission(entry.name, agent=agent)
            print(
                f"{entry.name}\t{entry.priority}\t{entry.listing}\t"
                f"{decision.access}\t{entry.description}"
            )
        return 0

    if args.skill_command == "search":
        results = gateway.evaluate_triggers(args.query, limit=args.limit)
        if args.json:
            print(
                json.dumps(
                    [
                        {
                            "name": result.name,
                            "score": result.score,
                            "matchedTerms": list(result.matched_terms),
                            "matchedFields": list(result.matched_fields),
                        }
                        for result in results
                    ],
                    ensure_ascii=False,
                    indent=2,
                )
            )
            return 0
        if not results:
            print("no matching Skills")
            return 0
        for result in results:
            print(f"{result.name}\t{result.score:.2f}\t{','.join(result.matched_fields)}")
        return 0

    gateway.record_request(args.name)
    try:
        decision = gateway.permission(args.name, agent=agent)
        if decision.access == "deny":
            gateway.record_denied(args.name)
            raise ValueError(f"Skill not found or denied: {args.name}")
        if decision.access == "ask" and not args.approve:
            gateway.record_approval_required(args.name)
            gateway.record_approval_denied(args.name)
            raise ValueError(f"Skill {args.name} requires --approve for this manual invocation")
        if decision.access == "ask":
            gateway.record_approval_required(args.name)
            gateway.record_approval_granted(args.name)

        if args.resource:
            content = gateway.get_resource(
                args.name,
                args.resource,
                approved=args.approve,
                agent=agent,
            )
        else:
            content = gateway.get(
                args.name,
                approved=args.approve,
                agent=agent,
            )
    except (KeyError, PermissionError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 1

    if args.json:
        print(
            json.dumps(
                {
                    "name": args.name,
                    "resource": args.resource,
                    "access": decision.access,
                    "content": content,
                    "usage": asdict(gateway.usage(args.name)),
                },
                ensure_ascii=False,
                indent=2,
            )
        )
    else:
        print(content)
    return 0


def _load_visible_skill_plugins(
    manager: PluginManager,
    config: AppConfig,
) -> list[SkillPlugin]:
    roots: list[Path] = [manager.builtin_dir]
    roots.extend(manager.plugin_path(record.name) for record in manager.enabled_records())
    return [
        load_skill_plugin(contribution.manifest, config=config)
        for contribution in discover_contributions(roots)
        if contribution.kind == "skill"
    ]


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
