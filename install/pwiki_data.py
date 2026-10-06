"""Decide whether a pwiki data location (PWIKI_HOME, vault) belongs to pwiki, and delete only what pwiki created.

Called by install.sh and uninstall.sh. Standard library only.

Marker files: PWIKI_HOME/.pwiki-home, vault/.pwiki-vault (created by install.sh)

When a location counts as pwiki's
- Empty: missing, or has no non-hidden entries.
- Marker: the marker file exists.
- Old install (before marker files): PWIKI_HOME holds a pwiki DB (pwiki.db, confirmed by its table set). Entries a person put
  alongside are left alone (install does not mix in, removal does not delete them).
- No DB (e.g. a pre-placed secrets.local): every non-hidden entry has a name pwiki creates, logs/ holds only pwiki log
  names, and runs/ is empty. A file named pwiki.db that is not a pwiki DB makes it a person's folder (an empty DB counts by name).
  A vault has at least one page from the export records (exports in PWIKI_HOME/pwiki.db) exactly as recorded.
  The presence of _notes alone does not make it a pwiki vault.
Anything else is a person's folder. Install stops, and deletion deletes nothing.

What gets deleted (only what pwiki created)
- PWIKI_HOME: DB (-wal, -shm, -journal), secrets.local, secrets.harvested, ingest.lock, install.json, marker file,
  pwiki records in logs/ (ingest, install, collect, hook logs). Then only an empty logs/, runs/ and PWIKI_HOME are rmdir'ed.
- vault: pages in the export records (including pages a person edited), _notes/README.md if still the original intro text,
  marker file, *.pwiki-tmp left over from an export. Then only empty folders are rmdir'ed.

Usage. main() prints everything after the Korean marker line below on bad arguments, so keep that marker as is.
사용:
  pwiki_data.py check-install HOME_DIR VAULT_DIR
      0 accept, 1 stop (one-line reason)
  pwiki_data.py purge HOME_DIR VAULT_DIR USER_HOME REPO CLAUDE_DIR [--apply] [--dry-run]
      Without --apply, only reports what would be deleted (0 something to delete, 3 nothing). With --apply, deletes and reports
      the number of entries left.
"""
import hashlib
import os
import re
import sqlite3
import sys
import urllib.parse

HOME_MARK = ".pwiki-home"
VAULT_MARK = ".pwiki-vault"
HOME_FILES = {"pwiki.db", "pwiki.db-wal", "pwiki.db-shm", "pwiki.db-journal", "secrets.local", "secrets.harvested",
              "secrets.harvested.tmp", "ingest.lock", "install.json", "install.json.tmp", HOME_MARK}
HOME_DIRS = ("logs", "runs")
LOG_RE = re.compile(r"^(ingest\.log|install\.ingest\.txt|hook\.log(\.1)?|"
                    r"collect\.(log(\.1)?|status\.json|last\.txt|launchd\.log))(\.tmp\d*)?$")
PAGE_TOPS = ("days", "projects", "cards")
# pwiki DB 로 볼 표(pwikilib/db.py SCHEMA 의 일부). 이것이 다 있어야 pwiki DB 다
DB_TABLES = {"files", "events", "cards", "sessions", "compact_sections", "fts_map", "exports", "runs", "state"}


def inside(a, b):
    return a == b or a.startswith(b.rstrip("/") + "/")


def visible(p):
    try:
        return [x for x in os.listdir(p) if not x.startswith(".")]
    except OSError:
        return []


def is_file(p):
    return os.path.islink(p) or os.path.isfile(p)


def count_files(top):
    """top 아래 파일(폴더가 아닌 항목) 수와 그중 숨김 경로 안의 수. 링크는 따라가지 않는다."""
    n = hidden = 0
    for dp, dn, fn in os.walk(top):
        rel = os.path.relpath(dp, top)
        hid_dir = rel != "." and any(part.startswith(".") for part in rel.split(os.sep))
        for f in fn + [d for d in dn if os.path.islink(os.path.join(dp, d))]:
            n += 1
            if hid_dir or f.startswith("."):
                hidden += 1
    return n, hidden


