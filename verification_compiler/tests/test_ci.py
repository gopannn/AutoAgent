"""PR runner, repository I/O, report rendering and the deployment gate."""
import json
import subprocess
from pathlib import Path

import pytest

from verification_compiler import ci, verify_gate
from verification_compiler.hashing import codebase_hash, sha256_hex
from verification_compiler.repo_io import ProjectError, load_codebase, write_back
from verification_compiler.report import MARKER, render_summary

from . import fakes

ENTRY = "service.main:app"


def make_project(root: Path) -> Path:
    proj = root / "svc"
    (proj / "service").mkdir(parents=True)
    (proj / "service" / "__init__.py").write_text("")
    (proj / "service" / "main.py").write_text(fakes.APP)
    (proj / "requirements.txt").write_text("# pins\nfastapi==0.141.1\nuvicorn==0.54.0  # server\n")
    (proj / "tests").mkdir()
    (proj / "tests" / "conftest.py").write_text("import pytest\n")
    (proj / ".env.example").write_text("X=1\n")
    (proj / "logo.png").write_bytes(b"\x89PNG\r\n\x1a\n\xff\xfe")
    (proj / "__pycache__").mkdir()
    (proj / "__pycache__" / "x.pyc").write_bytes(b"\x00")
    return proj


# ---------------------------------------------------------------- repo_io

def test_load_codebase_skips_what_the_compiler_must_not_touch(tmp_path):
    loaded = load_codebase(make_project(tmp_path), ENTRY)
    paths = [f["path"] for f in loaded.codebase["files"]]
    assert paths == ["service/__init__.py", "service/main.py"]
    assert loaded.codebase["dependencies"] == ["fastapi==0.141.1", "uvicorn==0.54.0"]
    skipped = dict(loaded.skipped)
    assert set(skipped) == {".env.example", "logo.png", "tests/conftest.py"}


def test_load_codebase_refuses_to_ship_secrets(tmp_path):
    proj = make_project(tmp_path)
    (proj / "service" / "settings.py").write_text('JWT_SECRET = "super-secret-signing-key"\n')
    with pytest.raises(ProjectError, match="secrets"):
        load_codebase(proj, ENTRY)
    assert load_codebase(proj, ENTRY, scan_secrets=False).codebase["files"]


def test_requirements_options_rejected(tmp_path):
    proj = make_project(tmp_path)
    (proj / "requirements.txt").write_text("--extra-index-url https://evil\nfastapi==1.0\n")
    with pytest.raises(ProjectError, match="option lines"):
        load_codebase(proj, ENTRY)


def test_symlinks_are_skipped(tmp_path):
    proj = make_project(tmp_path)
    (tmp_path / "outside.py").write_text("SECRET = 1\n")
    (proj / "service" / "link.py").symlink_to(tmp_path / "outside.py")
    assert ("service/link.py", "symlink") in load_codebase(proj, ENTRY).skipped


def test_write_back_applies_only_the_diff(tmp_path):
    proj = make_project(tmp_path)
    original = load_codebase(proj, ENTRY).codebase
    final = json.loads(json.dumps(original))
    final["files"] = [f for f in final["files"] if f["path"] != "service/__init__.py"]
    final["files"][0]["content"] += "\n# patched\n"
    final["files"].append({"path": "service/auth.py", "content": "X = 1\n"})
    final["dependencies"] = ["fastapi==0.141.1", "uvicorn==0.54.0", "pyjwt==2.10.1"]

    changed = write_back(proj, original, final)
    assert changed == ["service/auth.py", "service/main.py", "service/__init__.py", "requirements.txt"]
    assert not (proj / "service" / "__init__.py").exists()
    assert (proj / "tests" / "conftest.py").exists(), "skipped files are untouched"
    assert "pyjwt==2.10.1" in (proj / "requirements.txt").read_text()
    assert codebase_hash(load_codebase(proj, ENTRY).codebase) == codebase_hash(final)


