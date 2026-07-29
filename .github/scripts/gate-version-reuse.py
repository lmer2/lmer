#!/usr/bin/env python3
"""gate-version-reuse.py — publish-gate version reuse check for release.yml.

`skip-existing: true` on the publish step converges leg-2 re-entry, but it
converges SILENTLY: a version already on the index is skipped whatever it
holds. This gate runs before the publish step and makes reuse an explicit,
admin-authorized act instead.

Why filename comparison alone is not enough: distribution filenames are a
pure function of (project name, version), so a tag re-pointed at different
code with the same version produces exactly the filenames this run built.
"Published set ⊆ built set" therefore cannot tell a converging resume from
a divergent republish — both look identical. Digests cannot either (builds
are not byte-reproducible), and the index's PEP 740 provenance binds the
artifact to the run in the signing certificate, not in the JSON the API
serves. So the gate fails CLOSED on an already-published version and takes
the resume decision from a human: an admin sets the
`RELEASE_RESUME_VERSION` Actions repository variable to the exact version
being resumed, re-runs, and clears it afterwards. Same trust model as
`RELEASE_ALLOWED_SIGNERS` — admin-controlled variable, never repo content.

Environment seams (all inputs; no positional arguments):
  PYPI_PROJECT_URL        required. The index project url,
                          https://pypi.org/project/<name>/. The rehearsal
                          transform (Ctl/rehearsal/derive-workflow.py)
                          rewrites it to the TestPyPI project url, so the
                          gate follows the rig automatically. The JSON API
                          url is derived from it — nothing is hard-coded.
  GITHUB_REF_NAME         required. The pushed tag name (e.g. v0.5.0); the
                          version is the tag minus a leading `v`.
  RELEASE_RESUME_VERSION  optional. The one version whose already-published
                          state may be published over (with or without the
                          leading `v`). Empty/unset means no resume is
                          authorized.
  DIST_DIR                optional. Directory holding the built
                          distributions, default `dist`.

Exit status: 0 only when publishing may proceed; every refusal emits a
GitHub Actions ::error:: annotation and exits 1 (fail closed).
"""
import hashlib
import json
import os
import pathlib
import sys
import urllib.error
import urllib.request


def fail(message):
    print(f"::error::{message}")
    return 1


def required_env(name):
    value = os.environ.get(name, "").strip()
    if not value:
        raise SystemExit(fail(f"{name} is empty or unset; refusing to gate a "
                              "publish without it"))
    return value


def index_api_url(project_url, version):
    """The JSON API url for <version>, derived from the project url.

    https://pypi.org/project/lmer/ -> https://pypi.org/pypi/lmer/<v>/json
    """
    host_base, separator, name = project_url.rstrip("/").rpartition("/project/")
    if not separator or not host_base or not name:
        raise SystemExit(fail(
            f"PYPI_PROJECT_URL '{project_url}' is not an index project url "
            "(expected https://<host>/project/<name>/)"))
    return f"{host_base}/pypi/{name}/{version}/json", name


def built_distributions(dist_dir):
    directory = pathlib.Path(dist_dir)
    if not directory.is_dir():
        raise SystemExit(fail(f"{dist_dir}/ does not exist; nothing was built "
                              "to publish"))
    built = {
        path.name: hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(directory.iterdir()) if path.is_file()
    }
    if not built:
        raise SystemExit(fail(f"{dist_dir}/ holds no distributions; nothing "
                              "was built to publish"))
    return built


def published_distributions(api_url):
    """Published files for the version, or None when it is not on the index."""
    try:
        with urllib.request.urlopen(api_url) as response:
            data = json.load(response)
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return None
        raise SystemExit(fail(
            f"index lookup {api_url} failed with HTTP {exc.code}: {exc.reason} "
            "— refusing to publish without knowing the version's state"))
    except urllib.error.URLError as exc:
        raise SystemExit(fail(
            f"index lookup {api_url} failed: {exc.reason} — refusing to "
            "publish without knowing the version's state"))
    return {
        entry.get("filename"): {
            "sha256": (entry.get("digests") or {}).get("sha256"),
            "uploaded": entry.get("upload_time_iso_8601"),
            "provenance": entry.get("provenance"),
        }
        for entry in data.get("urls", []) if entry.get("filename")
    }


def report(built, published):
    """Everything a human needs to judge the version's state by hand."""
    for filename in sorted(published):
        detail = published[filename]
        print(f"  published {filename} sha256={detail['sha256']} "
              f"uploaded={detail['uploaded']} "
              f"provenance={detail['provenance'] or 'none'}")
    for filename in sorted(built):
        print(f"  built     {filename} sha256={built[filename]}")


def main():
    project_url = required_env("PYPI_PROJECT_URL")
    tag = required_env("GITHUB_REF_NAME")
    version = tag[1:] if tag.startswith("v") else tag
    resume = os.environ.get("RELEASE_RESUME_VERSION", "").strip()
    resume = resume[1:] if resume.startswith("v") else resume

    api_url, name = index_api_url(project_url, version)
    built = built_distributions(os.environ.get("DIST_DIR", "dist"))
    published = published_distributions(api_url)

    if published is None:
        print(f"{name} {version} is not on the index yet — first publish, "
              "proceeding")
        return 0

    foreign = sorted(set(published) - set(built))
    if foreign:
        report(built, published)
        return fail(f"version {version} already exists on {project_url} with "
                    f"artifacts this run did not build: {', '.join(foreign)} — "
                    "refusing to publish over a diverged release")

    if resume != version:
        report(built, published)
        return fail(
            f"version {version} is already published on {project_url}. "
            "Filenames are a function of (name, version), so this run cannot "
            "tell a resumed publish from a republish of different code — and "
            "skip-existing would keep the PUBLISHED artifacts while the "
            "GitHub Release got this run's. Either release a new version, or "
            f"— if this is a deliberate resume — set the "
            f"RELEASE_RESUME_VERSION Actions variable to '{version}', re-run, "
            "and clear it afterwards.")

    print(f"::warning::version {version} is already published and "
          "RELEASE_RESUME_VERSION authorizes resuming it — skip-existing will "
          "no-op the overlap, so PyPI keeps the artifacts (and attestations) "
          "it already serves, not this run's")
    report(built, published)
    return 0


if __name__ == "__main__":
    sys.exit(main())