def sha1(p):
    h = hashlib.sha1()
    with open(p, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


# ---- PWIKI_HOME ----------------------------------------------------------------------
def ro_connect(db):
    """DB 를 읽기 전용으로 연다(쓰지 않는다). WAL DB 는 -wal 이 없으면 mode=ro 로 못 여므로, 그때는 immutable 로 연다
    (-wal 이 없으면 DB 파일이 전부다)."""
    q_ = urllib.parse.quote(db)
    try:
        con = sqlite3.connect("file:%s?mode=ro" % q_, uri=True)
        con.execute("SELECT count(*) FROM sqlite_master").fetchone()
        return con
    except sqlite3.Error:
        if os.path.exists(db + "-wal"):
            raise
    return sqlite3.connect("file:%s?mode=ro&immutable=1" % q_, uri=True)


def db_kind(h):
    """PWIKI_HOME/pwiki.db 의 정체: 'none'(없음)·'empty'(표 없는 빈 DB)·'pwiki'(pwiki 표가 다 있음)·'other'."""
    db = os.path.join(h, "pwiki.db")
    if not os.path.lexists(db):
        return "none"
    if os.path.islink(db) or not os.path.isfile(db):
        return "other"
    if os.path.getsize(db) == 0:
        return "empty"
    try:
        con = ro_connect(db)
        try:
            names = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        finally:
            con.close()
    except sqlite3.Error:
        return "other"
    if not names:
        return "empty"
    return "pwiki" if DB_TABLES <= names else "other"


def home_state(h):
    """('absent'|'empty'|'marked'|'legacy'|'foreign', 낯선 항목 수). legacy 에서 낯선 항목 수는 남길 사람 항목 수다."""
    if not os.path.lexists(h):
        return "absent", 0
    if not os.path.isdir(h):
        return "foreign", 1
    if is_file(os.path.join(h, HOME_MARK)):
        return "marked", 0
    vis = visible(h)
    if not vis:
        return "empty", 0
    foreign = [x for x in vis if x not in HOME_FILES and x not in HOME_DIRS]
    kind = db_kind(h)
    if kind == "pwiki":
        return "legacy", len(foreign)  # 옛 설치. 곁의 사람 항목(백업 폴더 등)은 건드리지 않는다
    if kind == "other":
        foreign.append("pwiki.db")
    # DB 없이 이름으로만 볼 때는 logs/·runs/ 안도 본다(사람의 logs/·runs/ 폴더를 pwiki 것으로 오인하지 않게)
    for d, ok in (("logs", LOG_RE.match), ("runs", lambda x: False)):
        p = os.path.join(h, d)
        if not os.path.lexists(p):
            continue
        if os.path.islink(p) or not os.path.isdir(p):
            foreign.append(d)
            continue
        foreign += [d + "/" + x for x in visible(p) if not ok(x)]
    if not foreign:
        return "legacy", 0
    return "foreign", len(foreign)


def home_targets(h):
    """지울 파일 목록(pwiki 가 만든 이름만)."""
    out = []
    for name in sorted(HOME_FILES):
        p = os.path.join(h, name)
        if is_file(p):
            out.append(p)
    logs = os.path.join(h, "logs")
    if os.path.isdir(logs) and not os.path.islink(logs):
        for name in sorted(os.listdir(logs)):
            p = os.path.join(logs, name)
            if LOG_RE.match(name) and is_file(p):
                out.append(p)
    return out


# ---- vault ---------------------------------------------------------------------------
def export_records(home):
    """PWIKI_HOME/pwiki.db 의 export 기록 [(상대 경로, sha1)]. 못 읽으면 None."""
    db = os.path.join(home, "pwiki.db")
    if not os.path.isfile(db):
        return None
    try:
        con = ro_connect(db)
        try:
            return [(str(p), str(s)) for p, s in con.execute("SELECT path, sha1 FROM exports")]
        finally:
            con.close()
    except sqlite3.Error:
        return None


def page_path(vault, rel):
    """export 기록의 상대 경로를 vault 안의 절대 경로로. pwiki 페이지 자리가 아니면 None."""
    if not rel or os.path.isabs(rel) or "\\" in rel:
        return None
    norm = os.path.normpath(rel)
    parts = norm.split(os.sep)
    if ".." in parts or not (norm == "index.md" or (parts[0] in PAGE_TOPS and len(parts) >= 2)):
        return None
    ap = os.path.join(vault, norm)
    if not inside(os.path.realpath(os.path.dirname(ap)), os.path.realpath(vault)):
        return None  # 링크로 vault 밖을 가리키는 폴더는 건드리지 않는다
    return ap


def vault_state(v, records):
    if not os.path.lexists(v):
        return "absent", 0
    if not os.path.isdir(v):
        return "foreign", 1
    if is_file(os.path.join(v, VAULT_MARK)):
        return "marked", 0
    vis = visible(v)
    if not vis:
        return "empty", 0
    for rel, s in records or ():
        ap = page_path(v, rel)
        if ap and os.path.isfile(ap) and not os.path.islink(ap):
            try:
                if sha1(ap) == s:
                    return "legacy", 0
            except OSError:
                pass
    return "foreign", len(vis)


def notes_readme_text(repo):
    try:
        sys.path.insert(0, repo)
        from pwikilib.export import NOTES_README
        return NOTES_README.encode("utf-8")
    except Exception:
        return None
    finally:
        if sys.path and sys.path[0] == repo:
            sys.path.pop(0)


def vault_targets(v, records, repo):
    """(페이지 목록, 그중 사람이 고친 수, 안내·표시·임시 파일 목록)"""
    pages, edited = [], 0
    for rel, s in records or ():
        ap = page_path(v, rel)
        if not ap or not is_file(ap) or ap in pages:
            continue
        pages.append(ap)
        try:
            if os.path.islink(ap) or sha1(ap) != s:
                edited += 1
        except OSError:
            edited += 1
    extra = []
    mark = os.path.join(v, VAULT_MARK)
    if is_file(mark):
        extra.append(mark)
    readme = os.path.join(v, "_notes", "README.md")
    want = notes_readme_text(repo)
    if want is not None and os.path.isfile(readme) and not os.path.islink(readme):
        with open(readme, "rb") as fh:
            if fh.read() == want:
                extra.append(readme)
    for top in [v] + [os.path.join(v, t) for t in PAGE_TOPS]:
        if not os.path.isdir(top) or os.path.islink(top):
            continue
        walk = os.walk(top) if top != v else [(v, [], os.listdir(v))]
        for dp, dn, fn in walk:
            for f in fn:
                p = os.path.join(dp, f)
                if f.endswith(".pwiki-tmp") and is_file(p) and p not in pages:
                    extra.append(p)
    return pages, edited, extra


# ---- 지우기 -----------------------------------------------------------------------------
def guard(p, user_home, repo, claude):
    """지울 수 있는 자리인가. 안 되면 이유."""
    if not os.path.isabs(p):
        return "절대 경로가 아니다"
    if not os.path.isdir(p):
        return "폴더가 없다"
    rp, rh, rr, rc = (os.path.realpath(x) for x in (p, user_home, repo, claude))
    if rp == "/" or inside(rh, rp):
        return "/ 또는 HOME 이거나 HOME 을 품은 폴더다"
    if inside(rp, rr) or inside(rr, rp):
        return "pwiki 저장소(%s)와 겹친다" % rr
    if inside(rp, rc) or inside(rc, rp):
        return "Claude Code 기록 폴더와 겹친다"
    if os.path.lexists(os.path.join(rp, ".git")):
        return "git 저장소다"
    return None


def q(s):
    if s and re.fullmatch(r"[A-Za-z0-9_@%+=:,./-]+", s):
        return s
    return "'%s'" % s.replace("'", "'\\''")


def remove_files(files, dry):
    if dry:
        return
    for p in files:
        try:
            os.unlink(p)
        except FileNotFoundError:
            pass


def rmdir_empty(dirs, dry):
    """깊은 것부터 빈 폴더만 지운다. 지운 폴더 목록."""
    done = []
    for d in sorted(set(dirs), key=lambda x: x.count(os.sep), reverse=True):
        if dry or os.path.islink(d) or not os.path.isdir(d):
            continue
        try:
            if not os.listdir(d):
                os.rmdir(d)
                done.append(d)
        except OSError:
            pass
    return done


def ancestors(p, stop):
    out = []
    d = os.path.dirname(p)
    while inside(d, stop) and d != stop:
        out.append(d)
        d = os.path.dirname(d)
    return out


def purge(home, vault, user_home, repo, claude, apply, dry):
    say = print
    home_ok = vault_ok = False
    records = export_records(home)
    # vault 먼저 본다(export 기록이 PWIKI_HOME 의 DB 에 있다)
    why = guard(vault, user_home, repo, claude)
    vst = None
    if why is None:
        vst, _ = vault_state(vault, records)
        if vst == "foreign":
            why = "pwiki 표시(%s)도 export 기록에 맞는 페이지도 없다(사람 폴더로 본다)" % VAULT_MARK
    if why is None:
        pages, edited, extra = vault_targets(vault, records, repo)
        total, _ = count_files(vault)
        keep = total - len(pages) - len(extra)
        vault_ok = bool(pages or extra or not visible(vault))
        if not apply:
            say("- vault %s: pwiki 페이지 %d개(export 기록 기준, 사람이 고친 페이지 %d개 포함)와 안내·표시 파일 %d개를 지운다."
                % (vault, len(pages), edited, len(extra)))
            if records is None:
                say("  export 기록(DB)을 읽지 못해 페이지를 가려내지 못했다. 페이지는 남긴다")
            say("  pwiki 가 만들지 않은 파일 %d개는 남긴다. 비는 폴더만 지운다" % keep)
        else:
            remove_files(pages + extra, dry)
            say("+ (지우기) %s 의 pwiki 페이지 %d개 · 안내·표시 파일 %d개" % (q(vault), len(pages), len(extra)))
            dirs = [vault, os.path.join(vault, "_notes")]
            for p in pages + extra:
                dirs += ancestors(p, vault)
            gone = rmdir_empty(dirs, dry)
            say("+ rmdir 빈 폴더 %s" % ("(비는 것만)" if dry else "%d개" % len(gone)))
            if not dry and os.path.isdir(vault):
                left, hid = count_files(vault)
                say("남김: %s 에 pwiki 가 만들지 않은 파일 %d개(숨김 %d개 포함). 필요하면 직접 지운다" % (vault, left, hid))
            elif not dry:
                say("vault 폴더도 비어 지웠다")
    else:
        say("- vault %s 는 지우지 않는다: %s" % (vault, why))

    why = guard(home, user_home, repo, claude)
    if why is None:
        hst, nf = home_state(home)
        if hst == "foreign":
            why = "pwiki 표시(%s)가 없고 pwiki 가 아닌 항목이 %d개 있다(사람 폴더로 본다)" % (HOME_MARK, nf)
    if why is None:
        files = home_targets(home)
        total, _ = count_files(home)
        home_ok = True
        if not apply:
            say("- PWIKI_HOME %s: pwiki 파일 %d개(DB·secrets.local·수확값·잠금·설치 기록·로그)를 지운다."
                % (home, len(files)))
            say("  그 밖의 파일 %d개는 남긴다. 비는 폴더만 지운다" % (total - len(files)))
        else:
            for p in files:
                say("+ rm -f %s" % q(p))
            remove_files(files, dry)
            gone = rmdir_empty([os.path.join(home, d) for d in HOME_DIRS] + [home], dry)
            for d in gone:
                say("+ rmdir %s" % q(d))
            if not dry and os.path.isdir(home):
                left, hid = count_files(home)
                say("남김: %s 에 pwiki 가 만들지 않은 파일 %d개(숨김 %d개 포함). 필요하면 직접 지운다" % (home, left, hid))
    else:
        say("- PWIKI_HOME %s 는 지우지 않는다: %s" % (home, why))
    return 0 if (home_ok or vault_ok) else 3


def check_install(home, vault):
    """멈출 이유가 있으면 그 한 줄을 내고 1. 받으면 0(알릴 것이 있으면 한 줄)."""
    hst, nf = home_state(home)
    if hst == "foreign":
        print("PWIKI_HOME 자리(%s)에 pwiki 가 아닌 항목이 %d개 있다. 빈 폴더나 새 하위 폴더를 PWIKI_HOME 으로 준다"
              % (home, nf))
        return 1
    vst, nv = vault_state(vault, export_records(home))
    if vst == "foreign":
        print("vault 자리(%s)에 pwiki 가 아닌 항목이 %d개 있다. 빈 폴더나 새 하위 폴더를 PWIKI_VAULT 로 준다" % (vault, nv))
        return 1
    if hst == "legacy" and nf:
        print("PWIKI_HOME 자리(%s)는 기존 pwiki 설치다(DB 확인). 곁에 있는 다른 항목 %d개는 그대로 둔다" % (home, nf))
    return 0


def main(argv):
    if len(argv) >= 3 and argv[0] == "check-install":
        return check_install(argv[1], argv[2])
    if len(argv) >= 6 and argv[0] == "purge":
        flags = set(argv[6:])
        return purge(*argv[1:6], apply="--apply" in flags, dry="--dry-run" in flags)
    print(__doc__.split("사용:")[1].rstrip(), file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
