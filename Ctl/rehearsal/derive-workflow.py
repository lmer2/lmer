#!/usr/bin/env python3
"""derive-workflow.py — derive the rehearsal rig's release workflow from the
production one (see README.md in this directory; that document is the frozen
design, "Workflow derivation").

The production `.github/workflows/release.yml` is the single source of
truth; a checked-in rig copy would go stale silently. This script applies
the minimal enumerable transform (anything else passes through verbatim,
comments and formatting included):

  1. publish step: add `repository-url: https://test.pypi.org/legacy/` to
     the `pypa/gh-action-pypi-publish` step.
  2. environment: `pypi` -> the rehearsal environment (`testpypi`);
     environment url -> https://test.pypi.org/project/<rehearsal project>/.
  3. project-name references (environment url, GitHub Release step if any)
     -> the rehearsal project name.

Modes (workflow path defaults to this repo's production workflow):

  derive-workflow.py --check [--rig-workflow PATH] [workflow.yml]
      Drift guard. Exits non-zero, loudly, when the source workflow no
      longer has the expected shape: verify-tag-signature is the FIRST
      job; it reads the signer key from vars.RELEASE_ALLOWED_SIGNERS
      (never repo content); its checkout is pinned to ref: main (the
      verifier never comes from the tag's own tree); it asserts tag commit
      == GitHub main HEAD via the API; the PyPI publish step carries
      skip-existing: true and is preceded by the version-reuse gate (the
      PYPI_PROJECT_URL step the transform rewrites) whose script is
      checked out from main before the dist download; and the publish
      environment is the production pypi environment the transform
      rewrites. A plain --check also dry-runs the transform, so a green
      check implies --emit will succeed. With --rig-workflow (a local
      path, or '-' for stdin,
      holding the rig repo's committed release.yml — the rehearsal runners
      fetch it via the GitHub contents API), additionally re-derive from
      the production workflow and diff against that copy, exiting non-zero
      with a readable diff on any difference. A green full --check is
      required immediately before every rehearsal run.

  derive-workflow.py --emit [workflow.yml]
      Run --check, apply the transform, self-verify the result, and write
      the derived workflow to stdout.

Rig identity arrives via LMER_REHEARSAL_PROJECT and
LMER_REHEARSAL_ENVIRONMENT (rig.env; defaults match rig.env.example).
Production targets are refused, mirroring the lib.sh guard.

Stdlib + yaml only — this must run standalone in the rig.
"""
import argparse
import difflib
import os
import re
import sys
from pathlib import Path

import yaml

SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parent.parent
DEFAULT_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "release.yml"

VERIFY_JOB = "verify-tag-signature"
PUBLISH_ACTION = "pypa/gh-action-pypi-publish"
TESTPYPI_LEGACY = "https://test.pypi.org/legacy/"

# The signer allowlist must come from the admin-controlled Actions
# repository variable — never secrets, never repo content.
SIGNERS_VALUE_RE = re.compile(r"^\$\{\{\s*vars\.RELEASE_ALLOWED_SIGNERS\s*\}\}$")

# Marker for the tag-commit == GitHub-main-HEAD assertion (the REST API
# path), looked for in the verify job's inline run blocks and in any
# repo script the job invokes.
MAIN_HEAD_MARKER = "commits/heads/main"

# Marker for the EXPLICIT tag fetch. `fetch-tags: true` only leaves git's
# tag auto-following on, which brings a tag down when its object is
# reachable from the fetched refs — a tag off `main` may be absent, and
# verification would then exit at "tag not found" without reaching the
# main-HEAD comparison. Fails closed either way; the rule exists so the
# rehearsal's not-main-head case cannot silently prove the wrong branch.
TAG_FETCH_MARKER = "refs/tags/${GITHUB_REF_NAME}"

# Marker for the verified-tag == GITHUB_SHA pin: the build/publish jobs
# check out GITHUB_SHA, so a tag re-pointed between the push event and
# this job would split "what was verified" from "what gets published".
TRIGGER_SHA_MARKER = "GITHUB_SHA"

# Production environment url the transform rewrites; the production
# project name is read from it (never hard-coded here).
PROD_ENV_URL_RE = re.compile(r"^https://pypi\.org/project/(?P<name>[^/]+)/?$")


