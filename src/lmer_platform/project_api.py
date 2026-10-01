"""Request binding shared by HTTP actions and their threadpool execution."""
from contextlib import contextmanager
from contextvars import ContextVar
from functools import wraps
import inspect

from fastapi.routing import APIRoute

from . import project_context as scope, projects, runs
from . import peers, project_requests, assistant, config

_REQUEST = ContextVar("project_request", default=None)


class WorkspaceRoute(APIRoute):
    def __init__(self, path, endpoint, **kwargs):
        if not inspect.iscoroutinefunction(endpoint):
            original = endpoint
            success_status = kwargs.get("status_code") or 200

            @wraps(original)
            def endpoint(*args, **named):
                if not projects.enabled():
                    return original(*args, **named)
                binding = _REQUEST.get()
                if binding and binding["method"] in ("GET", "HEAD"):
                    recheck()
                    return original(*args, **named)
                with projects.operation():
                    recheck()
                    if binding and binding["request_id"] and receipted(binding["path"]):
                        return project_requests.action({**binding, "success_status": success_status}, lambda: original(*args, **named))
                    return original(*args, **named)

            annotations = inspect.get_annotations(original, eval_str=True)
            signature = inspect.signature(original)
            endpoint.__signature__ = signature.replace(parameters=[
                parameter.replace(annotation=annotations.get(name, parameter.annotation))
                for name, parameter in signature.parameters.items()
            ], return_annotation=annotations.get("return", signature.return_annotation))

        super().__init__(path, endpoint, **kwargs)


def _run(data):
    if all(data.get(key) for key in ("host", "project", "slug")):
        return runs.run_key(data["host"], data["project"], data["slug"])
    return None


def receipted(path):
    return path not in ("/api/assistant/pending", "/api/assistant/requests", "/api/comms/thread") and not (
        path.startswith("/api/assistant/requests/") and path.endswith(("/reply", "/park", "/release")))


@contextmanager
def bind(request, caller, body):
    if not projects.enabled():
        yield
        return
    query = dict(request.query_params)
    path = request.url.path
    parking = path.startswith("/api/assistant/requests/") and path.endswith(("/park", "/release"))
    if caller.kind != "operator" and parking:
        raise projects.ProjectError("Only an operator may release or park a conversation request.", status=403)
    if caller.kind != "operator" and path in ("/api/project-context", "/api/project-relay"):
        raise projects.ProjectError("Only an operator may explicitly share context between projects.", status=403)
    candidates = [value for value in (
        body.get("workspace_id"), query.get("workspace_id"),
        request.headers.get("x-lmer-workspace-id"), request.path_params.get("workspace_id"),
    ) if value]
    if any(not isinstance(value, str) for value in candidates):
        raise projects.ProjectError("Invalid project identity.")
    if len(set(candidates)) > 1:
        raise projects.ProjectError("Conflicting project destinations.", status=409)
    explicit = candidates[0] if candidates else None
    session = request.path_params.get("session_id")
    caller_home = projects.session_home(caller.session_id) if caller.kind == "assistant" else None
    peer = bool(caller_home and session and projects.session_home(session) != caller_home
                and path == f"/api/sessions/{session}/comms" and request.method == "POST")
    key = _run(body) or _run(query) or projects.session_run(session)
    if peer:
        peers.validate_body(body)
        peers.identify(session)
        key = None
    home = projects.run_home(key) if key else None
    owner = caller_home if peer else projects.session_home(session) if session else None
    request_id = body.get("request_id") or request.headers.get("x-lmer-request-id") or request.path_params.get("request_id")
    if request_id:
        requested_owner = project_requests.get(request_id)["project_id"]
        if owner and requested_owner != owner:
            raise projects.ProjectError("The request and action name different projects.", status=409)
        owner = requested_owner
    if home:
        if owner and owner != home["project_id"]:
            raise projects.ProjectError("Conflicting session and run destinations.", status=409)
        owner = home["project_id"]
    chosen = explicit or owner or caller_home
    if owner and chosen != owner or caller_home and chosen != caller_home:
        raise projects.ProjectError("This action belongs to another project.", code="project_changed", status=409)
    if caller.kind == "assistant" and not caller_home:
        raise projects.ProjectError("This assistant generation is no longer authorized.", status=401)
    unbound = path in {
        "/api/assistant/requests", "/api/projects", "/api/settings", "/api/health",
        "/api/state", "/api/rescan", "/api", "/", "/api/assistant/config",
        "/api/comms/feed", "/api/comms/questions", "/api/comms/connectors", "/api/spawn-options",
        "/api/prune",
        "/api/assistant/peers",
    } or path.startswith(("/assets/", "/api/slots"))
    operator_history_read = caller.kind == "operator" and request.method == "GET" and session and path in {
        f"/api/sessions/{session}/messages", f"/api/sessions/{session}/log", f"/api/sessions/{session}/ask",
    }
    if path.startswith("/api/") and not chosen and not unbound and not operator_history_read:
        raise projects.ProjectError("Choose a project for this request.", code="project_required")
    if chosen:
        projects.get(chosen, active=request.method not in ("GET", "HEAD") and not path.endswith("/restore"))
    if caller.kind != "operator" and (path.startswith("/api/projects") or path == "/api/settings"
                                       or path.startswith("/api/slots") or path in ("/api/runs/move", "/api/prune")) and request.method != "GET":
        raise projects.ProjectError("Only an operator may change platform or project settings.", status=403)
    expected = body.get("home_revision", query.get("home_revision"))
    if expected is not None:
        try:
            expected = int(expected)
        except (ValueError, TypeError) as exc:
            raise projects.ProjectError("Invalid project ownership revision.") from exc
    binding = {"workspace_id": chosen, "key": key, "session": session,
               "revision": expected if expected is not None else home["revision"] if home else None,
               "body": body, "path": path, "method": request.method, "request_id": request_id,
               "parking": parking,
               "peer": peer,
               "caller_session": caller.session_id if caller.kind == "assistant" else None}
    token = _REQUEST.set(binding)
    try:
        with scope.bound(chosen):
            recheck()
            yield
    finally:
        _REQUEST.reset(token)


