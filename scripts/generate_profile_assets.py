#!/usr/bin/env python3
"""Refresh profile/stats.svg and profile/top-langs.svg from the public GitHub API.

Uses the token in GH_TOKEN (the Actions GITHUB_TOKEN in CI). The previous
workflow depended on a classic PAT in PAT_TOKEN; that secret now returns
401 Bad credentials and failed the job before any card was written.
"""

from __future__ import annotations

import html
import json
import math
import os
import subprocess
import sys
from pathlib import Path

USERNAME = os.environ.get("PROFILE_USER", "nischaysharma-me")
ROOT = Path(__file__).resolve().parents[1]
PROFILE = ROOT / "profile"

BG = "161616"
CORAL = "E57373"
TEXT = "ffffff"
BAR_WIDTH = 250

STATS_QUERY = """
query($login: String!, $after: String) {
  user(login: $login) {
    name
    commits: contributionsCollection {
      totalCommitContributions
      restrictedContributionsCount
    }
    reviews: contributionsCollection {
      totalPullRequestReviewContributions
    }
    repositoriesContributedTo(
      first: 1
      contributionTypes: [COMMIT, ISSUE, PULL_REQUEST, REPOSITORY]
    ) {
      totalCount
    }
    pullRequests(first: 1) { totalCount }
    openIssues: issues(states: OPEN) { totalCount }
    closedIssues: issues(states: CLOSED) { totalCount }
    followers { totalCount }
    repositories(
      first: 100
      ownerAffiliations: OWNER
      orderBy: {direction: DESC, field: STARGAZERS}
      after: $after
    ) {
      totalCount
      pageInfo { hasNextPage endCursor }
      nodes {
        name
        isFork
        stargazerCount
        languages(first: 10, orderBy: {field: SIZE, direction: DESC}) {
          edges { size node { name color } }
        }
      }
    }
  }
}
"""

PUBLIC_COMMITS_QUERY = """
query($login: String!, $after: String) {
  user(login: $login) {
    name
    commits: contributionsCollection {
      totalCommitContributions
    }
    reviews: contributionsCollection {
      totalPullRequestReviewContributions
    }
    repositoriesContributedTo(
      first: 1
      contributionTypes: [COMMIT, ISSUE, PULL_REQUEST, REPOSITORY]
    ) {
      totalCount
    }
    pullRequests(first: 1) { totalCount }
    openIssues: issues(states: OPEN) { totalCount }
    closedIssues: issues(states: CLOSED) { totalCount }
    followers { totalCount }
    repositories(
      first: 100
      ownerAffiliations: OWNER
      orderBy: {direction: DESC, field: STARGAZERS}
      after: $after
    ) {
      totalCount
      pageInfo { hasNextPage endCursor }
      nodes {
        name
        isFork
        stargazerCount
        languages(first: 10, orderBy: {field: SIZE, direction: DESC}) {
          edges { size node { name color } }
        }
      }
    }
  }
}
"""


def gh_graphql(query: str, variables: dict) -> dict:
    payload = json.dumps({"query": query, "variables": variables})
    result = subprocess.run(
        ["gh", "api", "graphql", "--input", "-"],
        input=payload,
        text=True,
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or result.stdout.strip() or "gh api graphql failed")
    data = json.loads(result.stdout)
    if data.get("errors"):
        messages = "; ".join(error.get("message", "unknown") for error in data["errors"])
        raise RuntimeError(messages)
    return data["data"]


def fetch_user() -> dict:
    repos: list[dict] = []
    user = None
    after = None
    query = STATS_QUERY
    while True:
        try:
            data = gh_graphql(query, {"login": USERNAME, "after": after})
        except RuntimeError as exc:
            if query is STATS_QUERY and after is None:
                print(f"Private contribution fields unavailable ({exc}); using public counts.", file=sys.stderr)
                query = PUBLIC_COMMITS_QUERY
                continue
            raise
        page_user = data["user"]
        if page_user is None:
            raise RuntimeError(f"GitHub user {USERNAME} was not found")
        if user is None:
            user = page_user
        page = page_user["repositories"]
        repos.extend(page["nodes"])
        if not page["pageInfo"]["hasNextPage"]:
            break
        after = page["pageInfo"]["endCursor"]
    assert user is not None
    user["repositories"]["nodes"] = repos
    return user


