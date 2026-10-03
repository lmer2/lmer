"""Freeze Git transport configuration for an authorized leased push."""
from contextlib import contextmanager
import os
from pathlib import Path
import re
import subprocess
import tempfile
from urllib.parse import urlsplit

from .push_allow import split_target


class LeaseError(ValueError):
    """A leased push cannot safely proceed."""


def git(*args: str, allowed=(0,), env=None) -> str:
    if env is None and args[0] == "config":
        env = {key: value for key, value in os.environ.items() if key != "GIT_CONFIG"}
    try:
        result = subprocess.run(["git", *args], capture_output=True, text=True,
                                timeout=30, check=False, env=env)
    except (OSError, subprocess.SubprocessError, ValueError) as exc:
        raise LeaseError(f"git {args[0]} could not complete; diagnostic output withheld") from exc
    if result.returncode not in allowed:
        raise LeaseError(f"git {args[0]} failed ({result.returncode}); diagnostic output withheld")
    return result.stdout.strip()


def _quoted(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"').replace(
        "\n", "\\n").replace("\t", "\\t").replace("\b", "\\b") + '"'


def _entry(key: str, value: str) -> str:
    section, name = key.rsplit(".", 1)
    section, sep, subsection = section.partition(".")
    header = section + (" " + _quoted(subsection) if sep else "")
    return f"[{header}]\n\t{name} = {_quoted(value)}\n"


def _config_layers(remote: str, by_url: bool, env=None):
    records = git("config", "--null", "--list", "--includes", "--show-scope",
                  env=env).split("\0")
    if records.pop() != "" or len(records) % 2:
        raise LeaseError("Cannot capture Git configuration")
    layers = {scope: [] for scope in ("system", "global", "local", "worktree", "command")}
    for scope, entry in zip(records[::2], records[1::2]):
        key, sep, value = entry.partition("\n")
        if scope not in layers:
            raise LeaseError("Unsupported Git configuration scope")
        if (key.startswith(("include.", "includeif.", "url.")) or
                (by_url and key.startswith("remote."))):
            continue
        selector = key.removeprefix(f"remote.{remote}.")
        if selector in {"receivepack", "uploadpack", "vcs", "serveroption"}:
            raise LeaseError(f"Leased transport does not support remote {selector}")
        layers[scope].append((key, value if sep else "true"))
    return layers


@contextmanager
def frozen_transport(remote: str, url: str, by_url: bool, *, env=None):
    """Use captured config and source storage, never live config at push time."""
    inherited = os.environ if env is None else env
    if re.match(r"^[^/:]+::", url) or ("://" in url and url.split(":", 1)[0] not in
            {"file", "git", "ssh", "git+ssh", "ssh+git", "http", "https"}):
        raise LeaseError("Leased transport does not support custom URL helpers")
    for selector in ("GIT_OVERRIDE_VIRTUAL_HOST", "GIT_NAMESPACE"):
        if selector in inherited:
            raise LeaseError(f"Leased transport does not support {selector}")
    if git("rev-parse", "--show-ref-format", env=env) != "files":
        raise LeaseError("Leased pushes require the files reference backend")
    layers = _config_layers(remote, by_url, env=env)
    worktree = git("rev-parse", "--show-toplevel", env=env)
    paths = {name: git("rev-parse", "--path-format=absolute", "--git-path", name, env=env)
             for name in ("HEAD", "refs", "objects", "packed-refs", "index", "shallow", "hooks")}
    with tempfile.TemporaryDirectory(prefix="gate-push-") as directory:
        root = Path(directory)
        for name, path in paths.items():
            if Path(path).exists():
                if name == "HEAD":
                    (root / name).write_bytes(Path(path).read_bytes())
                else:
                    (root / name).symlink_to(path)
        env = {key: value for key, value in inherited.items()
               if not key.startswith("GIT_CONFIG") and key not in
               {"GIT_DIR", "GIT_COMMON_DIR", "GIT_WORK_TREE"}}
        env.update(GIT_DIR=directory, GIT_WORK_TREE=worktree,
                   GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_SYSTEM=os.devnull,
                   GIT_CONFIG_GLOBAL=str(root / "global"))
        for filename, scopes in (("global", ("system", "global")),
                                 ("config", ("local", "worktree"))):
            (root / filename).write_text("".join(
                _entry(key, value) for scope in scopes for key, value in layers[scope]))
        overrides = [("core.worktree", worktree), ("core.hooksPath", paths["hooks"]),
                     ("extensions.worktreeConfig", "false")]
        if not by_url:
            overrides += [(f"remote.{remote}.url", ""), (f"remote.{remote}.url", url),
                          (f"remote.{remote}.pushurl", ""), (f"remote.{remote}.pushurl", url),
                          (f"remote.{remote}.mirror", "false")]
        commands = layers["command"] + overrides
        env["GIT_CONFIG_COUNT"] = str(len(commands))
        for index, (key, value) in enumerate(commands):
            env[f"GIT_CONFIG_KEY_{index}"] = key
            env[f"GIT_CONFIG_VALUE_{index}"] = value
        if not by_url and git("remote", "get-url", "--push", "--all", remote,
                              env=env).splitlines() != [url]:
            raise LeaseError("This Git/configuration cannot pin a single push URL")
        if git("ls-remote", "--get-url", "--", remote, env=env) != url:
            raise LeaseError("Cannot pin the authorized destination")
        yield env