def fail(message):
    print(f"REFUSED: {message}", file=sys.stderr)
    return 1


# ---------------------------------------------------------------------------
# Workflow model helpers (yaml.safe_load; dict order preserves job order)
# ---------------------------------------------------------------------------

def load_workflow(path):
    """Parse a workflow file -> (doc, text). Loud failure on any problem."""
    try:
        text = path.read_text()
    except OSError as exc:
        raise WorkflowShapeError([f"cannot read workflow {path}: {exc}"])
    try:
        doc = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise WorkflowShapeError([f"workflow {path} is not valid YAML: {exc}"])
    if not isinstance(doc, dict) or not isinstance(doc.get("jobs"), dict):
        raise WorkflowShapeError([f"workflow {path} has no jobs mapping"])
    return doc, text


class WorkflowShapeError(Exception):
    def __init__(self, failures):
        super().__init__("; ".join(failures))
        self.failures = failures


def job_steps(job):
    steps = job.get("steps")
    return steps if isinstance(steps, list) else []


def find_publish(doc):
    """-> (job_name, job, step) for the pypa publish step, or None."""
    for name, job in doc["jobs"].items():
        if not isinstance(job, dict):
            continue
        for step in job_steps(job):
            uses = step.get("uses") if isinstance(step, dict) else None
            if isinstance(uses, str) and uses.split("@")[0] == PUBLISH_ACTION:
                return name, job, step
    return None


def verify_job_run_text(path, job):
    """All shell the verify job executes: inline run blocks plus the
    contents of any repo script they invoke (resolved against the repo
    root when the workflow lives at <root>/.github/workflows/)."""
    chunks = []
    root = None
    if path.parent.name == "workflows" and path.parent.parent.name == ".github":
        root = path.parent.parent.parent
    for step in job_steps(job):
        run = step.get("run") if isinstance(step, dict) else None
        if not isinstance(run, str):
            continue
        chunks.append(run)
        if root is None:
            continue
        for token in run.split():
            if ".github/scripts/" in token:
                script = root / token
                if script.is_file():
                    chunks.append(script.read_text())
    return "\n".join(chunks)


# ---------------------------------------------------------------------------
# --check: the drift guard
# ---------------------------------------------------------------------------

