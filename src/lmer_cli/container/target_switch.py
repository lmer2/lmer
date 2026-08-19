"""Retarget a group session's ``target-exec`` at another member (issue #312).

``target-exec`` reads its container out of its own environment, so switching
cannot be an environment change: every shell, agent and wrapper already running
would keep the old value. The switch is therefore a **pointer file** that the
wrappers read on each invocation — ``bin/target-exec`` and ``bin/target-logs``
prefer it and fall back to ``LMER_SERVICE_CONTAINER`` when it is absent, which
is what leaves single-service sessions behaving exactly as before.

Membership is re-read from the runtime on every call rather than captured at
launch, so a member that restarted under a new container id is still reachable
and a member that is down is simply not offered.
"""

import os
import sys
from pathlib import Path

from ..service import ServiceError, ServiceMember, member_of, resolve_group

#: Inside the session container the host's runtime socket is always mounted at
#: the docker path and the image ships ``docker-ce-cli``, whichever runtime runs
#: on the host (docs/SERVICE-MODE.md, "Runtime Compatibility"). ``target-exec``
#: calls the same CLI for the same reason; this is the container's one runtime,
#: not a preference between two.
CONTAINER_RUNTIME_CLI = "docker"

#: Where the current target is recorded. Overridable with
#: ``LMER_SERVICE_TARGET_FILE``, which the host CLI sets for a group session.
DEFAULT_TARGET_FILE = "/home/developer/.lmer-session/service-target"

#: The pointer file: service name, container id, workdir — one per line, in
#: that order. Fixed positions rather than ``KEY=value``, because the readers
#: are shell scripts and the safe way for a shell to read a file is not to
#: source it.
_FIELDS = 3


def target_file_path() -> Path:
    """The pointer file this session uses."""
    return Path(
        os.environ.get("LMER_SERVICE_TARGET_FILE") or DEFAULT_TARGET_FILE
    )


def read_target(path: Path) -> tuple[str, str, str] | None:
    """``(service, container_id, workdir)`` from *path*, or ``None``.

    ``None`` covers both "no file" and "not readable/complete" — the caller
    treats them alike, since neither is a target the agent chose.
    """
    try:
        lines = path.read_text().splitlines()
    except OSError:
        return None
    if len(lines) < _FIELDS:
        return None
    service, container, workdir = (line.strip() for line in lines[:_FIELDS])
    if not service or not container:
        return None
    return service, container, workdir or "/"


def write_target(path: Path, member: ServiceMember) -> None:
    """Record *member* as the current target, atomically.

    ``os.replace`` over a sibling temp file: a ``target-exec`` reading the file
    while a ``target-switch`` writes it must see one target or the other, never
    half of each.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.parent / f"{path.name}.tmp.{os.getpid()}"
    tmp.write_text(
        f"{member.service}\n{member.container_id}\n{member.workdir}\n"
    )
    os.replace(tmp, path)


def _describe(service: str, container: str, workdir: str) -> str:
    return f"{service} → {container[:12]} in {workdir}"


def _current(path: Path) -> tuple[str, str, str] | None:
    """The target in force right now: the pointer file, else the launch env."""
    recorded = read_target(path)
    if recorded is not None:
        return recorded
    container = os.environ.get("LMER_SERVICE_CONTAINER")
    if not container:
        return None
    return (
        os.environ.get("LMER_SERVICE_NAME") or "?",
        container,
        os.environ.get("LMER_SERVICE_WORKDIR") or "/",
    )


def _print_listing(group: str, members: list[ServiceMember], path: Path) -> None:
    current = _current(path)
    print(f"group:   {group} ({len(members)} running containers)")
    if current is None:
        print("current: none selected yet")
    else:
        print(f"current: {_describe(*current)}")
    print("members:")
    # Marked by container id, not by name: the replicas of a scaled service
    # share one service name, so a name comparison marks every sibling as the
    # current target when only one of them is. The id is what the pointer file
    # records to exec into, so it is also what identifies the row.
    selected_any = False
    for member in members:
        selected = current is not None and member.container_id == current[1]
        selected_any = selected_any or selected
        print(f"  {'*' if selected else ' '} {member.service:<24} {member.container_name}")
    if current is not None and not selected_any:
        # Nothing matched, so the recorded container is no longer among the
        # running ones — usually a recreate. Said out loud, because an unmarked
        # listing otherwise looks like the marker is broken.
        print(f"\nthe current target ({current[0]}, {current[1][:12]}) is no "
              "longer running — switch again to pick up its replacement")
    print("\nswitch with: target-switch <service>")
    if len(members) != len({member.service for member in members}):
        # A scaled service names more than one container, so its own name is
        # ambiguous; the replica's container name is what selects one.
        print("  (a scaled service: name a container from the right-hand "
              "column to pick one replica)")


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] in ("-h", "--help"):
        print(__doc__.strip().splitlines()[0])
        print("\nUsage:\n  target-switch              list the group and the current target"
              "\n  target-switch <service>    point target-exec/target-logs at <service>")
        return 0
    if len(argv) > 1:
        print("❌ target-switch: expected at most one service name", file=sys.stderr)
        return 2

    group = (os.environ.get("LMER_SERVICE_GROUP") or "").strip()
    if not group:
        print(
            "❌ target-switch: this session is not attached to a service group",
            file=sys.stderr,
        )
        print(
            "   Launch with --service-group <compose-project> to attach to one",
            file=sys.stderr,
        )
        return 2

    try:
        members = resolve_group(CONTAINER_RUNTIME_CLI, group, announce=False)
    except ServiceError as exc:
        print(f"❌ target-switch: {exc}", file=sys.stderr)
        return 2

    path = target_file_path()
    if not argv:
        _print_listing(group, members, path)
        return 0

    try:
        member = member_of(members, argv[0])
    except ServiceError as exc:
        print(f"❌ target-switch: {exc}", file=sys.stderr)
        return 2

    try:
        write_target(path, member)
    except OSError as exc:
        print(f"❌ target-switch: could not write {path}: {exc}", file=sys.stderr)
        return 2

    print(
        f"✅ target-exec and target-logs now run against "
        f"{_describe(member.service, member.container_id, member.workdir)}"
    )
    return 0


if __name__ == "__main__":  # pragma: no cover - module is run via bin/target-switch
    sys.exit(main())
