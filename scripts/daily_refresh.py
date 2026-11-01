#!/usr/bin/env python3
"""Laptop Deal Radar - daily refresh: collect -> VERIFY -> deploy.

1. Run scripts/collect.py (fetch, value, bake index.html + data/deals.json).
2. Gate on node scripts/verify_site.js. A failed gate aborts BEFORE deploy, so a
   broken artifact never ships.
3. Deploy into the shallow gh-pages working clone, byte-comparing first: no
   change means no commit, no push, and a silent-ish report.

stdout IS the cron report (the job runs with no_agent=true).
Windows-safe: no `source`, no `rm`, absolute paths from __file__.
"""

import os
import re
import shutil
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PAGES_DIR = os.path.join(os.path.dirname(ROOT), "laptop-deal-radar-pages")
REPO_URL = "https://github.com/nwfella/laptop-deal-radar.git"
ARTIFACTS = ["index.html", os.path.join("data", "deals.json")]


def find_node():
    for cand in ("node", r"C:\Users\homee\AppData\Local\hermes\tools\node-26.7.0-win32-x64\node.exe"):
        try:
            subprocess.run([cand, "--version"], capture_output=True, timeout=30, check=True)
            return cand
        except (OSError, subprocess.SubprocessError):
            continue
    return None


def run(cmd, cwd=None, timeout=900):
    """Run a command with a hard, tree-killing timeout and a no-prompt git env.

    `subprocess.run(timeout=)` does NOT kill grandchildren on Windows: a hung
    `git push` whose credential helper is waiting on a GUI dialog leaves the pipe
    open, the parent blocks reading it, and the job runs until the scheduler's own
    cap (observed: 3600s, and one run that spanned three days). So the child is
    killed with `taskkill /T`, and git is told never to prompt in the first place.
    """
    env = os.environ.copy()
    env.pop("PYTHONPATH", None)
    env["GIT_TERMINAL_PROMPT"] = "0"   # a missing credential must fail fast
    env["GCM_INTERACTIVE"] = "never"   # Git Credential Manager: never open a dialog
    env["GIT_ASKPASS"] = ""            # no askpass popup
    env["SSH_ASKPASS"] = ""
    env["GIT_CONFIG_PARAMETERS"] = "'credential.interactive=false'"
    flags = 0
    if os.name == "nt":
        flags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    p = subprocess.Popen(cmd, cwd=cwd or ROOT, env=env, text=True,
                         stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                         creationflags=flags)
    try:
        out, err = p.communicate(timeout=timeout)
        return p.returncode, out or "", err or ""
    except subprocess.TimeoutExpired:
        if os.name == "nt":
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(p.pid)],
                           capture_output=True)
        else:
            p.kill()
        try:
            out, err = p.communicate(timeout=30)
        except subprocess.TimeoutExpired:
            out, err = "", ""
        return -1, out or "", ((err or "") + "\nTIMEOUT after %ss (process tree killed)"
                               % timeout).strip()


def git(*args, cwd=PAGES_DIR):
    return run(["git"] + list(args), cwd=cwd, timeout=120)


# a network-shaped git failure is worth retrying; an auth rejection is not
TRANSIENT_GIT = re.compile(
    r"could not resolve host|connection (?:timed out|reset|refused)|failed to connect"
    r"|network is unreachable|remote end hung up|early eof|rpc failed"
    r"|tls|ssl|timed out|timeout", re.I)


def git_push(retries=(0, 15, 45)):
    """Push gh-pages, retrying only transient network faults.

    The site is stale until this succeeds, so a transient failure here silently
    freezes the deploy for days - worth three attempts. Anything that looks like a
    credential problem must fail on the first attempt, loudly.
    """
    last = (-1, "", "")
    for i, wait in enumerate(retries):
        if wait:
            time.sleep(wait)
        rc, out, err = git("push", "origin", "gh-pages")
        if rc == 0:
            if i:
                out += "\n(push succeeded on attempt %d)" % (i + 1)
            return rc, out, err
        last = (rc, out, err)
        if not TRANSIENT_GIT.search(err or out or ""):
            break
    return last