def shape_failures(path, doc):
    """Every way the workflow deviates from the expected shape (all
    reported at once, matching stand-up.sh's report-every-gap style)."""
    failures = []
    jobs = doc["jobs"]
    if not jobs:
        # An empty mapping would make next(iter(...)) below die with a raw
        # StopIteration instead of a drift-guard report.
        return [f"workflow {path} has an empty jobs mapping"]

    # 1. verify-tag-signature is the first job — nothing runs before the
    # publish gate.
    first = next(iter(jobs))
    if first != VERIFY_JOB:
        failures.append(
            f"first job is '{first}', expected '{VERIFY_JOB}' "
            f"(tag verification must gate everything)"
        )
    verify = jobs.get(VERIFY_JOB)
    if not isinstance(verify, dict):
        failures.append(f"job '{VERIFY_JOB}' is missing")
        verify = {}

    # 2. Signer allowlist comes from vars.RELEASE_ALLOWED_SIGNERS — never
    # repo content, never secrets.
    env_blocks = [verify.get("env")] + [
        step.get("env") for step in job_steps(verify) if isinstance(step, dict)
    ]
    signer_values = [
        env["RELEASE_ALLOWED_SIGNERS"]
        for env in env_blocks
        if isinstance(env, dict) and "RELEASE_ALLOWED_SIGNERS" in env
    ]
    if not signer_values:
        failures.append(
            f"'{VERIFY_JOB}' has no step with RELEASE_ALLOWED_SIGNERS in env"
        )
    for value in signer_values:
        if not (isinstance(value, str) and SIGNERS_VALUE_RE.match(value.strip())):
            failures.append(
                f"RELEASE_ALLOWED_SIGNERS is '{value}', expected "
                "'${{ vars.RELEASE_ALLOWED_SIGNERS }}' (admin-controlled "
                "Actions variable, never repo content)"
            )

    # 3. Tag commit == GitHub main HEAD, asserted via the REST API.
    verify_text = verify_job_run_text(path, verify) if verify else ""
    if verify and MAIN_HEAD_MARKER not in verify_text:
        failures.append(
            f"'{VERIFY_JOB}' never asserts tag commit == GitHub main HEAD "
            f"via the API (no '{MAIN_HEAD_MARKER}' in its run steps or the "
            "scripts they invoke)"
        )

    # 3a. The pushed tag is fetched EXPLICITLY, not left to `fetch-tags`'
    # auto-following (see TAG_FETCH_MARKER).
    if verify and TAG_FETCH_MARKER not in verify_text:
        failures.append(
            f"'{VERIFY_JOB}' never fetches the pushed tag explicitly (no "
            f"'{TAG_FETCH_MARKER}' refspec in its run steps): a tag whose "
            "commit is on no branch may be absent from the checkout, and "
            "verification would exit at 'tag not found' instead of the "
            "main-HEAD comparison"
        )

    # 3b. The verified tag is pinned to the triggering commit, so the tag
    # cannot move between the push event and this job.
    if verify and TRIGGER_SHA_MARKER not in verify_text:
        failures.append(
            f"'{VERIFY_JOB}' never pins the verified tag to "
            f"{TRIGGER_SHA_MARKER} (the commit build/publish check out), so "
            "a tag re-pointed after the push event would be verified as one "
            "commit and published as another"
        )

    # 3c. The verify job's checkout is pinned to `main`, never the pushed
    # tag's ref: the verification code must not come from the tree it is
    # verifying (whoever can push a tag would supply the verifier).
    checkout_refs = [
        (step.get("with") or {}).get("ref")
        for step in job_steps(verify)
        if isinstance(step, dict)
        and isinstance(step.get("uses"), str)
        and step["uses"].split("@")[0] == "actions/checkout"
    ]
    if verify and (not checkout_refs or any(r != "main" for r in checkout_refs)):
        failures.append(
            f"'{VERIFY_JOB}' checkout is not pinned to ref: main (the "
            "verification code must never come from the pushed tag's tree)"
        )

    # 4. Publish step exists and carries skip-existing: true (leg-2
    # re-entry converges instead of failing on "file already exists").
    publish = find_publish(doc)
    if publish is None:
        failures.append(f"no step uses {PUBLISH_ACTION}")
        return failures
    _, publish_job, publish_step = publish
    with_block = publish_step.get("with")
    if not (isinstance(with_block, dict) and with_block.get("skip-existing") is True):
        failures.append(f"{PUBLISH_ACTION} step does not set skip-existing: true")

    # 4b. The version-reuse gate precedes the publish step: it is what
    # turns skip-existing's silent convergence into an admin-authorized
    # act, and its PYPI_PROJECT_URL env var is a transform contract — the
    # https://pypi.org/project/<name>/ string the rig derivation rewrites.
    steps = job_steps(publish_job)
    # 4c. The gate's code is checked out from `main`, never the pushed tag:
    # the publish job holds id-token: write, so tag-borne content must not
    # run here (same rule as the verify job, rule 3b). The checkout must
    # also precede the artifact download — checkout cleans its workspace.
    publish_checkouts = [
        (i, step) for i, step in enumerate(steps)
        if isinstance(step, dict) and isinstance(step.get("uses"), str)
        and step["uses"].split("@")[0] == "actions/checkout"
    ]
    if not publish_checkouts:
        failures.append(
            "publish job has no checkout step (the version-reuse gate "
            "script must be fetched from main, never from the pushed tag)"
        )
    for _, step in publish_checkouts:
        if (step.get("with") or {}).get("ref") != "main":
            failures.append(
                "publish job checkout is not pinned to ref: main (the job "
                "holds id-token: write — its code must never come from the "
                "pushed tag's tree)"
            )
    download_positions = [
        i for i, step in enumerate(steps)
        if isinstance(step, dict) and isinstance(step.get("uses"), str)
        and step["uses"].split("@")[0] == "actions/download-artifact"
    ]
    if publish_checkouts and download_positions \
            and publish_checkouts[-1][0] > min(download_positions):
        failures.append(
            "publish job checks out after downloading the dist artifact — "
            "checkout cleans its workspace, which would delete dist/"
        )
    gate_positions = [
        i for i, step in enumerate(steps)
        if isinstance(step, dict)
        and isinstance((step.get("env") or {}).get("PYPI_PROJECT_URL"), str)
        and PROD_ENV_URL_RE.match(step["env"]["PYPI_PROJECT_URL"])
    ]
    publish_position = next(
        (i for i, step in enumerate(steps)
         if isinstance(step, dict) and isinstance(step.get("uses"), str)
         and step["uses"].split("@")[0] == PUBLISH_ACTION), None,
    )
    if not gate_positions or publish_position is None \
            or min(gate_positions) > publish_position:
        failures.append(
            "publish job has no version-reuse gate step (PYPI_PROJECT_URL "
            "env matching https://pypi.org/project/<name>/) before the "
            f"{PUBLISH_ACTION} step"
        )

    # 5. Publish environment is the production pypi environment the
    # transform rewrites (name: pypi, url: https://pypi.org/project/<name>/).
    environment = publish_job.get("environment")
    if not (isinstance(environment, dict) and environment.get("name") == "pypi"):
        failures.append("publish job environment.name is not 'pypi'")
    url = environment.get("url") if isinstance(environment, dict) else None
    if not (isinstance(url, str) and PROD_ENV_URL_RE.match(url)):
        failures.append(
            "publish job environment.url does not match "
            "https://pypi.org/project/<name>/"
        )
    return failures