def recheck():
    binding = _REQUEST.get()
    if not binding:
        return
    workspace_id = binding["workspace_id"]
    if binding["caller_session"]:
        credential = config.active_assistant_credential()
        owner = assistant.authenticated_generation(credential) if credential else None
        live = assistant._live_assistant()
        if not owner or owner["session"] != binding["caller_session"] or not live or live["id"] != binding["caller_session"]:
            raise projects.ProjectError("This assistant generation ended before the action ran.",
                                        code="assistant_generation_changed", status=409)
        if binding["method"] not in ("GET", "HEAD") and binding["path"] != "/api/assistant/pending":
            current = project_requests.active(workspace_id)
            if binding["request_id"] and current and binding["request_id"] != current:
                raise projects.ProjectError("Finish the active conversation request before acting on another.", status=409)
            binding["request_id"] = binding["request_id"] or current
            if current and binding["path"].endswith("/tty-ticket"):
                raise projects.ProjectError("Use request-bound HTTP input while handling a conversation request.", status=409)
    if binding["request_id"] and not binding.get("parking"):
        if binding["path"] == "/api/assistant/pending":
            row = project_requests.get(binding["request_id"])
            current = project_requests.active(workspace_id)
            if row["project_id"] != workspace_id or current and current != row["id"]:
                raise projects.ProjectError("Take pending for the active request in its recorded project.", status=409)
        else:
            project_requests.authorize(binding["request_id"], workspace_id)
    if binding.get("peer"):
        peers.identify(binding["session"])
    elif binding["session"] and projects.session_home(binding["session"]) != workspace_id:
        raise projects.ProjectError("This session's project changed. Reopen the run.", code="project_changed", status=409)
    if binding["key"]:
        home = projects.check_home(binding["key"], workspace_id, binding["revision"])
        if home and home["reconfirm"] and binding["path"].endswith(("/answer", "/resume")):
            body = binding["body"]
            if not body.get("confirm_project") or body.get("home_revision") != home["revision"]:
                raise projects.ProjectError("This run moved. Confirm its current project before continuing.",
                                            code="project_reconfirmation_required", status=409)



def capture():
    binding = _REQUEST.get()
    return dict(binding) if binding else {}


def invoke_captured(binding, function, *args, mutation=False, **kwargs):
    if not binding:
        return function(*args, **kwargs)
    token = _REQUEST.set(binding)
    try:
        with projects.operation(), scope.bound(binding["workspace_id"]):
            recheck()
            if mutation and binding["request_id"]:
                raise projects.ProjectError("Use request-bound HTTP input while handling a conversation request.", status=409)
            return function(*args, **kwargs)
    finally:
        _REQUEST.reset(token)


def annotate(payload, *, workspace_id=None):
    result = dict(payload)
    for collection in ("runs", "sessions", "attention"):
        if not isinstance(result.get(collection), list):
            continue
        rows = []
        for original in result[collection]:
            row = dict(original)
            key = _run(row)
            if not key and isinstance(row.get("run"), dict):
                key = _run(row["run"])
            home = projects.run_home(key) if key else None
            session = row.get("session")
            session_id = session.get("id") if isinstance(session, dict) else row.get("id") or session
            owner = home["project_id"] if home else projects.session_home(session_id)
            row["workspace_id"] = owner
            row["home_revision"] = home["revision"] if home else None
            row["confirm_project"] = bool(home and home["reconfirm"])
            if owner:
                project = projects.get(owner)
                row["workspace_name"], row["workspace_color"] = project["name"], project["color"]
            if workspace_id is None or owner == workspace_id:
                rows.append(row)
        result[collection] = rows
    if workspace_id is not None:
        if isinstance(result.get("tracked"), dict):
            tracked = annotate({"runs": result["tracked"].get("runs", [])}, workspace_id=workspace_id)["runs"]
            result["tracked"] = {"runs": tracked, "count": len(tracked)}
        if "relations" in result:
            result["relations"] = [edge for edge in result["relations"] if all(
                (projects.run_home(edge.get(end)) or {}).get("project_id") == workspace_id
                for end in ("a", "b")
            )]
        if "totals" in result:
            result["totals"] = {"runs": len(result.get("runs", [])),
                                "live": sum(bool(row.get("live")) for row in result.get("runs", [])),
                                "attention": len(result.get("attention", []))}
        if "counts" in result:
            counts = {}
            for row in result.get("runs", []):
                state = row.get("state")
                counts[state] = counts.get(state, 0) + 1
            result["counts"] = counts
    return result


def public_project(row):
    return {key: value for key, value in row.items() if key not in ("assistant_state", "reserved", "retry_at")}