def exponential_cdf(x: float) -> float:
    return 1 - 2 ** (-x)


def log_normal_cdf(x: float) -> float:
    return x / (1 + x)


def calculate_rank(commits: int, prs: int, issues: int, reviews: int, stars: int, followers: int) -> tuple[str, float]:
    """Same rank curve as anuraghazra/github-readme-stats."""
    weights = {
        "commits": (250, 2),
        "prs": (50, 3),
        "issues": (25, 1),
        "reviews": (2, 1),
        "stars": (50, 4),
        "followers": (10, 1),
    }
    values = {
        "commits": commits,
        "prs": prs,
        "issues": issues,
        "reviews": reviews,
        "stars": stars,
        "followers": followers,
    }
    total_weight = sum(weight for _, weight in weights.values())
    score = 0.0
    for key, (median, weight) in weights.items():
        cdf = log_normal_cdf if key in {"stars", "followers"} else exponential_cdf
        score += weight * cdf(values[key] / median)
    rank = 1 - score / total_weight
    percentile = rank * 100
    levels = ["S", "A+", "A", "A-", "B+", "B", "B-", "C+", "C"]
    thresholds = [1, 12.5, 25, 37.5, 50, 62.5, 75, 87.5, 100]
    level = "C"
    for threshold, name in zip(thresholds, levels):
        if percentile <= threshold:
            level = name
            break
    return level, percentile


def language_totals(repos: list[dict]) -> list[dict]:
    totals: dict[str, dict] = {}
    for repo in repos:
        if repo.get("isFork"):
            continue
        for edge in repo.get("languages", {}).get("edges", []):
            name = edge["node"]["name"]
            bucket = totals.setdefault(
                name,
                {"name": name, "color": edge["node"]["color"] or "#8b949e", "size": 0},
            )
            bucket["size"] += edge["size"]
    ranked = sorted(totals.values(), key=lambda item: item["size"], reverse=True)
    return ranked[:6]


def esc(value: object) -> str:
    return html.escape(str(value), quote=True)