def run_check(path, doc, ok_stream=sys.stdout):
    failures = shape_failures(path, doc)
    if failures:
        print(f"DRIFT GUARD FAILED: {path} no longer has the expected shape:",
              file=sys.stderr)
        for failure in failures:
            print(f"  - {failure}", file=sys.stderr)
        print("Do NOT run a rehearsal until this is resolved and --check is "
              "green.", file=sys.stderr)
        return 1
    print(f"drift guard OK: {path}", file=ok_stream)
    return 0


# ---------------------------------------------------------------------------
# --emit: the transform (text-level, so everything else — comments, SHA-pin
# rationale, formatting — passes through verbatim)
# ---------------------------------------------------------------------------

def located(iterator, description):
    """next(iterator), but a miss surfaces as a WorkflowShapeError instead
    of a raw StopIteration: the transform is text-level, so a YAML-valid
    workflow with unusual formatting (flow-style blocks, nonstandard job
    indentation) must fail loudly, not crash."""
    try:
        return next(iterator)
    except StopIteration:
        raise WorkflowShapeError(
            [f"transform could not locate {description} in the workflow "
             "text (unusual formatting? the transform is text-level and "
             "expects the production block layout)"]
        ) from None


def indent_of(line):
    return len(line) - len(line.lstrip(" "))


def block_end(lines, start, indent):
    """Index one past the last line of the block whose lines are indented
    deeper than `indent` (blank lines pass through)."""
    end = start
    for i in range(start, len(lines)):
        if lines[i].strip() and indent_of(lines[i]) <= indent:
            break
        end = i + 1
    return end