def _endpoint_label(url: str) -> str:
    if "://" in url:
        try:
            parsed = urlsplit(url)
            host = parsed.hostname
            if host:
                host = f"[{host}]" if ":" in host else host
                port = f":{parsed.port}" if parsed.port is not None else ""
                return f"{parsed.scheme}://{host}{port}"
        except ValueError:
            pass
    elif re.match(r"^(?:[^/@]*@)?(?:\[[^\]]+\]|[^/:]+):", url):
        host, _ = split_target(url)
        if host:
            return "ssh://" + (f"[{host}]" if ":" in host else host)
    return "endpoint details withheld"


def inspect_expectation(remote: str, destination: str) -> tuple[str, str]:
    """Return only transport/host/port and the branch OID; capture Git output."""
    env = {key: value for key, value in os.environ.items()
           if not key.startswith("GIT_TRACE") and key not in {"GIT_CURL_VERBOSE", "GIT_CONFIG"}}
    # Explicit zero overrides configuration-backed Trace2 destinations too.
    env.update(GIT_TRACE2="0", GIT_TRACE2_EVENT="0", GIT_TRACE2_PERF="0")
    if not destination.startswith("refs/heads/"):
        raise LeaseError("Inspection requires refs/heads/<branch>")
    git("check-ref-format", destination, env=env)
    if not remote or remote.startswith("-") or any(c.isspace() for c in remote):
        raise LeaseError("Inspection requires one remote name")
    urls = git("remote", "get-url", "--push", "--all", remote, env=env).splitlines()
    if len(urls) != 1:
        raise LeaseError("Inspection requires exactly one push URL")
    with frozen_transport(remote, urls[0], False, env=env) as transport_env:
        advertised = git("ls-remote", "--symref", "--", remote, destination, env=transport_env)
    lines = advertised.splitlines()
    if len(lines) != 1 or lines[0].startswith("ref: "):
        raise LeaseError("Inspection requires one existing, nonsymbolic branch")
    fields = lines[0].split("\t", 1)
    if len(fields) != 2:
        raise LeaseError("Unexpected branch advertisement")
    oid, ref = fields
    if (ref != destination or len(oid) not in (40, 64) or set(oid) == {"0"} or
            any(c not in "0123456789abcdef" for c in oid)):
        raise LeaseError("Unexpected branch advertisement")
    return _endpoint_label(urls[0]), oid