def render_stats(user: dict) -> str:
    commits = user["commits"]["totalCommitContributions"] + user["commits"].get("restrictedContributionsCount", 0)
    prs = user["pullRequests"]["totalCount"]
    issues = user["openIssues"]["totalCount"] + user["closedIssues"]["totalCount"]
    reviews = user["reviews"]["totalPullRequestReviewContributions"]
    followers = user["followers"]["totalCount"]
    stars = sum(repo["stargazerCount"] for repo in user["repositories"]["nodes"])
    contributed = user["repositoriesContributedTo"]["totalCount"]
    name = user["name"] or USERNAME
    level, percentile = calculate_rank(commits, prs, issues, reviews, stars, followers)
    circumference = 2 * math.pi * 40
    progress = max(0.0, min(1.0, 1 - percentile / 100))
    dash_offset = circumference * (1 - progress)

    rows = [
        ("Total Stars Earned:", stars, "M8 .25a.75.75 0 01.673.418l1.882 3.815 4.21.612a.75.75 0 01.416 1.279l-3.046 2.97.719 4.192a.75.75 0 01-1.088.791L8 12.347l-3.766 1.98a.75.75 0 01-1.088-.79l.72-4.194L.818 6.374a.75.75 0 01.416-1.28l4.21-.611L7.327.668A.75.75 0 018 .25z"),
        ("Total Commits (last year):", commits, "M1.643 3.143L.427 1.927A.25.25 0 000 2.104V5.75c0 .138.112.25.25.25h3.646a.25.25 0 00.177-.427L2.715 4.215a6.5 6.5 0 11-1.18 4.458.75.75 0 10-1.493.154 8.001 8.001 0 101.6-5.684zM7.75 4a.75.75 0 01.75.75v2.992l2.028.812a.75.75 0 01-.557 1.392l-2.5-1A.75.75 0 017 8.25v-3.5A.75.75 0 017.75 4z"),
        ("Total PRs:", prs, "M7.177 3.073L9.573.677A.25.25 0 0110 .854v4.792a.25.25 0 01-.427.177L7.177 3.427a.25.25 0 010-.354zM3.75 2.5a.75.75 0 100 1.5.75.75 0 000-1.5zm-2.25.75a2.25 2.25 0 113 2.122v5.256a2.251 2.251 0 11-1.5 0V5.372A2.25 2.25 0 011.5 3.25zM11 2.5h-1V4h1a1 1 0 011 1v5.628a2.251 2.251 0 101.5 0V5A2.5 2.5 0 0011 2.5zm1 10.25a.75.75 0 111.5 0 .75.75 0 01-1.5 0zM3.75 12a.75.75 0 100 1.5.75.75 0 000-1.5z"),
        ("Total Issues:", issues, "M8 1.5a6.5 6.5 0 100 13 6.5 6.5 0 000-13zM0 8a8 8 0 1116 0A8 8 0 010 8zm9 3a1 1 0 11-2 0 1 1 0 012 0zm-.25-6.25a.75.75 0 00-1.5 0v3.5a.75.75 0 001.5 0v-3.5z"),
        ("Contributed to (last year):", contributed, "M2 2.5A2.5 2.5 0 014.5 0h8.75a.75.75 0 01.75.75v12.5a.75.75 0 01-.75.75h-2.5a.75.75 0 110-1.5h1.75v-2h-8a1 1 0 00-.714 1.7.75.75 0 01-1.072 1.05A2.495 2.495 0 012 11.5v-9zm10.5-1V9h-8c-.356 0-.694.074-1 .208V2.5a1 1 0 011-1h8zM5 12.25v3.25a.25.25 0 00.4.2l1.45-1.087a.25.25 0 01.3 0L8.6 15.7a.25.25 0 00.4-.2v-3.25a.25.25 0 00-.25-.25h-3.5a.25.25 0 00-.25.25z"),
    ]
    row_markup = []
    for index, (label, value, path) in enumerate(rows):
        row_markup.append(
            f"""<g transform="translate(0, {index * 25})">
      <g transform="translate(25, 0)">
        <svg class="icon" viewBox="0 0 16 16" width="16" height="16">
          <path fill-rule="evenodd" d="{path}"/>
        </svg>
        <text class="stat bold" x="22" y="12.5">{esc(label)}</text>
        <text class="stat bold" x="330" y="12.5" text-anchor="end">{value:,}</text>
      </g>
      </g>"""
        )

    return f"""<svg width="467" height="195" viewBox="0 0 467 195" fill="none" xmlns="http://www.w3.org/2000/svg" role="img" aria-label="{esc(name)}'s GitHub stats">
  <title>{esc(name)}&#39;s GitHub Stats, Rank: {esc(level)}</title>
  <style>
    .header {{ font: 600 18px 'Segoe UI', Ubuntu, Sans-Serif; fill: #{CORAL}; }}
    .stat {{ font: 600 14px 'Segoe UI', Ubuntu, Sans-Serif; fill: #{TEXT}; }}
    .bold {{ font-weight: 700 }}
    .icon {{ fill: #{CORAL}; }}
    .rank-text {{ font: 800 24px 'Segoe UI', Ubuntu, Sans-Serif; fill: #{TEXT}; }}
    .rank-circle-rim {{ stroke: #{CORAL}; fill: none; stroke-width: 6; opacity: 0.2; }}
    .rank-circle {{
      stroke: #{CORAL}; fill: none; stroke-width: 6; stroke-linecap: round; opacity: 0.8;
      stroke-dasharray: {circumference:.3f}; stroke-dashoffset: {dash_offset:.3f};
      transform-origin: -10px 8px; transform: rotate(-90deg);
    }}
  </style>
  <rect x="0.5" y="0.5" rx="4.5" width="466" height="194" fill="#{BG}" stroke="#{CORAL}"/>
  <g transform="translate(25, 35)">
    <text class="header" x="0" y="0">{esc(name)}&#39;s GitHub Stats</text>
  </g>
  <g data-testid="rank-circle" transform="translate(400, 102)">
    <circle class="rank-circle-rim" cx="-10" cy="8" r="40"/>
    <circle class="rank-circle" cx="-10" cy="8" r="40"/>
    <text class="rank-text" x="-10" y="8" text-anchor="middle" dominant-baseline="central">{esc(level)}</text>
  </g>
  <g transform="translate(0, 55)">
    {''.join(row_markup)}
  </g>
</svg>
"""


