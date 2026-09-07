"""Copy-only offline baseline; never imports application from the working tree.

Usage: python tools/baseline/run_tests.py --python /path/to/locked/python --node node
CI MUST additionally use a network-disabled OS namespace or container. These
language guards protect accidental I/O, not adversarial native-code execution.
"""
import argparse
import hashlib
import io
import json
import os
from pathlib import Path
import platform
import re
import shutil
import subprocess
import sys
import tempfile
import time
import unittest

REPOSITORY = Path(__file__).resolve().parents[2]
APP_NAME = "WalletHunterV05_final"
ALLOWED_DIRS = {"core": {".py"}, "integrations": {".py"}, "desktop": {".py"},
                "webapp": {".py", ".js", ".css", ".html"}, "tests": {".py", ".cjs"}}
ALLOWED_SCRIPTS = {"backup_runtime.py", "enable_ai_modes.py", "recover_ownership.py"}
CLASSIFICATIONS = {"A":"environment/setup", "B":"reproducibility", "C":"existing application defect",
                   "D":"flaky/nondeterministic", "E":"unknown"}


def copy_app(source, target):
    files = []
    for dirname, extensions in ALLOWED_DIRS.items():
        for path in sorted((source/dirname).rglob("*")):
            relative = path.relative_to(source)
            if path.is_symlink():
                raise RuntimeError("Source symlink requires explicit baseline review: " + str(relative))
            if not path.is_file() or path.suffix.lower() not in extensions:
                continue
            if any(part.startswith(".") or part in {"data","backups","__pycache__","node_modules"} for part in relative.parts):
                continue
            files.append(relative)
    files += [Path("scripts")/name for name in sorted(ALLOWED_SCRIPTS)]
    for relative in files:
        if (source/relative).is_symlink(): raise RuntimeError("Symlink not allowed")
        destination = target/relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source/relative, destination)
    return [p.as_posix() for p in files]


def safe_environment(sandbox, source, app, manifest, python, phase="0"):
    # No inherited API keys, proxy settings, cloud/SSH credentials or Python hooks.
    env = {k: os.environ[k] for k in ("SystemRoot","WINDIR","COMSPEC","SYSTEMDRIVE") if k in os.environ}
    env.update({"PATH":str(Path(os.path.abspath(python)).parent), "HOME":str(sandbox/"home"),
        "USERPROFILE":str(sandbox/"home"), "APPDATA":str(sandbox/"home"), "LOCALAPPDATA":str(sandbox/"home"),
        "TMP":str(sandbox/"tmp"),"TEMP":str(sandbox/"tmp"),"TMPDIR":str(sandbox/"tmp"),
        "PYTHONPATH":os.pathsep.join((str(sandbox/"guards"),str(app),str(app/"tests"))),
        "PYTHONDONTWRITEBYTECODE":"1", "PYTHONNOUSERSITE":"1", "PYTHONUTF8":"1", "PYTHONIOENCODING":"utf-8",
        "PYTHON_DOTENV_DISABLED":"1",
        "HL_MODE":"TESTNET", "AUTO_TRADING":"false", "TELEGRAM_API_ID":"1", "TELEGRAM_API_HASH":"baseline-fake-hash",
        "TELEGRAM_BOT_TOKEN":"000000000:baseline-fake-token", "TELEGRAM_OWNER_ID":"1",
        "MASTER_KEY":"MDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDA=", "LANG":"C.UTF-8",
        "WALLETHUNTER_BASELINE_CHILD":"1", "WALLETHUNTER_BASELINE_SANDBOX":str(sandbox),
        "WALLETHUNTER_BASELINE_PHASE":phase,
        "WALLETHUNTER_BASELINE_SOURCE":str(source), "WALLETHUNTER_BASELINE_APP":str(app),
        "WALLETHUNTER_BASELINE_MANIFEST":str(manifest)})
    return env


