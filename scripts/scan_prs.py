"""scan_prs.py — scan upstream open PRs, print candidates. Usage: python scripts/scan_prs.py [N]"""

import json
import subprocess
import sys

REPO = "Aider-AI/aider"
PATCHED = {
    5974, 5967, 4987, 5950, 5964, 5966,  # 首批
    5947, 5565, 5939, 5945, 5915, 5952,  # 第二轮
    3756,  # 已评估，不采用
    5782, 5809,  # 第二轮审查后拒绝/暂缓
}


def ci_state(rollup):
    verdicts = [(c.get("conclusion") or c.get("state") or "?") for c in (rollup or [])]
    if not verdicts:
        return "no-checks"
    if all(v == "SUCCESS" for v in verdicts):
        return "green"
    if any(v in ("FAILURE", "FAILED") for v in verdicts):
        return "fail"
    return "pending"


def main():
    top = int(sys.argv[1]) if len(sys.argv) > 1 else 30
    r = subprocess.run(
        [
            "gh",
            "pr",
            "list",
            "--repo",
            REPO,
            "--state",
            "open",
            "--limit",
            "200",
            "--json",
            "number,title,updatedAt,additions,deletions,changedFiles,statusCheckRollup",
        ],
        capture_output=True,
        text=True,
    )
    if r.returncode != 0:
        sys.exit(r.stderr)
    prs = json.loads(r.stdout)
    prs.sort(key=lambda p: p["updatedAt"], reverse=True)
    n = 0
    for p in prs:
        if p["number"] in PATCHED or n >= top:
            continue
        n += 1
        c = ci_state(p.get("statusCheckRollup"))
        print(
            f"#{p['number']} [{p['updatedAt'][:10]}] {c:<9}"
            f" +{p['additions']}/-{p['deletions']} ({p.get('changedFiles', 0)}f)"
            f" {p['title'][:60]}"
        )
    print(f"shown {n} of {len(prs)} scanned (recently updated first)")


if __name__ == "__main__":
    main()