def render_languages(langs: list[dict]) -> str:
    total = sum(item["size"] for item in langs) or 1
    all_size = total
    # Percentages are shares of the displayed languages so the bar and labels agree.
    segments = []
    cursor = 0.0
    for item in langs:
        width = BAR_WIDTH * item["size"] / all_size
        segments.append((item, cursor, width))
        cursor += width
    # Absorb rounding error into the last segment so the bar has no gap or overlap.
    if segments:
        item, start, _width = segments[-1]
        segments[-1] = (item, start, BAR_WIDTH - start)

    bars = []
    for item, start, width in segments:
        if width <= 0:
            continue
        bars.append(
            f'<rect x="{start:.2f}" y="0" width="{width:.2f}" height="8" fill="{esc(item["color"])}"/>'
        )

    columns = [[], []]
    for index, item in enumerate(langs):
        percent = 100 * item["size"] / all_size
        columns[index // 3].append(
            f"""<g transform="translate(0, {(index % 3) * 25})">
        <circle cx="5" cy="6" r="5" fill="{esc(item["color"])}"/>
        <text class="lang-name" x="16" y="10">{esc(item["name"])} {percent:.2f}%</text>
      </g>"""
        )

    return f"""<svg width="340" height="165" viewBox="0 0 340 165" fill="none" xmlns="http://www.w3.org/2000/svg" role="img" aria-label="Most used languages">
  <title>Most Used Languages</title>
  <style>
    .header {{ font: 600 18px 'Segoe UI', Ubuntu, Sans-Serif; fill: #{CORAL}; }}
    .lang-name {{ font: 400 11px 'Segoe UI', Ubuntu, Sans-Serif; fill: #{TEXT}; }}
  </style>
  <rect x="0.5" y="0.5" rx="4.5" width="339" height="164" fill="#{BG}" stroke="#{CORAL}"/>
  <g transform="translate(25, 35)">
    <text class="header" x="0" y="0">Most Used Languages</text>
  </g>
  <g transform="translate(25, 55)">
    <rect x="0" y="0" width="{BAR_WIDTH}" height="8" rx="4" fill="#30363d"/>
    <svg x="0" y="0" width="{BAR_WIDTH}" height="8">
      <defs>
        <clipPath id="lang-bar"><rect x="0" y="0" width="{BAR_WIDTH}" height="8" rx="4"/></clipPath>
      </defs>
      <g clip-path="url(#lang-bar)">
        {''.join(bars)}
      </g>
    </svg>
  </g>
  <g transform="translate(25, 80)">{''.join(columns[0])}</g>
  <g transform="translate(175, 80)">{''.join(columns[1])}</g>
</svg>
"""


def main() -> None:
    if not os.environ.get("GH_TOKEN") and not os.environ.get("GITHUB_TOKEN"):
        print("GH_TOKEN or GITHUB_TOKEN is required.", file=sys.stderr)
        sys.exit(1)
    user = fetch_user()
    langs = language_totals(user["repositories"]["nodes"])
    if not langs:
        raise RuntimeError("No language data returned for " + USERNAME)
    PROFILE.mkdir(exist_ok=True)
    stats_path = PROFILE / "stats.svg"
    langs_path = PROFILE / "top-langs.svg"
    stats_path.write_text(render_stats(user), encoding="utf-8")
    langs_path.write_text(render_languages(langs), encoding="utf-8")
    print(f"Wrote {stats_path.relative_to(ROOT)} and {langs_path.relative_to(ROOT)}")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"Failed to generate profile assets: {exc}", file=sys.stderr)
        sys.exit(1)