def selected_modules(location, names):
    """Resolve only direct copied test module names; no paths or dotted imports."""
    modules = []
    for name in names:
        if not re.fullmatch(r"test_[A-Za-z0-9_]+", name):
            raise ValueError("Selected Python tests must be direct test_module names")
        path = location / (name + ".py")
        if path.is_symlink() or not path.is_file() or path.resolve().parent != location.resolve():
            raise ValueError("Selected Python test does not exist as a regular direct module: " + name)
        if name in modules:
            raise ValueError("Duplicate selected Python module: " + name)
        modules.append(name)
    return modules


def python_test_suite(location, modules=()):
    if not modules:
        return unittest.TestLoader().discover(str(location), pattern="test_*.py")
    suite = unittest.TestSuite()
    for name in selected_modules(location, modules):
        suite.addTests(unittest.TestLoader().discover(str(location), pattern=name + ".py", top_level_dir=str(location)))
    return suite


def child_run(kind, result_path, modules=()):
    import sitecustomize
    if not sitecustomize.ACTIVE: raise RuntimeError("Baseline guard was not installed")
    app = Path(os.environ["WALLETHUNTER_BASELINE_APP"])
    location = app/"tests" if kind == "python" else app.parent/"tests_baseline"
    started = time.monotonic()
    suite = python_test_suite(location, modules if kind == "python" else ())
    capture = io.StringIO()
    class TracedResult(unittest.TextTestResult):
        def startTest(self, test):
            sitecustomize.CURRENT_TEST = test.id()
            super().startTest(test)
        def stopTest(self, test):
            super().stopTest(test)
            sitecustomize.CURRENT_TEST = None
    result = unittest.TextTestRunner(stream=capture, verbosity=1, resultclass=TracedResult).run(suite)
    details = [{"test":str(test),"traceback":trace,"classification":"E"} for test,trace in result.failures+result.errors]
    payload = {"tests":result.testsRun,"passed":result.testsRun-len(result.failures)-len(result.errors)-len(result.skipped)-len(result.expectedFailures)-len(result.unexpectedSuccesses),
        "failed":len(result.failures),"errors":len(result.errors),"skipped":len(result.skipped),
        "expected_failures":len(result.expectedFailures),"unexpected_successes":len(result.unexpectedSuccesses),
        "duration_seconds":round(time.monotonic()-started,3),"success":result.wasSuccessful(),
        "failures":details,"skips":[{"test":str(t),"reason":r,"classification":"A" if "POSIX" in r else "E"} for t,r in result.skipped],
        "denied_operations":dict(sitecustomize.DENIED), "denied_events":list(sitecustomize.EVENTS), "output":capture.getvalue()}
    for event in payload["denied_events"]:
        if any(frame["function"] == "_has_ipv6" for frame in event["stack"]):
            event.update(classification="A", explanation="urllib3 import-time local IPv6 capability probe was blocked; no outbound request or application order.")
    Path(result_path).write_text(json.dumps(payload,ensure_ascii=False,indent=2),encoding="utf-8")
    print(json.dumps({"suite":kind,**{k:payload[k] for k in ("tests","passed","failed","errors","skipped","duration_seconds")}}))
    return 0 if result.wasSuccessful() else 1


def execute(command, cwd, env, timeout=300):
    start=time.monotonic()
    try:
        done=subprocess.run(command,cwd=cwd,env=env,capture_output=True,text=True,encoding="utf-8",errors="replace",timeout=timeout)
        return {"exit_code":done.returncode,"duration_seconds":round(time.monotonic()-start,3),"stdout":done.stdout,"stderr":done.stderr}
    except subprocess.TimeoutExpired as exc:
        return {"exit_code":124,"duration_seconds":round(time.monotonic()-start,3),"stdout":str(exc.stdout or ""),"stderr":"Baseline timeout","classification":"E"}


def node_result(execution):
    output=execution["stdout"]
    metrics={}
    for field in ("tests","pass","fail","cancelled","skipped","todo"):
        match=re.search(r"^# "+field+r" (\d+)\s*$",output,re.M)
        metrics[field]=int(match.group(1)) if match else None
    return {**execution,**metrics,"success":execution["exit_code"]==0 and metrics["fail"]==0}