def transform(path, doc, text, project, environment):
    prod_name, _, publish_job_name = production_identity(doc)
    lines = text.splitlines()

    # Rule 1: repository-url on the publish step, inserted next to
    # skip-existing (guaranteed present by --check).
    uses_idx = located(
        (i for i, line in enumerate(lines)
         if PUBLISH_ACTION in line
         and line.lstrip().startswith(("uses:", "- uses:"))),
        f"the {PUBLISH_ACTION} uses: line",
    )
    uses_indent = indent_of(lines[uses_idx])
    # `- uses:` bullets carry the step's keys two columns deeper; a bare
    # `uses:` shares its indent with the rest of the step.
    step_indent = uses_indent + 1 if lines[uses_idx].lstrip().startswith("-") \
        else uses_indent - 1
    step_end = block_end(lines, uses_idx + 1, step_indent)
    skip_idx = located(
        (i for i in range(uses_idx + 1, step_end)
         if lines[i].strip().startswith("skip-existing:")),
        "the skip-existing: line in the publish step",
    )
    lines.insert(
        skip_idx + 1,
        " " * indent_of(lines[skip_idx]) + f"repository-url: {TESTPYPI_LEGACY}",
    )

    # Rules 2 + 3a: environment name (scoped to the environment block) and
    # every production-project-url occurrence in the publish job — the
    # environment url AND the version-reuse gate's PYPI_PROJECT_URL env var
    # carry the same https://pypi.org/project/<name>/ string by contract.
    job_idx = located(
        (i for i, line in enumerate(lines)
         if line.strip() == f"{publish_job_name}:" and indent_of(line) == 2),
        f"the '{publish_job_name}:' job line",
    )
    job_end = block_end(lines, job_idx + 1, indent_of(lines[job_idx]))
    env_idx = located(
        (i for i in range(job_idx + 1, job_end)
         if lines[i].strip() == "environment:"),
        f"the environment: block of the '{publish_job_name}' job",
    )
    env_end = block_end(lines, env_idx + 1, indent_of(lines[env_idx]))
    for i in range(env_idx + 1, env_end):
        stripped = lines[i].strip()
        pad = " " * indent_of(lines[i])
        if stripped == "name: pypi":
            lines[i] = f"{pad}name: {environment}"
    prod_url = f"https://pypi.org/project/{prod_name}/"
    test_url = f"https://test.pypi.org/project/{project}/"
    for i in range(job_idx + 1, job_end):
        if prod_url in lines[i]:
            lines[i] = lines[i].replace(prod_url, test_url)

    # Rule 3b: project-name references in the GitHub Release job, if any.
    # (?!-) so an already-suffixed name is never double-rewritten.
    name_re = re.compile(rf"\b{re.escape(prod_name)}\b(?!-)")
    for job_name in ("github-release",):
        for i, line in enumerate(lines):
            if line.strip() == f"{job_name}:" and indent_of(line) == 2:
                job_end = block_end(lines, i + 1, indent_of(line))
                for j in range(i + 1, job_end):
                    lines[j] = name_re.sub(project, lines[j])

    derived = "\n".join(lines) + "\n"
    self_verify(path, derived, project, environment, prod_name,
                publish_job_name)
    return derived


def production_identity(doc):
    """(production project name, environment url, publish job name), read
    from the workflow itself — never hard-coded."""
    job_name, publish_job, _ = find_publish(doc)
    url = publish_job["environment"]["url"]
    return PROD_ENV_URL_RE.match(url).group("name"), url, job_name


def self_verify(path, derived, project, environment, prod_name, publish_job):
    """The transform must have landed exactly; anything short of that is a
    bug here, reported loudly instead of shipped to the rig."""
    doc = yaml.safe_load(derived)
    _, job, step = find_publish(doc)
    problems = []
    if step["with"].get("repository-url") != TESTPYPI_LEGACY:
        problems.append("publish step lacks the TestPyPI repository-url")
    if step["with"].get("skip-existing") is not True:
        problems.append("skip-existing: true was lost")
    env = job.get("environment") or {}
    if env.get("name") != environment:
        problems.append(f"environment.name is not '{environment}'")
    if env.get("url") != f"https://test.pypi.org/project/{project}/":
        problems.append("environment.url is not the TestPyPI project url")
    if "pypi.org" in derived.replace("test.pypi.org", ""):
        problems.append("a production pypi.org reference survived")
    if problems:
        raise WorkflowShapeError(
            [f"derived workflow failed self-verification ({p})" for p in problems]
        )


def rehearsal_identity(doc):
    """(project, environment) from the rig env (defaults match
    rig.env.example), or None after a REFUSED notice — the same offline
    production-target guard as lib.sh: rehearsal identity must be
    unmistakably non-production."""
    project = os.environ.get("LMER_REHEARSAL_PROJECT") or "lmer-rehearsal"
    environment = os.environ.get("LMER_REHEARSAL_ENVIRONMENT") or "testpypi"
    prod_name, _, _ = production_identity(doc)

    if project.lower() == prod_name.lower():
        fail(
            f"'{project}' is the production PyPI project name; the rehearsal "
            "project must be unmistakably non-production (e.g. lmer-rehearsal)"
        )
        return None
    if environment.lower() == "pypi":
        fail(
            "'pypi' is the production environment name; the rehearsal "
            "environment must be distinct (e.g. testpypi)"
        )
        return None
    return project, environment