def test_write_back_never_overwrites_unseen_files(tmp_path):
    proj = make_project(tmp_path)
    original = load_codebase(proj, ENTRY).codebase
    final = {**original, "files": original["files"] + [{"path": "logo.png", "content": "text"}]}
    with pytest.raises(ProjectError, match="not part of the verified codebase"):
        write_back(proj, original, final)


# ---------------------------------------------------------------- ci.run

def released_final(codebase):
    sp = fakes.spec()
    lock = fakes.FakeResolver().resolve(codebase["dependencies"])
    result = fakes.passing_result(codebase, sp, lock)
    return {
        "status": "released",
        "codebase": codebase,
        "lockfile": lock,
        "iteration": 1,
        "verification_result": result.model_dump(),
        "findings_ledger": {},
        "release_manifest": {
            "status": "release_ready", "build_id": "BLD-x", "artifact_hash": "a" * 64,
            "codebase_hash": codebase_hash(codebase), "lockfile_hash": lock["sha256"],
            "models_used": {"builder": "m"}, "verification_evidence": result.model_dump(),
        },
    }


def run_ci(tmp_path, invoke, out_env=None):
    proj = make_project(tmp_path)
    report = tmp_path / "report"
    code = ci.run(tmp_path, "svc", ENTRY, report, title="Add auth", body="Protect /health",
                  thread_id="PR-1", invoke=invoke)
    return code, proj, report


def test_ci_release_writes_patch_manifest_and_lock(tmp_path):
    seen = {}

    def invoke(requirements, codebase, thread_id):
        seen.update(requirements=requirements, thread_id=thread_id)
        patched = json.loads(json.dumps(codebase))
        patched["files"][1]["content"] += "\n# fixed\n"
        return released_final(patched)

    code, proj, report = run_ci(tmp_path, invoke)
    assert code == 0
    assert "Add auth" in seen["requirements"] and "Protect /health" in seen["requirements"]
    assert (proj / "service" / "main.py").read_text().endswith("# fixed\n")
    manifest = json.loads((tmp_path / ".verification" / "release_manifest.json").read_text())
    assert manifest["codebase_hash"] == codebase_hash(load_codebase(proj, ENTRY).codebase)
    assert sha256_hex((tmp_path / ".verification" / "requirements.lock").read_text()) == manifest["lockfile_hash"]
    summary = (report / "summary.md").read_text()
    assert summary.startswith(MARKER) and "RELEASE_READY" in summary and "service/main.py" in summary


def test_ci_rejection_changes_nothing(tmp_path):
    def invoke(requirements, codebase, thread_id):
        return {"status": "budget_exceeded", "validation_feedback": "[ruff] exit=1\nF401", "feedback_source": "verification"}

    code, proj, report = run_ci(tmp_path, invoke)
    assert code == 1
    assert (proj / "service" / "main.py").read_text() == fakes.APP
    assert not (tmp_path / ".verification").exists()
    assert "REJECTED" in (report / "summary.md").read_text() and "F401" in (report / "summary.md").read_text()


def test_ci_crash_is_an_error_not_a_release(tmp_path):
    def invoke(*a):
        raise RuntimeError("boom")

    code, _, report = run_ci(tmp_path, invoke)
    assert code == 2 and "compiler crashed" in (report / "summary.md").read_text()


def test_ci_rejects_project_root_escape(tmp_path):
    code = ci.run(tmp_path, "../..", ENTRY, tmp_path / "r", title="", body="", thread_id="t", invoke=lambda *a: {})
    assert code == 2


def test_manifest_not_rewritten_for_same_code(tmp_path):
    cb = load_codebase(make_project(tmp_path), ENTRY).codebase
    manifest = released_final(cb)["release_manifest"]
    d = tmp_path / ".verification"
    assert ci._store_manifest(d, manifest, "lock") is True
    (d / "release_manifest.sigstore.json").write_text("{}")
    assert ci._store_manifest(d, {**manifest, "build_id": "BLD-other"}, "lock") is False
    assert (d / "release_manifest.sigstore.json").exists()