def argument_parser():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source",type=Path,default=REPOSITORY/APP_NAME)
    parser.add_argument("--python",default=sys.executable)
    parser.add_argument("--node",default="node")
    parser.add_argument("--report",type=Path)
    parser.add_argument("--phase",choices=("0", "1.1"),default="0")
    parser.add_argument("--python-module",action="append",default=[])
    parser.add_argument("--guards-only",action="store_true")
    parser.add_argument("--child",choices=("python","guards"))
    parser.add_argument("--child-report")
    return parser


def validate_options(args, source):
    if args.phase == "0" and args.python_module:
        raise ValueError("Selected modules require --phase 1.1; Phase 0 remains a full baseline")
    if args.guards_only and args.python_module:
        raise ValueError("--guards-only cannot be combined with Python test selection")
    if args.phase == "1.1":
        if args.report is None:
            raise ValueError("Phase 1.1 requires an explicit --report path")
        historical = {REPOSITORY / "docs" / "baseline-tests.json", REPOSITORY / "docs" / "baseline-guards.json"}
        if args.report.resolve() in {path.resolve() for path in historical}:
            raise ValueError("Phase 1.1 cannot overwrite historical Phase 0 reports")
    return selected_modules(source / "tests", args.python_module)


def main():
    parser=argument_parser()
    args=parser.parse_args()
    if args.child: return child_run(args.child,args.child_report,args.python_module)
    source=args.source.resolve()
    try:
        modules=validate_options(args,source)
    except ValueError as exc:
        parser.error(str(exc))
    if args.report is None:
        args.report=REPOSITORY/"docs"/"baseline-tests.json"
    # A Linux venv executable is normally a symlink: resolving its target would
    # silently select system Python and discard the locked environment.
    python=os.path.abspath(args.python) if Path(args.python).exists() else shutil.which(args.python)
    node=str(Path(args.node).resolve()) if Path(args.node).exists() else shutil.which(args.node)
    if not python or not node: parser.error("Python and Node executables must be installed before isolated testing")
    root=REPOSITORY
    report={"schema_version":1,"phase":args.phase,"application_changes":args.phase != "0","classifications":CLASSIFICATIONS,
            "platform":platform.platform(),"os_network_sandbox":os.environ.get("BASELINE_OS_NETWORK_SANDBOX","not_attested"),
            "dependency_inventory":"docs/dependency-inventory.json",
            "full_project_status":"PARTIAL",
            "auxiliary_browser":{"status":"NOT_RUN","inventory":"docs/browser-baseline.json",
                "reason":"Existing browser scripts require a separately isolated, reproducibly pinned browser runner."},
            "isolation_note":"Language I/O guards, clean environment and disposable allowlisted copy; not a hostile native-code sandbox.",
            "existing":{},"guards":{}}
    if args.phase != "0":
        report.update(evidence_label="P1.1 authorized ownership-history cache validation",
            historical_phase0_baseline=False,
            test_scope="GUARDS_ONLY" if args.guards_only else ("SELECTED_PYTHON_AND_ALL_JAVASCRIPT" if modules else "ALL_COPIED_PYTHON_AND_JAVASCRIPT"),
            selected_python_modules=modules)
    # mkdtemp outside source: no app path, credential or runtime file is reused.
    with tempfile.TemporaryDirectory(prefix="wallethunter-baseline-") as folder:
        sandbox=Path(folder).resolve(); app=sandbox/APP_NAME
        (sandbox/"guards").mkdir(); (sandbox/"home").mkdir(); (sandbox/"tmp").mkdir()
        files=copy_app(source,app)
        manifest=sandbox/"copied-files.json"; manifest.write_text(json.dumps(files),encoding="utf-8")
        for name in ("sitecustomize.py","node_network_guard.cjs","run_tests.py"):
            shutil.copyfile(root/"tools"/"baseline"/name,sandbox/"guards"/name)
        shutil.copytree(root/"tests_baseline",sandbox/"tests_baseline",ignore=shutil.ignore_patterns("__pycache__","*.pyc"))
        env=safe_environment(sandbox,source,app,manifest,python,args.phase)
        report["copied_files"]=files
        report["source_hashes"]={name:hashlib.sha256((source/name).read_bytes()).hexdigest() for name in files}
        report["discovery"]={"python_modules":len(list((app/"tests").glob("test_*.py"))),"javascript_files":len(list((app/"tests").glob("*.cjs")))}
        if args.phase != "0":
            report["discovery"]["selected_python_modules"] = len(modules) if modules else report["discovery"]["python_modules"]
        report["python_version"]=execute([python,"--version"],app,env)["stdout"].strip()
        report["node_version"]=execute([node,"--version"],app,env)["stdout"].strip()
        def python_suite(kind):
            result_file=sandbox/(kind+"-result.json")
            command=[python,"-B",str(sandbox/"guards"/"run_tests.py"),"--child",kind,"--child-report",str(result_file)]
            if kind == "python":
                for name in modules:
                    command.extend(("--python-module",name))
            execution=execute(command,app,env)
            result=json.loads(result_file.read_text(encoding="utf-8")) if result_file.exists() else {"success":False,"classification":"A","reason":"child_failed_before_report"}
            result["process"]=execution
            return result
        report["guards"]["python"]=python_suite("guards")
        report["guards"]["javascript"]=node_result(execute([node,"--test-reporter=tap","--require",str(sandbox/"guards"/"node_network_guard.cjs"),str(sandbox/"tests_baseline"/"node_guard_test.cjs")],app,env))
        guards_ok=all(row.get("success") for row in report["guards"].values())
        if guards_ok and not args.guards_only:
            report["existing"]["python"]=python_suite("python")
            report["existing"]["javascript"]={file.name:node_result(execute([node,"--test-reporter=tap","--require",str(sandbox/"guards"/"node_network_guard.cjs"),str(file)],app,env)) for file in sorted((app/"tests").glob("*.cjs"))}
        report["source_unchanged"]=all(hashlib.sha256((source/name).read_bytes()).hexdigest()==digest for name,digest in report["source_hashes"].items())
        report["success"]=guards_ok and report["source_unchanged"] and (args.guards_only or
            (report["existing"]["python"]["success"] and all(r["success"] for r in report["existing"]["javascript"].values())))
        report["scoped_status"]="PASS" if report["success"] else "FAIL"
        legacy_py=report["existing"].get("python",{})
        legacy_js=list(report["existing"].get("javascript",{}).values())
        report["totals"]={
            "existing":{"tests":(legacy_py.get("tests") or 0)+sum(r.get("tests") or 0 for r in legacy_js),
                "passed":(legacy_py.get("passed") or 0)+sum(r.get("pass") or 0 for r in legacy_js),
                "failed":(legacy_py.get("failed") or 0)+sum(r.get("fail") or 0 for r in legacy_js),
                "errors":legacy_py.get("errors") or 0,
                "skipped":(legacy_py.get("skipped") or 0)+sum(r.get("skipped") or 0 for r in legacy_js),
                "duration_seconds":round((legacy_py.get("duration_seconds") or 0)+sum(r["duration_seconds"] for r in legacy_js),3)},
            "guards":{"tests":(report["guards"]["python"].get("tests") or 0)+(report["guards"]["javascript"].get("tests") or 0),
                "passed":(report["guards"]["python"].get("passed") or 0)+(report["guards"]["javascript"].get("pass") or 0),
                "duration_seconds":round(report["guards"]["python"].get("duration_seconds",0)+report["guards"]["javascript"]["duration_seconds"],3)}}
    args.report.parent.mkdir(parents=True,exist_ok=True)
    args.report.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding="utf-8")
    print(json.dumps({"report":str(args.report),"success":report["success"],"guards_ok":guards_ok,"existing_python":{k:report["existing"].get("python",{}).get(k) for k in ("tests","passed","failed","errors","skipped")}}))
    return 0 if report["success"] else 1


if __name__=="__main__": raise SystemExit(main())