def run_emit(path, doc, text):
    identity = rehearsal_identity(doc)
    if identity is None:
        return 1
    project, environment = identity
    sys.stdout.write(transform(path, doc, text, project, environment))
    return 0


def run_rig_diff(path, doc, text, rig_workflow):
    """The committed-copy half of the drift guard (README: Workflow
    derivation): re-derive from the current production workflow and diff
    against the rig repo's committed release.yml. Any difference — a stale
    rig copy, a hand-edited rig workflow, a missing file — exits non-zero
    with a readable diff."""
    identity = rehearsal_identity(doc)
    if identity is None:
        return 1
    project, environment = identity
    derived = transform(path, doc, text, project, environment)

    if rig_workflow == "-":
        rig_name = "<stdin>"
        rig_text = sys.stdin.read()
    else:
        rig_name = str(rig_workflow)
        try:
            rig_text = Path(rig_workflow).read_text()
        except OSError as exc:
            print(f"DRIFT GUARD FAILED: cannot read the rig workflow copy "
                  f"{rig_workflow}: {exc}", file=sys.stderr)
            return 1

    if rig_text == derived:
        print(f"drift guard OK: rig copy ({rig_name}) matches the freshly "
              "derived workflow")
        return 0

    print(f"DRIFT GUARD FAILED: the rig repo's committed workflow differs "
          f"from the output freshly derived from {path}:", file=sys.stderr)
    sys.stderr.writelines(difflib.unified_diff(
        derived.splitlines(keepends=True),
        rig_text.splitlines(keepends=True),
        fromfile="derived-from-production",
        tofile=rig_name,
    ))
    print("Re-run Ctl/rehearsal/stand-up.sh to converge the rig copy. Do "
          "NOT run a rehearsal until --check is green.", file=sys.stderr)
    return 1


def main(argv):
    parser = argparse.ArgumentParser(
        description="Derive the rehearsal rig workflow from the production "
        "release workflow; --check is the drift guard."
    )
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--check", action="store_true",
                      help="drift guard: fail loudly if the workflow no "
                      "longer has the expected shape")
    mode.add_argument("--emit", action="store_true",
                      help="check, then write the derived rig workflow to "
                      "stdout")
    parser.add_argument("--rig-workflow", metavar="PATH",
                        help="with --check: the rig repo's committed "
                        "release.yml ('-' for stdin); re-derive and diff "
                        "against it, failing on any difference")
    parser.add_argument("workflow", nargs="?", type=Path,
                        default=DEFAULT_WORKFLOW,
                        help=f"source workflow (default: {DEFAULT_WORKFLOW})")
    args = parser.parse_args(argv)
    if args.rig_workflow and args.emit:
        parser.error("--rig-workflow only applies to --check")

    try:
        doc, text = load_workflow(args.workflow)
        # When emitting, stdout is the derived workflow — the guard's OK
        # notice goes to stderr instead.
        ok_stream = sys.stderr if args.emit else sys.stdout
        if run_check(args.workflow, doc, ok_stream) != 0:
            return 1
        if args.emit:
            return run_emit(args.workflow, doc, text)
        if args.rig_workflow:
            return run_rig_diff(args.workflow, doc, text, args.rig_workflow)
        # Plain --check additionally DRY-RUNS the transform: the shape
        # checks are YAML-level but the transform is text-level, so a
        # semantically-identical reformat could keep the checks green while
        # --emit refuses — a green --check must imply emit-ability.
        identity = rehearsal_identity(doc)
        if identity is None:
            return 1
        transform(args.workflow, doc, text, *identity)
        print("transform dry-run OK (a rehearsal --emit will succeed)",
              file=ok_stream)
        return 0
    except WorkflowShapeError as exc:
        print(f"DRIFT GUARD FAILED: {args.workflow}:", file=sys.stderr)
        for failure in exc.failures:
            print(f"  - {failure}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
