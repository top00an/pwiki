#!/usr/bin/python3
"""Helpers for .github/workflows/release.yml. Standard library only; tested by tests/test_ci_release.py.

notes TAG            Check that TAG (vX.Y.Z) matches pwikilib VERSION and print that version's CHANGELOG section body
                     (heading line removed), the same shape as the hand-made v0.2.0 release.
notion-payload       Print the Notion POST /v1/pages body for a failed run. Inputs come from the environment so no value
                     is ever pasted into shell or JSON text: NOTION_DATABASE_ID, REPO, REF, RUN_URL, ACTOR, COMMIT_MSG,
                     and optionally SUMMARY_FILE (failed jobs/steps and log excerpts, collected by the workflow).
log-excerpt          Read a job log on stdin and print the lines of the step(s) that failed.

Exit codes: 0 ok, 1 check failed, 2 usage error.
"""
import json
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from pwikilib.update import _CL_HEAD_RX, _VER_RX, vtuple  # noqa: E402

TEXT_LIMIT = 2000  # Notion rich_text content limit per text object


def changelog_section(text, version):
    """Body of the '## [version]' section (without the heading line), or None."""
    heads = list(_CL_HEAD_RX.finditer(text or ""))
    for i, m in enumerate(heads):
        if vtuple(m.group(1)) == vtuple(version):
            end = heads[i + 1].start() if i + 1 < len(heads) else len(text)
            return text[m.end():end].strip("\n").rstrip() + "\n"
    return None


def release_notes(tag, init_text, changelog_text):
    """(notes, error). The tag must be vX.Y.Z and equal VERSION; the CHANGELOG must have a non-empty section for it."""
    if not tag.startswith("v") or not tag[1:] or not all(p.isdigit() for p in tag[1:].split(".")):
        return None, "태그 형식이 vX.Y.Z 가 아니다: %s" % tag
    m = _VER_RX.search(init_text or "")
    if not m:
        return None, "pwikilib/__init__.py 에서 VERSION 을 찾지 못했다"
    if m.group(1) != tag[1:]:
        return None, "태그 %s 와 VERSION %s 가 다르다" % (tag, m.group(1))
    body = changelog_section(changelog_text, m.group(1))
    if not body or not body.strip():
        return None, "CHANGELOG.md 에 [%s] 절이 없거나 비어 있다" % m.group(1)
    return body, None


def failed_step_excerpt(log, max_lines=60):
    """From a job log, the lines of each step that ended in ##[error] (from its '##[group]Run' line), timestamps removed.
    Falls back to the last max_lines lines when no error marker is found."""
    lines = [re.sub(r"\x1b\[[0-9;]*m", "", re.sub(r"^\d{4}-\d\d-\d\dT[\d:.]+Z ", "", ln))
             for ln in (log or "").splitlines()]
    out, step = [], []
    for ln in lines:
        if ln.startswith("##[group]Run "):
            step = [ln[len("##[group]"):]]
        elif ln.startswith("##[error]"):
            out.extend(step[-max_lines:] + [ln[len("##[error]"):]])
            step = []
        elif not ln.startswith(("##[group]", "##[endgroup]")):
            step.append(ln)
    return "\n".join(out if out else lines[-max_lines:])


def _chunks(s, limit=TEXT_LIMIT):
    s = s or ""
    return [s[i:i + limit] for i in range(0, len(s), limit)] or [""]


def _text(s):
    return [{"type": "text", "text": {"content": c}} for c in _chunks(s)]


def notion_payload(env, summary=""):
    repo, ref, run_url = env.get("REPO", ""), env.get("REF", ""), env.get("RUN_URL", "")
    commit = " ".join((env.get("COMMIT_MSG") or "").split())
    link = {"type": "text", "text": {"content": run_url}}
    if run_url:
        link["text"]["link"] = {"url": run_url}
    children = [
        {"object": "block", "type": "heading_3", "heading_3": {"rich_text": _text("상세 정보")}},
        {"object": "block", "type": "bulleted_list_item", "bulleted_list_item": {"rich_text": _text("브랜치/태그: " + ref)}},
        {"object": "block", "type": "bulleted_list_item", "bulleted_list_item": {"rich_text": _text("커밋: " + commit)}},
        {"object": "block", "type": "bulleted_list_item", "bulleted_list_item": {"rich_text": [
            {"type": "text", "text": {"content": "Actions 로그: "}}, link]}},
    ]
    if summary.strip():
        children.append({"object": "block", "type": "heading_3", "heading_3": {"rich_text": _text("에러 요약")}})
        # A code block holds at most 100 rich_text items; keep the tail, which is where the error is.
        children.append({"object": "block", "type": "code",
                         "code": {"language": "plain text", "rich_text": _text(summary[-TEXT_LIMIT * 50:])}})
    return {
        "parent": {"database_id": env.get("NOTION_DATABASE_ID", "")},
        "properties": {
            "이슈명": {"title": _text(("빌드/릴리즈 실패: %s (%s)" % (repo, ref))[:TEXT_LIMIT])},
            "상태": {"status": {"name": "진행 전"}},
            "우선순위": {"select": {"name": "긴급"}},
            "담당자/커밋자": {"rich_text": _text(env.get("ACTOR", ""))},
            "실패링크": {"url": run_url or None},
        },
        "children": children,
    }


def main(argv):
    if len(argv) == 2 and argv[0] == "notes":
        with open(os.path.join(ROOT, "pwikilib", "__init__.py"), encoding="utf-8") as fh:
            init_text = fh.read()
        with open(os.path.join(ROOT, "CHANGELOG.md"), encoding="utf-8") as fh:
            changelog_text = fh.read()
        notes, err = release_notes(argv[1], init_text, changelog_text)
        if err:
            print(err, file=sys.stderr)
            return 1
        sys.stdout.write(notes)
        return 0
    if len(argv) == 1 and argv[0] == "log-excerpt":
        sys.stdout.write(failed_step_excerpt(sys.stdin.read()) + "\n")
        return 0
    if len(argv) == 1 and argv[0] == "notion-payload":
        summary = ""
        path = os.environ.get("SUMMARY_FILE")
        if path and os.path.isfile(path):
            with open(path, encoding="utf-8", errors="replace") as fh:
                summary = fh.read()
        json.dump(notion_payload(os.environ, summary), sys.stdout, ensure_ascii=False)
        return 0
    print(__doc__.strip(), file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