def main():
    report = []
    hard = []

    # ---- 1. collect ----
    rc, out, err = run([sys.executable, os.path.join(ROOT, "scripts", "collect.py")])
    if rc != 0:
        print("laptop-deal-radar: COLLECT FAILED (exit %s)" % rc)
        print((err or out)[-1200:])
        return 1

    # keep only the report lines worth delivering
    for line in out.splitlines():
        if (line.startswith("[ok]") or line.startswith("[FAIL]") or line.startswith("[warn]")
                or line.startswith("cfg ") or line.startswith("live ")
                or line.startswith("scanned:") or line.startswith("filtered:")
                or line.startswith("ASK ") or line.startswith("baked:")
                or line.startswith("  cfg") or line.startswith("  live")
                or line.startswith("condition")):
            report.append(line)

    # ---- 2. verify gate ----
    node = find_node()
    if not node:
        print("laptop-deal-radar: node not found - cannot run the verify gate, refusing to deploy")
        return 1
    rc, vout, verr = run([node, os.path.join(ROOT, "scripts", "verify_site.js")])
    if rc != 0:
        print("laptop-deal-radar: VERIFY FAILED - artifact NOT deployed")
        print((vout or "")[-1500:])
        print((verr or "")[-400:])
        return 1
    m = re.search(r"VERIFY OK: (\d+) assertions passed", vout)
    report.append("verify: OK (%s assertions)" % (m.group(1) if m else "?"))

    # ---- 3. ensure the gh-pages clone exists ----
    if not os.path.isdir(os.path.join(PAGES_DIR, ".git")):
        rc, o, e = run(["git", "clone", "--branch", "gh-pages", "--depth", "1",
                        REPO_URL, PAGES_DIR], cwd=os.path.dirname(PAGES_DIR))
        if rc != 0:
            print("laptop-deal-radar: cannot clone gh-pages: %s" % (e or o)[:300])
            return 1

    rc, o, e = git("pull", "--ff-only", "origin", "gh-pages")
    if rc != 0:
        # a diverged or brand-new remote is not fatal - we overwrite with the bake
        report.append("warn: git pull skipped (%s)" % (e or o).strip()[:80])

    # ---- 4. compare, then deploy only on change ----
    changed = []
    for rel in ARTIFACTS:
        src = os.path.join(ROOT, rel)
        dst = os.path.join(PAGES_DIR, rel)
        if not os.path.exists(src):
            print("laptop-deal-radar: missing artifact %s" % rel)
            return 1
        with open(src, "rb") as fh:
            new = fh.read()
        old = None
        if os.path.exists(dst):
            with open(dst, "rb") as fh:
                old = fh.read()
        if new != old:
            changed.append(rel)

    if not changed:
        report.append("deploy: no change, nothing pushed")
        print("\n".join(report))
        return 0

    for rel in changed:
        dst = os.path.join(PAGES_DIR, rel)
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        shutil.copy2(os.path.join(ROOT, rel), dst)

    rc, o, e = git("add", "--", *ARTIFACTS)
    if rc != 0:
        print("laptop-deal-radar: git add failed: %s" % (e or o)[:300])
        return 1
    cfg = git("config", "user.email")[0] or git("config", "user.name")[0]
    if cfg != 0:
        report.append("warn: git identity may be unset for cron commits")

    rc, o, e = git("commit", "-m", "refresh: %s" % ", ".join(changed))
    if rc != 0:
        report.append("deploy: nothing to commit")
        print("\n".join(report))
        return 0
    rc, o, e = git_push()
    if rc != 0:
        # a failed push is a real failure: the site is stale
        print("laptop-deal-radar: PUSH FAILED - site is stale")
        print((e or o)[:600])
        return 1
    report.append("deploy: pushed %s" % ", ".join(changed))

    print("\n".join(report))
    return 0


if __name__ == "__main__":
    sys.exit(main())