def test_pr_requirements_defaults_and_truncation():
    assert "Automated maintenance" in ci.pr_requirements("", "")
    assert len(ci.pr_requirements("t", "x" * 50_000)) < 21_000


# ---------------------------------------------------------------- report

def test_report_shows_real_gate_results():
    cb = fakes.codebase()
    final = released_final(cb)
    final["verification_result"]["checks"]["semgrep"] = {"name": "semgrep", "passed": False, "exit_code": 1}
    text = render_summary(final)
    assert "| SAST | Semgrep | FAILED (exit 1) |" in text
    assert "(1/1 acceptance criteria)" in text


# ---------------------------------------------------------------- verify_gate

def cosign_ok(cmd, **kw):
    return subprocess.CompletedProcess(cmd, 0, "", "Verified OK")


def released_repo(tmp_path):
    ci_code, proj, _ = run_ci(tmp_path, lambda r, cb, t: released_final(cb))
    assert ci_code == 0
    (tmp_path / ".verification" / "release_manifest.sigstore.json").write_text("{}")
    return proj


def gate(tmp_path, proj, runner=cosign_ok, **kw):
    return verify_gate.check(tmp_path / ".verification", proj, ENTRY,
                             identity="https://github.com/o/r/.github/workflows/ai-compiler.yml@refs/heads/main",
                             issuer=verify_gate.GITHUB_OIDC_ISSUER, runner=runner, **kw)


def test_gate_passes_for_signed_manifest_matching_disk(tmp_path):
    proj = released_repo(tmp_path)
    assert gate(tmp_path, proj) == []


def test_gate_blocks_code_changed_after_verification(tmp_path):
    proj = released_repo(tmp_path)
    (proj / "service" / "main.py").write_text(fakes.APP + "\nBACKDOOR = True\n")
    assert any("changed after verification" in p for p in gate(tmp_path, proj))


def test_gate_blocks_lockfile_drift(tmp_path):
    proj = released_repo(tmp_path)
    (tmp_path / ".verification" / "requirements.lock").write_text("evil==1.0\n")
    assert any("lockfile" in p for p in gate(tmp_path, proj))


def test_gate_blocks_bad_signature_and_missing_cosign(tmp_path):
    proj = released_repo(tmp_path)
    bad = lambda cmd, **kw: subprocess.CompletedProcess(cmd, 1, "", "none of the expected identities matched")  # noqa: E731
    assert any("cosign verification failed" in p for p in gate(tmp_path, proj, runner=bad))

    def missing(cmd, **kw):
        raise FileNotFoundError("cosign")

    assert any("could not run" in p for p in gate(tmp_path, proj, runner=missing))


def test_gate_passes_expected_identity_to_cosign(tmp_path):
    proj = released_repo(tmp_path)
    seen = []
    gate(tmp_path, proj, runner=lambda cmd, **kw: seen.append(cmd) or cosign_ok(cmd))
    cmd = seen[0]
    assert cmd[cmd.index("--certificate-identity") + 1].endswith("ai-compiler.yml@refs/heads/main")
    assert cmd[cmd.index("--certificate-oidc-issuer") + 1] == verify_gate.GITHUB_OIDC_ISSUER


def test_gate_blocks_unsigned_or_rejected_manifest(tmp_path):
    proj = released_repo(tmp_path)
    (tmp_path / ".verification" / "release_manifest.sigstore.json").unlink()
    assert any("bundle" in p for p in gate(tmp_path, proj))
    path = tmp_path / ".verification" / "release_manifest.json"
    path.write_text(json.dumps({**json.loads(path.read_text()), "status": "rejected"}))
    assert any("status" in p for p in gate(tmp_path, proj))
