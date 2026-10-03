"""SQLite 스키마. 시스템 SQLite 3.51 에 있는 기능만 쓴다(메뉴바 앱과 같은 판).

키 규칙
- 원문 줄: <sessionId>:<uuid>. uuid 없는 줄은 <sessionId>:o:<fid>:<바이트오프셋>:<줄 sha1 앞 8자>.
- 파일 id(fid): ~/.claude 기준 상대 경로의 sha1 앞 12자(결정론, 자동증가 아님).
- 작업 카드: c:<sessionId>:<첫 사람 줄 uuid>(uuid 없으면 그 줄의 결정론 키 뒷부분).
"""
import os
import sqlite3

from . import paths

SCHEMA_VERSION = 1

SCHEMA = r"""
CREATE TABLE IF NOT EXISTS files (
  path TEXT PRIMARY KEY,
  fid TEXT UNIQUE NOT NULL,
  kind TEXT NOT NULL,
  project TEXT,
  sid TEXT,
  inode INTEGER, dev INTEGER, size INTEGER, mtime REAL,
  off INTEGER NOT NULL DEFAULT 0,
  head_len INTEGER NOT NULL DEFAULT 0,
  head_sha1 TEXT,
  content_sha1 TEXT,
  n_lines INTEGER NOT NULL DEFAULT 0,
  n_uuid INTEGER NOT NULL DEFAULT 0,
  n_bad INTEGER NOT NULL DEFAULT 0,
  resets INTEGER NOT NULL DEFAULT 0,
  missing INTEGER NOT NULL DEFAULT 0,
  first_seen TEXT, last_ingest TEXT
);

CREATE TABLE IF NOT EXISTS excl (
  fid TEXT NOT NULL, reason TEXT NOT NULL, has_uuid INTEGER NOT NULL,
  n INTEGER NOT NULL DEFAULT 0, bytes INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (fid, reason, has_uuid)
);

CREATE TABLE IF NOT EXISTS strips (
  fid TEXT NOT NULL, reason TEXT NOT NULL,
  n INTEGER NOT NULL DEFAULT 0, bytes INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (fid, reason)
);

CREATE TABLE IF NOT EXISTS events (
  key TEXT PRIMARY KEY,
  sid TEXT, uuid TEXT, parent TEXT,
  fid TEXT NOT NULL, off INTEGER NOT NULL,
  type TEXT, sub TEXT, kind TEXT,
  ts TEXT, side INTEGER, agent TEXT,
  project TEXT, cwd TEXT, ver TEXT, pid TEXT, entry TEXT,
  msg_id TEXT, model TEXT,
  u_in INTEGER, u_out INTEGER, u_cc INTEGER, u_cr INTEGER,
  text TEXT,
  raw TEXT,
  lsha TEXT
);
CREATE INDEX IF NOT EXISTS ev_fid_off ON events(fid, off);
CREATE INDEX IF NOT EXISTS ev_sid ON events(sid);
CREATE INDEX IF NOT EXISTS ev_proj_ts ON events(project, ts);
CREATE INDEX IF NOT EXISTS ev_msg ON events(msg_id);

CREATE TABLE IF NOT EXISTS tool_uses (
  tuid TEXT PRIMARY KEY,
  ekey TEXT NOT NULL, sid TEXT, fid TEXT, off INTEGER, idx INTEGER,
  name TEXT, fpath TEXT, cmd TEXT, inp TEXT, ts TEXT
);
CREATE INDEX IF NOT EXISTS tu_fid_off ON tool_uses(fid, off);

CREATE TABLE IF NOT EXISTS tool_results (
  tuid TEXT PRIMARY KEY,
  ekey TEXT NOT NULL, sid TEXT, fid TEXT, off INTEGER,
  is_err INTEGER, err_sig TEXT, err_line TEXT,
  denial TEXT, intr INTEGER, link TEXT, ts TEXT
);
CREATE INDEX IF NOT EXISTS tr_fid_off ON tool_results(fid, off);

CREATE TABLE IF NOT EXISTS compact_sections (
  ekey TEXT NOT NULL, n INTEGER NOT NULL, title TEXT, body TEXT, fmt TEXT,
  PRIMARY KEY (ekey, n)
);

CREATE TABLE IF NOT EXISTS docs (
  path TEXT PRIMARY KEY,
  kind TEXT, project TEXT, sid TEXT,
  sha1 TEXT, size INTEGER, mtime REAL,
  text TEXT,
  meta TEXT,
  missing INTEGER NOT NULL DEFAULT 0,
  first_seen TEXT, updated TEXT
);

CREATE TABLE IF NOT EXISTS cards (
  key TEXT PRIMARY KEY,
  sid TEXT NOT NULL, project TEXT, cwd TEXT, first_uuid TEXT, first_ekey TEXT,
  human_kind TEXT, delivered INTEGER,
  start_ts TEXT, end_ts TEXT, dur_s REAL, turn_ms INTEGER,
  human TEXT, last_asst TEXT,
  n_asst INTEGER, n_tools INTEGER, tools TEXT,
  files TEXT, n_files INTEGER, cmds TEXT,
  n_err INTEGER, err_sigs TEXT, n_err_repeat INTEGER,
  n_denied INTEGER, n_intr INTEGER,
  tok_in INTEGER, tok_out INTEGER, tok_cc INTEGER, tok_cr INTEGER,
  n_sub INTEGER, n_wf INTEGER, sub_tok_out INTEGER, sub_tok_all INTEGER,
  features TEXT, model TEXT, todos TEXT,
  entry TEXT, ver TEXT, seq INTEGER, n_events INTEGER,
  built TEXT
);
CREATE INDEX IF NOT EXISTS cards_proj_start ON cards(project, start_ts);
CREATE INDEX IF NOT EXISTS cards_start ON cards(start_ts);
CREATE INDEX IF NOT EXISTS cards_sid ON cards(sid);

CREATE TABLE IF NOT EXISTS sessions (
  sid TEXT PRIMARY KEY, project TEXT, cwd TEXT, title TEXT,
  first_ts TEXT, last_ts TEXT, n_cards INTEGER, entry TEXT, built TEXT
);

CREATE TABLE IF NOT EXISTS exports (
  path TEXT PRIMARY KEY, sha1 TEXT NOT NULL, bytes INTEGER, at TEXT
);

CREATE TABLE IF NOT EXISTS runs (
  at TEXT PRIMARY KEY, cmd TEXT, summary TEXT
);

CREATE TABLE IF NOT EXISTS state (
  k TEXT PRIMARY KEY, v TEXT, at TEXT
);

CREATE TABLE IF NOT EXISTS fts_map (
  key TEXT PRIMARY KEY, rid INTEGER NOT NULL
);

CREATE VIRTUAL TABLE IF NOT EXISTS fts USING fts5(
  body, key UNINDEXED, kind UNINDEXED, project UNINDEXED, ts UNINDEXED, sid UNINDEXED,
  tokenize='trigram'
);
"""


