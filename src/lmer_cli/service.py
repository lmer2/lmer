"""
Service mode: resolve running Docker containers for target-exec.

This module handles finding and inspecting running containers so lmer
can use `docker exec` to run commands inside a project's dev environment.
"""

import subprocess
import sys


class ServiceError(Exception):
    """Exception raised when service resolution fails."""
    pass


def _docker_ps(runtime: str, filter_arg: str) -> list[tuple[str, str]]:
    """
    Run `runtime ps --filter <filter_arg>` and return [(id, name), ...].

    Raises ServiceError on runtime invocation failure or non-zero exit.
    """
    try:
        result = subprocess.run(
            [runtime, "ps", "--filter", filter_arg, "--format", "{{.ID}}\t{{.Names}}"],
            capture_output=True, text=True, timeout=10,
        )
    except (subprocess.SubprocessError, FileNotFoundError) as e:
        raise ServiceError(f"Failed to query {runtime}: {e}")

    if result.returncode != 0:
        raise ServiceError(f"{runtime} ps failed: {result.stderr.strip()}")

    out: list[tuple[str, str]] = []
    for line in result.stdout.strip().splitlines():
        line = line.strip()
        if not line:
            continue
        if "\t" in line:
            cid, name = line.split("\t", 1)
        else:
            cid, name = line, ""
        out.append((cid, name))
    return out


def resolve_container(runtime: str, service_name: str) -> str:
    """
    Find a running container matching the given service name.

    Match strategy (no fuzzy/substring matching — the input must be exact):
    1. Compose service label `com.docker.compose.service=<service_name>`.
    2. Exact container name `<service_name>`.

    Docker's `--filter name=` is a *substring* match, so this function
    post-filters the results to enforce an exact-name match. This is what
    keeps `--service myapp` from accidentally selecting
    `myappdev-database-1`.

    Args:
        runtime: Container runtime ('docker' or 'podman')
        service_name: Compose service name OR exact container name

    Returns:
        Container ID of the running container

    Raises:
        ServiceError: If no unambiguous match is found
    """
    # 1. Compose service label
    label_matches = _docker_ps(
        runtime, f"label=com.docker.compose.service={service_name}"
    )
    if len(label_matches) == 1:
        cid, name = label_matches[0]
        print(
            f"✅ Resolved service '{service_name}' → container {name} "
            f"({cid[:12]}) [compose label]",
            file=sys.stderr,
        )
        return cid
    if len(label_matches) > 1:
        names = ", ".join(n for _, n in label_matches)
        raise ServiceError(
            f"Multiple containers share compose service label '{service_name}': "
            f"{names}. Pass an exact container name to disambiguate."
        )

    # 2. Exact container name. Docker's name filter is a substring match,
    # so we ask for it and then keep only true-equal matches.
    name_matches = [
        (cid, name)
        for cid, name in _docker_ps(runtime, f"name={service_name}")
        if name == service_name
    ]
    if len(name_matches) == 1:
        cid, name = name_matches[0]
        print(
            f"✅ Resolved service '{service_name}' → container {name} ({cid[:12]})",
            file=sys.stderr,
        )
        return cid

    # No match — list running containers for the error message.
    try:
        result = subprocess.run(
            [runtime, "ps", "--format", "{{.Names}}"],
            capture_output=True, text=True, timeout=10,
        )
        running = [n.strip() for n in result.stdout.strip().splitlines() if n.strip()]
    except Exception:
        running = []

    msg = (
        f"No running container matched '{service_name}' "
        f"(checked compose service label and exact container name)"
    )
    if running:
        msg += f"\n   Running containers: {', '.join(running)}"
    else:
        msg += "\n   No containers are currently running"
    raise ServiceError(msg)


def inspect_container_workdir(runtime: str, container_id: str) -> str:
    """
    Get the working directory configured in a container.

    Args:
        runtime: Container runtime ('docker' or 'podman')
        container_id: Container ID to inspect

    Returns:
        Working directory path inside the container, or '/' as fallback
    """
    try:
        result = subprocess.run(
            [runtime, "inspect", container_id, "--format", "{{.Config.WorkingDir}}"],
            capture_output=True, text=True, timeout=10,
        )
        if result.returncode == 0 and result.stdout.strip():
            workdir = result.stdout.strip()
            print(f"✅ Target container workdir: {workdir}", file=sys.stderr)
            return workdir
    except Exception:
        pass

    print("⚠️  Could not determine target container workdir, using /", file=sys.stderr)
    return "/"
