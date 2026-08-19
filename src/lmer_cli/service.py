"""
Service mode: resolve running Docker containers for target-exec.

This module handles finding and inspecting running containers so lmer
can use `docker exec` to run commands inside a project's dev environment.
"""

import json
import subprocess
import sys
from collections import Counter
from dataclasses import dataclass

#: Compose's own labels. A service group is *read* from the project label rather
#: than declared anywhere (issue #312): a stack that is already up has named its
#: own members, and asking an operator to restate twelve of them is the thing
#: groups exist to avoid.
COMPOSE_PROJECT_LABEL = "com.docker.compose.project"
COMPOSE_SERVICE_LABEL = "com.docker.compose.service"


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


def resolve_container(
    runtime: str, service_name: str, *, announce: bool = True
) -> str:
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
        announce: Whether a successful resolution prints its line to stderr.
            True for the interactive path, where the line confirms that
            `--service` found what the user meant. False for a polled caller —
            `lmer_platform.slots` probes every slot on a schedule, and a success
            line per slot per poll would bury the daemon's log. A flag rather
            than a second resolver, so the polled answer cannot drift from the
            interactive one.

    Returns:
        Container ID of the running container

    Raises:
        ServiceError: If no unambiguous match is found
    """
    # 1. Compose service label
    label_matches = _docker_ps(
        runtime, f"label={COMPOSE_SERVICE_LABEL}={service_name}"
    )
    if len(label_matches) == 1:
        cid, name = label_matches[0]
        if announce:
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
        if announce:
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


# --- service groups (issue #312) ---------------------------------------------


@dataclass(frozen=True)
class ServiceMember:
    """One running container of a service group.

    ``service`` is the container's compose service name — stable across a scale
    change, because it is what the stack declared rather than a function of how
    many siblings happen to be up. A container with no service label carries its
    container name here instead. ``container_name`` always names this one
    container, so it is how a replica is addressed.
    """

    service: str
    container_id: str
    container_name: str
    workdir: str


#: Field spellings for the container id in an ``inspect --format`` template,
#: tried in this order. Docker accepts both (measured against its CLI); the two
#: runtimes resolve template keys differently in principle — one against the
#: JSON key, one against the Go struct field — so rather than betting on which,
#: a template that yields no usable row is retried with the other spelling. That
#: turns a runtime-specific field name from a total failure into one wasted
#: call, and is why no caller has to know which runtime it is talking to.
_ID_FIELDS = ("{{.ID}}", "{{.Id}}")


def _inspect_all(runtime: str, container_ids: list[str]) -> list[dict]:
    """``inspect`` every id at once → ``[{id, name, workdir, labels}, …]``.

    One call whatever the member count (two only if the first id spelling yields
    nothing — see :data:`_ID_FIELDS`).

    Two deliberate tolerances, both because a group is a live stack:

    - the labels come back as ``json`` rather than through ``{{index …}}``,
      which renders ``<no value>`` or fails outright for a container with no
      labels;
    - a **partial** result is used. ``inspect a-gone b-present`` prints the
      present row, complains about the missing one on stderr and exits
      non-zero; the containers this walks came from a separate ``ps`` a moment
      earlier, so one of them being recreated in between is ordinary. Only a
      call that yields no usable row at all is an error.
    """
    if not container_ids:
        return []

    last_error = ""
    for id_field in _ID_FIELDS:
        fmt = f'{id_field}\t{{{{.Name}}}}\t{{{{.Config.WorkingDir}}}}\t{{{{json .Config.Labels}}}}'
        try:
            result = subprocess.run(
                [runtime, "inspect", *container_ids, "--format", fmt],
                capture_output=True, text=True, timeout=20,
            )
        except (subprocess.SubprocessError, FileNotFoundError) as e:
            raise ServiceError(f"Failed to inspect containers with {runtime}: {e}")

        records = _parse_inspect(result.stdout)
        if records:
            return records
        last_error = (result.stderr or "").strip() or (
            f"{runtime} inspect produced no usable rows for {fmt}"
        )

    raise ServiceError(f"{runtime} inspect failed: {last_error}")


def _parse_inspect(stdout: str) -> list[dict]:
    """The rows of an ``inspect --format`` run that are usable.

    A row whose id is empty is dropped rather than trusted: that is what a
    runtime renders when it does not know the field, and a member with no
    container id could not be exec'd into anyway.
    """
    records: list[dict] = []
    for line in stdout.strip().splitlines():
        # maxsplit, so a tab inside the JSON labels cannot truncate them.
        parts = line.split("\t", 3)
        if len(parts) < 4:
            continue
        container_id, name, workdir, labels_json = (part.strip() for part in parts)
        if not container_id or container_id == "<no value>":
            continue
        try:
            labels = json.loads(labels_json)
        except (TypeError, ValueError):
            labels = {}
        if not isinstance(labels, dict):
            labels = {}
        records.append({
            "id": container_id,
            # podman reports the name bare, docker with a leading slash.
            "name": name.lstrip("/"),
            "workdir": workdir or "/",
            "labels": labels,
        })
    return records


def _running_projects(runtime: str) -> list[str]:
    """Every compose project with at least one running container."""
    try:
        result = subprocess.run(
            [runtime, "ps", "--format", "{{.ID}}"],
            capture_output=True, text=True, timeout=10,
        )
        ids = [i.strip() for i in result.stdout.strip().splitlines() if i.strip()]
        found = {
            record["labels"].get(COMPOSE_PROJECT_LABEL)
            for record in _inspect_all(runtime, ids)
        }
    except Exception:
        return []
    return sorted(project for project in found if project)


def resolve_group(
    runtime: str, project: str, *, announce: bool = True
) -> list[ServiceMember]:
    """Every running member of compose project *project*.

    Sorted by service name then container name, so every listing an operator or
    an agent sees — the launch announcement, ``target-switch``, a refusal — is in
    one order.

    Members are keyed by their compose service name whatever the replica count:
    a name that changed when a sibling stopped would move under a running
    session, and would not match what a group session recorded as the services
    it holds. A scaled service therefore appears once per replica under one
    service name, and :func:`member_of` refuses that name as ambiguous while
    accepting either replica's container name.

    Args:
        runtime: Container runtime ('docker' or 'podman')
        project: Compose project name — the group
        announce: Whether a successful resolution prints its line to stderr,
            for the same reason :func:`resolve_container` takes the flag.

    Raises:
        ServiceError: when the project has no running containers.
    """
    ids = [cid for cid, _ in _docker_ps(
        runtime, f"label={COMPOSE_PROJECT_LABEL}={project}"
    )]
    members = [
        ServiceMember(
            service=record["labels"].get(COMPOSE_SERVICE_LABEL) or record["name"],
            container_id=record["id"],
            container_name=record["name"],
            workdir=record["workdir"],
        )
        for record in _inspect_all(runtime, ids)
    ]
    if not members:
        msg = (
            f"No running containers in compose project {project!r} "
            f"(matched on the {COMPOSE_PROJECT_LABEL} label)"
        )
        projects = _running_projects(runtime)
        if projects:
            msg += f"\n   Running compose projects: {', '.join(projects)}"
        else:
            msg += "\n   No compose project has a running container"
        raise ServiceError(msg)

    members.sort(key=lambda member: (member.service, member.container_name))
    if announce:
        print(
            f"✅ Resolved service group '{project}' → {len(members)} containers: "
            f"{', '.join(describe_members(members))}",
            file=sys.stderr,
        )
    return members


def describe_members(members: list[ServiceMember]) -> list[str]:
    """One entry per service, replicas named — the listing every caller shows."""
    counts = Counter(member.service for member in members)
    out: list[str] = []
    for service, count in sorted(counts.items()):
        if count == 1:
            out.append(service)
            continue
        replicas = ", ".join(
            member.container_name for member in members
            if member.service == service
        )
        out.append(f"{service} ({count} replicas: {replicas})")
    return out


def member_names(members: list[ServiceMember]) -> list[str]:
    """Every name these containers can be addressed by, service names first.

    Both spellings, because both reach the same container: a slot's preset may
    name a compose service or an exact container name (:func:`resolve_container`
    takes either), so a group session holding these containers has to be
    understood as holding both spellings of each.
    """
    services = sorted({member.service for member in members})
    names = sorted(
        {member.container_name for member in members} - set(services)
    )
    return services + names


def member_of(members: list[ServiceMember], service: str) -> ServiceMember:
    """The member named *service*, or a :class:`ServiceError` listing the group.

    Accepts a compose service name, or a container name for a replica of a
    scaled service. A service name carrying more than one container is refused
    rather than resolved to one of them — the same answer
    :func:`resolve_container` gives for the same ambiguity, and for the same
    reason: nobody asked for *that* replica.

    Membership is checked against the resolved group rather than by resolving
    the name on its own: ``--service`` naming a container outside the group
    would otherwise start a session whose first ``target-switch`` moves it
    somewhere it can never move back to.
    """
    by_service = [member for member in members if member.service == service]
    if len(by_service) == 1:
        return by_service[0]
    if len(by_service) > 1:
        replicas = ", ".join(member.container_name for member in by_service)
        raise ServiceError(
            f"Service {service!r} has {len(by_service)} running replicas — name "
            f"one of them instead: {replicas}"
        )

    for member in members:
        if member.container_name == service:
            return member

    raise ServiceError(
        f"Service {service!r} is not a running member of this group — "
        f"members are: {', '.join(describe_members(members))}"
    )