def _ro_pragmas(con):
    con.execute("PRAGMA foreign_keys=OFF")
    con.execute("PRAGMA temp_store=MEMORY")
    con.execute("PRAGMA query_only=ON")
    return con


def connect(path=None, readonly=False):
    """readonly=True 면 URI mode=ro 로 연다(쓰기 연결이 아니므로 닫을 때 WAL 체크포인트도 돌지 않는다) + query_only.
    WAL DB 는 -wal 파일이 없으면 mode=ro 로 열 수 없다. -wal 이 없으면 체크포인트할 것도 없으므로 그때(또는 mode=ro 가
    실패하면) 옛 방식(쓰기 가능 연결 + query_only)으로 연다. DB 파일이 없으면 만들지 않도록 mode=ro 그대로 둔다."""
    path = path or paths.db_path()
    if readonly:
        import urllib.parse
        ap = os.path.abspath(path)
        if os.path.exists(ap + "-wal") or not os.path.isfile(ap):
            con = sqlite3.connect("file:%s?mode=ro" % urllib.parse.quote(ap), uri=True, timeout=30, isolation_level=None)
            try:
                con.execute("SELECT count(*) FROM sqlite_master").fetchone()
                return _ro_pragmas(con)
            except sqlite3.OperationalError:
                con.close()
                if not os.path.isfile(ap):
                    raise
        return _ro_pragmas(sqlite3.connect(ap, timeout=30, isolation_level=None))
    con = sqlite3.connect(path, timeout=30, isolation_level=None)
    con.execute("PRAGMA secure_delete=ON")
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA synchronous=NORMAL")
    con.execute("PRAGMA foreign_keys=OFF")
    con.execute("PRAGMA temp_store=MEMORY")
    if readonly:
        con.execute("PRAGMA query_only=ON")
    return con


def init(con):
    con.executescript(SCHEMA)
    # FTS5 는 지운 행의 trigram 게시 목록을 병합 전까지 fts_data 에 남긴다. secure-delete 를 켜면 지울 때 바로 없앤다
    # (SQLite 3.44+, 시스템 3.51). PRAGMA secure_delete 는 b-tree 만 덮으므로 따로 켠다.
    r = con.execute("SELECT v FROM fts_config WHERE k='secure-delete'").fetchone()
    if not r or str(r[0]) != "1":
        con.execute("INSERT INTO fts(fts, rank) VALUES('secure-delete', 1)")
    v = con.execute("PRAGMA user_version").fetchone()[0]
    if v < SCHEMA_VERSION:
        con.execute("PRAGMA user_version=%d" % SCHEMA_VERSION)
    return con


def connect_ro(path=None):
    """읽기 전용 연결(URI mode=ro). 미리보기·검사용. 쓰기·PRAGMA 변경을 하지 않는다."""
    import urllib.parse
    path = path or paths.db_path()
    return sqlite3.connect("file:%s?mode=ro" % urllib.parse.quote(path), uri=True, timeout=30, isolation_level=None)


def get_state(con, k):
    try:
        r = con.execute("SELECT v FROM state WHERE k=?", (k,)).fetchone()
    except sqlite3.OperationalError:
        return None  # state 표가 없는 옛 DB(읽기 전용으로 열었을 때)
    return r[0] if r else None


def set_state(con, k, v):
    con.execute("INSERT OR REPLACE INTO state(k, v, at) VALUES (?,?,?)", (k, v, paths.now_utc_iso()))


def _restrict(path):
    """DB·WAL·SHM 을 600 으로. umask 를 정하지 않은 호출(도구 스크립트·시험)이 만들어도 넓게 열리지 않게 한다."""
    import os
    for p in (path, path + "-wal", path + "-shm"):
        try:
            if os.path.exists(p) and os.stat(p).st_mode & 0o077:
                os.chmod(p, 0o600)
        except OSError:
            pass


def open_db(readonly=False):
    paths.ensure_home()
    con = connect(readonly=readonly)
    if not readonly:
        init(con)
        _restrict(paths.db_path())
    return con
