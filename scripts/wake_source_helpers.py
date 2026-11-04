"""Private publisher capabilities, statically enumerable by daemon fingerprints."""

from pathlib import Path


def restore_transport(module, context):
    from ai_chassis.wake.producers import source_command

    if "db_dir" in context:
        return module.DoltCliTransport(Path(context["db_dir"]), runner=source_command)
    endpoint = module.Endpoint(**context["endpoint"])
    return module.BdServerTransport(
        Path(context["repo"]),
        context["database"],
        endpoint=endpoint,
        runner=source_command,
    )


def register():
    # Call-time imports retain the disabled producer's original import boundary.
    from scripts import bd_dolt_publish
    from ai_chassis.wake import producers

    producers.register_publisher(bd_dolt_publish, restore_transport)
