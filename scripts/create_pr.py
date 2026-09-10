from __future__ import annotations

"""Create a GitHub PR using stored git credentials for github.com.

Usage:
  python scripts/create_pr.py --title "feat: ..." --body-file pr_body.md --head feat/x --base main
"""

import argparse
import json
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

REPO = "MagicShawn/unify-llm"


def github_token() -> tuple[str, str]:
    """Return (username, token) from git credential helper."""
    proc = subprocess.run(
        ["git", "credential", "fill"],
        input="protocol=https\nhost=github.com\n\n",
        capture_output=True,
        text=True,
        check=False,
    )
    user = password = ""
    for line in proc.stdout.splitlines():
        if line.startswith("username="):
            user = line.split("=", 1)[1]
        elif line.startswith("password="):
            password = line.split("=", 1)[1]
    if not password:
        raise SystemExit("No GitHub credential found in git credential helper")
    return user, password


def api(method: str, path: str, token: str, body: dict | None = None) -> dict:
    url = f"https://api.github.com{path}"
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Authorization", f"Bearer {token}")
    req.add_header("Accept", "application/vnd.github+json")
    req.add_header("X-GitHub-Api-Version", "2022-11-28")
    if body is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        detail = e.read().decode(errors="replace")
        raise SystemExit(f"GitHub API {method} {path} failed: {e.code}\n{detail}") from e


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--title", required=True)
    p.add_argument("--body", default="")
    p.add_argument("--body-file", default="")
    p.add_argument("--head", required=True)
    p.add_argument("--base", default="main")
    args = p.parse_args()

    body = args.body
    if args.body_file:
        body = Path(args.body_file).read_text(encoding="utf-8")
    if not body:
        body = "Automated PR from Unify LLM iteration loop."

    _, token = github_token()
    pr = api(
        "POST",
        f"/repos/{REPO}/pulls",
        token,
        {
            "title": args.title,
            "head": args.head,
            "base": args.base,
            "body": body,
            "maintainer_can_modify": True,
        },
    )
    print(pr.get("html_url") or pr.get("url"))


if __name__ == "__main__":
    main()
