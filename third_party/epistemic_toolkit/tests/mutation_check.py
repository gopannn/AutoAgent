"""Mutation check of the test suite itself: each mutant re-introduces a defect
found in a parent toolkit. Every mutant must be KILLED (some test fails).
Run: python3 tests/mutation_check.py"""
import shutil, subprocess, sys, pathlib, tempfile
SRC = pathlib.Path(__file__).resolve().parents[1]
M = {
 "group-out disabled": ("robustness.py", 'grp_pass = side != "tie" and not grp_flips', 'grp_pass = side != "tie"'),
 "tie counted as a side": ("robustness.py", 'if abs(x - t) <= EPS:\n        return "tie"', 'if False:\n        return "tie"'),
 "universal termination not required": ("statemachine.py", '"universal_termination": not cycle and not dead and not stranded', '"universal_termination": not dead and not stranded'),
 "FPR gate on point estimate": ("instrument.py", '"false_positive_rate_upper_bound_within_limit": fpr_ci[1] <= max_false_positive_rate', '"false_positive_rate_upper_bound_within_limit": fp / trials <= max_false_positive_rate'),
 "p-value without +1": ("instrument.py", "p = (1 + at_least) / (1 + null_runs)", "p = at_least / null_runs"),
 "support criterion removed": ("derived.py", "or ent[b] == 0.0 or sup1[a] < need:", "or ent[b] == 0.0:"),
 "observed FD reported as proven": ("derived.py", 'add(Finding("functional_dependency", b, [a], "observed"', 'add(Finding("functional_dependency", b, [a], "structural"'),
 "untargeted allowed in strict": ("regression.py", '({"UNTARGETED"} if self.strict else set())', 'set()'),
 "crash counted as catch": ("regression.py", 'elif any(t in errs for t in targets):\n                status = "ERRORED"', 'elif False:\n                status = "ERRORED"'),
 "verify_encodings substring-only": ("statemachine.py", "elif text != expected[name]:", "elif not all(l in text for l in expected[name].splitlines() if '-->' in l):"),
 "report skips re-gating": ("policy.py", "        if not g.robust:\n            out.append(ReportedScore(h.id, h.statement, COMPUTED, UNRESOLVED", "        if False:\n            out.append(ReportedScore(h.id, h.statement, COMPUTED, UNRESOLVED"),
}
survivors = 0
for name, (f, a, b) in M.items():
    tmp = pathlib.Path(tempfile.mkdtemp() + "/m"); shutil.rmtree(tmp, ignore_errors=True); shutil.copytree(SRC, tmp)
    p = tmp / "src/epistemic_toolkit" / f; s = p.read_text()
    assert a in s, (name, "anchor missing"); p.write_text(s.replace(a, b, 1))
    r = subprocess.run([sys.executable, "-m", "unittest", "discover", "-s", "tests"], cwd=tmp, capture_output=True, text=True)
    fails = r.stderr.count("\nFAIL:") + r.stderr.count("\nERROR:")
    print(f"{'KILLED' if r.returncode else 'SURVIVED':8s} ({fails} tests fail)  {name}")
    survivors += not r.returncode
    shutil.rmtree(tmp, ignore_errors=True)
raise SystemExit(1 if survivors else 0)
