"""
GitHub Issues for Spazcat IPAM: a two-way sync between a repo's issues and a
local copy, plus the API behind the Issues panel.

- Pull: issues and comments changed since the last sync (GitHub's `since`),
  so the panel opens instantly and works while GitHub is unreachable. Issues
  whose comment count doesn't match are re-read in full, which also catches
  deleted comments; "Full resync" rebuilds the local copy from scratch.
- Push: anything created or changed in the app (new issue, comment, edit,
  close/reopen, labels) is written to an outbox, applied to the local copy
  straight away and sent to GitHub at once. If GitHub can't be reached (or
  refuses), it stays in the outbox and is retried on the next sync, so
  nothing typed is lost. Items are sent in order; a failed item holds back
  later changes to the same issue.

Uses the GitHub token from Settings -> Maintenance -> Update source (needs
Issues: Read and write on the repo). The token is only sent to
api.github.com - pagination links are checked before they're followed.
"""

import json
import os
import re
import threading
import time
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime

import requests
from flask import jsonify, request

GITHUB_API = "https://api.github.com"
REPO_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
ALLOWED = os.environ.get("IPAM_ISSUES", "true").lower() not in ("0", "false", "no", "off")
MAX_PAGES = 50                 # 5000 items per sync, per endpoint
REFRESH_PER_SYNC = 25          # issues re-read in full per sync (comment drift)
STATE_REASONS = ("completed", "not_planned", "reopened")

SETTING_DEFAULTS = {
    "issues_enabled": "1",
    "issues_repo": "",          # blank = the update-source repo
    "issues_sync_minutes": "10",
    "issues_signature": "1",    # add "Sent from <app> by <user>" to posts
}

SCHEMA = """
CREATE TABLE IF NOT EXISTS gh_issues (
    repo         TEXT NOT NULL,
    number       INTEGER NOT NULL,          -- negative = created here, not sent yet
    title        TEXT NOT NULL DEFAULT '',
    body         TEXT NOT NULL DEFAULT '',
    body_html    TEXT NOT NULL DEFAULT '',
    state        TEXT NOT NULL DEFAULT 'open',
    state_reason TEXT NOT NULL DEFAULT '',
    labels       TEXT NOT NULL DEFAULT '[]',
    author       TEXT NOT NULL DEFAULT '',
    comments     INTEGER NOT NULL DEFAULT 0,
    created_at   TEXT NOT NULL DEFAULT '',
    updated_at   TEXT NOT NULL DEFAULT '',
    closed_at    TEXT NOT NULL DEFAULT '',
    html_url     TEXT NOT NULL DEFAULT '',
    pending      INTEGER NOT NULL DEFAULT 0, -- local changes waiting in the outbox
    PRIMARY KEY (repo, number)
);
CREATE TABLE IF NOT EXISTS gh_comments (
    repo         TEXT NOT NULL,
    id           INTEGER NOT NULL,          -- negative = written here, not sent yet
    issue_number INTEGER NOT NULL,
    body         TEXT NOT NULL DEFAULT '',
    body_html    TEXT NOT NULL DEFAULT '',
    author       TEXT NOT NULL DEFAULT '',
    created_at   TEXT NOT NULL DEFAULT '',
    updated_at   TEXT NOT NULL DEFAULT '',
    html_url     TEXT NOT NULL DEFAULT '',
    pending      INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (repo, id)
);
CREATE INDEX IF NOT EXISTS gh_comments_issue ON gh_comments (repo, issue_number);
CREATE TABLE IF NOT EXISTS gh_outbox (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    repo         TEXT NOT NULL,
    kind         TEXT NOT NULL,             -- create_issue | update_issue | comment | edit_comment | delete_comment
    issue_number INTEGER,                   -- target issue (negative while it's a local one)
    comment_id   INTEGER,
    payload      TEXT NOT NULL DEFAULT '{}',
    created_at   INTEGER,
    attempts     INTEGER NOT NULL DEFAULT 0,
    last_error   TEXT NOT NULL DEFAULT ''
);
"""

_lock = threading.Lock()


class GHError(Exception):
    def __init__(self, msg, status=None):
        super().__init__(msg)
        self.status = status

    @property
    def offline(self):
        """Not worth trying the rest of the outbox (GitHub unreachable, token
        rejected, rate limited)."""
        return self.status in (None, 401, 429) or (self.status or 0) >= 500


def init_db(db):
    db.executescript(SCHEMA)
    for k, v in SETTING_DEFAULTS.items():
        db.execute("INSERT OR IGNORE INTO settings (key, value) VALUES (?,?)", (k, v))


# ----------------------------------------------------------------------------
# GitHub HTTP
# ----------------------------------------------------------------------------

def _perm_label(header):
    """'issues=write' -> 'Issues: Read and write'."""
    first = re.split(r"[;,]", header or "")[0].strip()
    if "=" not in first:
        return ""
    name, level = first.split("=", 1)
    name = name.replace("_", " ").strip().capitalize()
    return f"{name}: {'Read and write' if level.strip() == 'write' else 'Read-only'}"


def _explain(r):
    try:
        body = r.json()
        msg = body.get("message", "")
        errs = "; ".join(e.get("message") or f"{e.get('field')} {e.get('code')}"
                         for e in body.get("errors") or [] if isinstance(e, dict))
    except ValueError:
        msg, errs = r.text[:200], ""
    s = r.status_code
    if s == 401:
        return "GitHub rejected the token (expired or revoked?)"
    if s in (403, 429) and (r.headers.get("X-RateLimit-Remaining") == "0" or "rate limit" in msg.lower()):
        reset = r.headers.get("X-RateLimit-Reset")
        when = time.strftime("%H:%M", time.localtime(int(reset))) if reset and reset.isdigit() else "later"
        return f"GitHub rate limit reached - try again after {when}"
    if s == 403:
        need = _perm_label(r.headers.get("X-Accepted-GitHub-Permissions"))
        return ("The token isn't allowed to do this" +
                (f" - give it {need} on the repo" if need else f" ({msg})" if msg else ""))
    if s == 404:
        return "Repo or issue not found - check the repo name, and that the token includes this repo"
    if s == 410:
        return "Issues are turned off for this repo (GitHub repo settings -> Features -> Issues)"
    if s == 422:
        return f"GitHub refused it: {errs or msg or 'validation failed'}"
    return f"GitHub error (HTTP {s}){': ' + msg if msg else ''}"


def gh(method, url, token, _hops=0, **kw):
    """One call to the GitHub API. `url` is a path, or a full URL that must be
    on api.github.com (so the token never leaves for another host)."""
    if not url.startswith("http"):
        url = GITHUB_API + url
    if not url.startswith(GITHUB_API + "/"):
        raise GHError(f"refusing to send the token to {url.split('/')[2]}")
    headers = {"Accept": "application/vnd.github.full+json",
               "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "spazcat-ipam"}
    headers.update(kw.pop("headers", {}))
    if token:
        headers["Authorization"] = f"Bearer {token}"
    try:
        r = requests.request(method, url, headers=headers, timeout=15, allow_redirects=False, **kw)
    except requests.RequestException as e:
        raise GHError(f"Can't reach GitHub ({e.__class__.__name__})")
    if r.status_code in (301, 302, 307, 308):   # renamed repo / transferred issue
        loc = r.headers.get("Location", "")
        if loc.startswith(GITHUB_API + "/") and _hops < 3:
            return gh(method, loc, token, _hops=_hops + 1, **kw)
        raise GHError("GitHub redirected somewhere unexpected", r.status_code)
    if r.status_code >= 400:
        raise GHError(_explain(r), r.status_code)
    return r


def _pages(path, token, params):
    """Every item from a paginated list endpoint (bounded)."""
    url, out, first = path, [], None
    for i in range(MAX_PAGES):
        r = gh("GET", url, token, params=params if i == 0 else None)
        if first is None:
            first = r
        out += r.json()
        m = re.search(r'<([^>]+)>;\s*rel="next"', r.headers.get("Link", ""))
        if not m:
            break
        url = m.group(1)
    return out, first


def _server_time(r):
    """GitHub's clock from the Date header (ours may drift), as ISO 8601."""
    try:
        t = parsedate_to_datetime(r.headers["Date"])
    except Exception:
        t = datetime.now(timezone.utc)
    return t.astimezone(timezone.utc)


def _iso(t):
    return t.strftime("%Y-%m-%dT%H:%M:%SZ")


def _now_iso():
    return _iso(datetime.now(timezone.utc))


# GitHub already sanitizes the HTML it renders; this is a second line of defence
# before it's put into the page.
_BAD_BLOCK = re.compile(r"<\s*(script|style|iframe|object|embed|form|textarea|select|button|base|meta|link|svg|math)\b.*?<\s*/\s*\1\s*>", re.I | re.S)
_BAD_TAG = re.compile(r"<\s*/?\s*(script|style|iframe|object|embed|form|textarea|select|button|base|meta|link|svg|math)\b[^>]*>", re.I)
_ON_ATTR = re.compile(r"""\s+(on[a-z]+|style|srcdoc|formaction)\s*=\s*("[^"]*"|'[^']*'|[^\s>]+)""", re.I)
_BAD_URL = re.compile(r"""\b(href|src|action|xlink:href)\s*=\s*(["']?)\s*(javascript|vbscript|data|file):""", re.I)


def clean_html(html):
    html = _BAD_BLOCK.sub("", html or "")
    html = _BAD_TAG.sub("", html)
    html = _ON_ATTR.sub("", html)
    return _BAD_URL.sub(r'\1=\2#blocked:', html)


# ----------------------------------------------------------------------------
# Local copy
# ----------------------------------------------------------------------------

def _labels_json(items):
    return json.dumps([{"name": l.get("name", ""), "color": l.get("color", "")}
                       for l in items or [] if isinstance(l, dict)])


def store_issue(db, repo, it, force=False):
    """Upsert one issue from the API. Rows with unsent local changes are left
    alone unless `force` (the response to our own change)."""
    row = (repo, it["number"], it.get("title") or "", it.get("body") or "",
           clean_html(it.get("body_html") or ""), it.get("state") or "open",
           it.get("state_reason") or "", _labels_json(it.get("labels")),
           (it.get("user") or {}).get("login", ""), int(it.get("comments") or 0),
           it.get("created_at") or "", it.get("updated_at") or "", it.get("closed_at") or "",
           it.get("html_url") or "")
    db.execute(
        "INSERT INTO gh_issues (repo, number, title, body, body_html, state, state_reason, labels,"
        " author, comments, created_at, updated_at, closed_at, html_url, pending)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,0)"
        " ON CONFLICT(repo, number) DO UPDATE SET title=excluded.title, body=excluded.body,"
        " body_html=excluded.body_html, state=excluded.state, state_reason=excluded.state_reason,"
        " labels=excluded.labels, author=excluded.author, comments=excluded.comments,"
        " created_at=excluded.created_at, updated_at=excluded.updated_at,"
        " closed_at=excluded.closed_at, html_url=excluded.html_url, pending=0"
        + ("" if force else " WHERE gh_issues.pending=0"), row)


def store_comment(db, repo, number, c, force=False):
    row = (repo, c["id"], number, c.get("body") or "", clean_html(c.get("body_html") or ""),
           (c.get("user") or {}).get("login", ""), c.get("created_at") or "",
           c.get("updated_at") or "", c.get("html_url") or "")
    db.execute(
        "INSERT INTO gh_comments (repo, id, issue_number, body, body_html, author, created_at,"
        " updated_at, html_url, pending) VALUES (?,?,?,?,?,?,?,?,?,0)"
        " ON CONFLICT(repo, id) DO UPDATE SET body=excluded.body, body_html=excluded.body_html,"
        " author=excluded.author, updated_at=excluded.updated_at, html_url=excluded.html_url,"
        " pending=0" + ("" if force else " WHERE gh_comments.pending=0"), row)


def _issue_number(c):
    m = re.search(r"/issues/(\d+)$", c.get("issue_url") or "")
    return int(m.group(1)) if m else None


def _queued_deletes(db, repo):
    return {r[0] for r in db.execute(
        "SELECT comment_id FROM gh_outbox WHERE repo=? AND kind='delete_comment'", (repo,))}


def refresh_issue(db, repo, token, number):
    """Re-read one issue and all its comments; drops comments deleted on GitHub."""
    try:
        it = gh("GET", f"/repos/{repo}/issues/{number}", token).json()
    except GHError as e:
        if e.status in (404, 410):      # deleted (or transferred away)
            db.execute("DELETE FROM gh_issues WHERE repo=? AND number=? AND pending=0", (repo, number))
            db.execute("DELETE FROM gh_comments WHERE repo=? AND issue_number=? AND pending=0", (repo, number))
            return None
        raise
    if "pull_request" in it or not (it.get("repository_url") or "").lower().endswith("/repos/" + repo.lower()):
        db.execute("DELETE FROM gh_issues WHERE repo=? AND number=? AND pending=0", (repo, number))
        return None
    store_issue(db, repo, it)
    comments, _ = _pages(f"/repos/{repo}/issues/{number}/comments", token, {"per_page": 100})
    skip = _queued_deletes(db, repo)
    keep = set()
    for c in comments:
        keep.add(c["id"])
        if c["id"] not in skip:
            store_comment(db, repo, number, c)
    have = [r[0] for r in db.execute(
        "SELECT id FROM gh_comments WHERE repo=? AND issue_number=? AND id>0 AND pending=0", (repo, number))]
    for cid in have:
        if cid not in keep:
            db.execute("DELETE FROM gh_comments WHERE repo=? AND id=?", (repo, cid))
    return it


# ----------------------------------------------------------------------------
# Outbox
# ----------------------------------------------------------------------------

def enqueue(db, repo, kind, payload, issue_number=None, comment_id=None):
    cur = db.execute(
        "INSERT INTO gh_outbox (repo, kind, issue_number, comment_id, payload, created_at)"
        " VALUES (?,?,?,?,?,?)",
        (repo, kind, issue_number, comment_id, json.dumps(payload), int(time.time())))
    return cur.lastrowid


def _still_pending(db, repo, number):
    return db.execute("SELECT 1 FROM gh_outbox WHERE repo=? AND issue_number=? LIMIT 1",
                      (repo, number)).fetchone() is not None


def _send(db, repo, token, row):
    p = json.loads(row["payload"] or "{}")
    kind, num = row["kind"], row["issue_number"]
    if kind == "create_issue":
        it = gh("POST", f"/repos/{repo}/issues", token, json=p).json()
        local = -row["id"]
        db.execute("DELETE FROM gh_issues WHERE repo=? AND number=?", (repo, local))
        store_issue(db, repo, it, force=True)
        return {"created": it["number"], "local": local}
    if kind == "update_issue":
        it = gh("PATCH", f"/repos/{repo}/issues/{num}", token, json=p).json()
        db.execute("DELETE FROM gh_outbox WHERE id=?", (row["id"],))
        store_issue(db, repo, it, force=not _still_pending(db, repo, num))
        return {}
    if kind == "comment":
        c = gh("POST", f"/repos/{repo}/issues/{num}/comments", token, json=p).json()
        db.execute("DELETE FROM gh_comments WHERE repo=? AND id=?", (repo, -row["id"]))
        store_comment(db, repo, num, c, force=True)
        db.execute("UPDATE gh_issues SET comments=(SELECT COUNT(*) FROM gh_comments WHERE repo=?"
                   " AND issue_number=?), updated_at=? WHERE repo=? AND number=?",
                   (repo, num, c.get("updated_at") or _now_iso(), repo, num))
        return {}
    if kind == "edit_comment":
        c = gh("PATCH", f"/repos/{repo}/issues/comments/{row['comment_id']}", token, json=p).json()
        store_comment(db, repo, num, c, force=True)
        return {}
    if kind == "delete_comment":
        try:
            gh("DELETE", f"/repos/{repo}/issues/comments/{row['comment_id']}", token)
        except GHError as e:
            if e.status != 404:          # already gone is fine
                raise
        db.execute("DELETE FROM gh_comments WHERE repo=? AND id=?", (repo, row["comment_id"]))
        db.execute("UPDATE gh_issues SET comments=MAX(comments-1, 0) WHERE repo=? AND number=?", (repo, num))
        return {}
    raise GHError(f"unknown outbox item {kind}")


def flush(db, repo, token, only=None):
    """Send queued changes in order. Returns (sent, failed, first_error, created_map)."""
    sent = failed = 0
    first_err, created, blocked = None, {}, set()
    if not token:
        return 0, 0, None, {}
    rows = db.execute("SELECT * FROM gh_outbox WHERE repo=? ORDER BY id", (repo,)).fetchall()
    for row in rows:
        if only is not None and row["id"] not in only:
            continue
        if row["issue_number"] in blocked:
            continue
        try:
            out = _send(db, repo, token, row)
            db.execute("DELETE FROM gh_outbox WHERE id=?", (row["id"],))
            if out.get("created"):
                created[out["local"]] = out["created"]
            sent += 1
            db.commit()
        except GHError as e:
            db.rollback()
            db.execute("UPDATE gh_outbox SET attempts=attempts+1, last_error=? WHERE id=?",
                       (str(e), row["id"]))
            db.commit()
            failed += 1
            first_err = first_err or str(e)
            blocked.add(row["issue_number"])
            if e.offline:
                break
    return sent, failed, first_err, created


# ----------------------------------------------------------------------------
# Sync
# ----------------------------------------------------------------------------

class Host:
    """What the issues module needs from app.py (filled in by register())."""
    get_db = get_setting = set_setting = github_config = app_name = None


H = Host()


def config(db):
    """(repo, token, locked fields)."""
    default_repo, token, token_locked = H.github_config(db)
    locked = set()
    if token_locked:
        locked.add("github_token")
    env_repo = os.environ.get("IPAM_ISSUES_REPO", "").strip()
    if env_repo:
        locked.add("issues_repo")
        repo = env_repo
    else:
        repo = (H.get_setting(db, "issues_repo", "") or "").strip() or default_repo
    return repo, token, locked


def _setting_int(db, key, default):
    try:
        return int(float(H.get_setting(db, key, str(default)) or default))
    except (TypeError, ValueError):
        return default


def enabled(db):
    return ALLOWED and (H.get_setting(db, "issues_enabled", "1") or "0") in ("1", "true")


def _since_key(repo):
    return "gh_since:" + repo.lower()


def pull(db, repo, token, full=False):
    """Fetch what changed on GitHub since the last pull."""
    since = None if full else (H.get_setting(db, _since_key(repo), "") or None)
    params = {"state": "all", "sort": "updated", "direction": "asc", "per_page": 100}
    if since:
        params["since"] = since
    items, first = _pages(f"/repos/{repo}/issues", token, params)
    started = _server_time(first) - timedelta(minutes=2)    # overlap; upserts are idempotent
    if full:
        db.execute("DELETE FROM gh_issues WHERE repo=? AND pending=0 AND number>0", (repo,))
        db.execute("DELETE FROM gh_comments WHERE repo=? AND pending=0 AND id>0", (repo,))
    n_issues = 0
    for it in items:
        if "pull_request" in it:
            continue
        store_issue(db, repo, it)
        n_issues += 1
    known = {r[0] for r in db.execute("SELECT number FROM gh_issues WHERE repo=? AND number>0", (repo,))}
    cparams = {"sort": "updated", "direction": "asc", "per_page": 100}
    if since:
        cparams["since"] = since
    comments, _ = _pages(f"/repos/{repo}/issues/comments", token, cparams)
    skip = _queued_deletes(db, repo)
    n_comments = 0
    for c in comments:
        num = _issue_number(c)
        if num in known and c["id"] not in skip:
            store_comment(db, repo, num, c)
            n_comments += 1
    db.commit()
    # comment counts that don't add up mean a comment was deleted on GitHub
    drift = db.execute(
        "SELECT i.number FROM gh_issues i WHERE i.repo=? AND i.number>0 AND i.pending=0 AND"
        " i.comments != (SELECT COUNT(*) FROM gh_comments c WHERE c.repo=i.repo AND"
        " c.issue_number=i.number AND c.id>0) ORDER BY i.updated_at DESC LIMIT ?",
        (repo, REFRESH_PER_SYNC)).fetchall()
    for r in drift:
        refresh_issue(db, repo, token, r[0])
    H.set_setting(db, _since_key(repo), _iso(started))
    db.commit()
    return n_issues, n_comments


def fetch_labels(db, repo, token):
    labels, _ = _pages(f"/repos/{repo}/labels", token, {"per_page": 100})
    H.set_setting(db, "gh_labels", json.dumps({"repo": repo, "labels": json.loads(_labels_json(labels))}))


def fetch_login(db, token):
    try:
        H.set_setting(db, "gh_login", gh("GET", "/user", token).json().get("login", ""))
    except GHError:
        pass    # not every token can read /user; only used for "posting as"


def sync(db, full=False, wait=30):
    """Send the outbox, then pull. Returns a result dict (never raises)."""
    repo, token, _ = config(db)
    if not repo or not REPO_RE.match(repo):
        return {"ok": False, "error": "Set the GitHub repo (owner/name) first"}
    if not _lock.acquire(timeout=wait):
        return {"ok": False, "error": "A sync is already running"}
    try:
        sent, failed, out_err, _ = flush(db, repo, token)
        try:
            if token and not H.get_setting(db, "gh_login", ""):
                fetch_login(db, token)
            n_i, n_c = pull(db, repo, token, full=full)
            fetch_labels(db, repo, token)
            err = None
        except GHError as e:
            db.rollback()
            n_i = n_c = 0
            err = str(e)
        H.set_setting(db, "gh_last_sync", str(int(time.time())))
        H.set_setting(db, "gh_error", err or "")
        if not err:
            H.set_setting(db, "gh_last_ok", str(int(time.time())))
        db.commit()
        if err:
            print(f"[issues] sync failed: {err}", flush=True)
        return {"ok": not err, "error": err, "issues": n_i, "comments": n_c,
                "sent": sent, "failed": failed, "outbox_error": out_err}
    finally:
        _lock.release()


def maybe_sync(db):
    """Background timer hook: sync when the interval has passed."""
    if not enabled(db):
        return
    repo, token, _ = config(db)
    if not token:
        return
    every = _setting_int(db, "issues_sync_minutes", 10)
    if every <= 0:
        return
    last = _setting_int(db, "gh_last_sync", 0)
    if time.time() - last >= every * 60:
        res = sync(db, wait=0)
        if res.get("ok") and (res["issues"] or res["comments"] or res["sent"]):
            print(f"[issues] {res}", flush=True)


# ----------------------------------------------------------------------------
# Serialization
# ----------------------------------------------------------------------------

def _issue_dict(r, full=False):
    d = {k: r[k] for k in ("number", "title", "state", "state_reason", "author", "comments",
                           "created_at", "updated_at", "closed_at", "html_url")}
    d["labels"] = json.loads(r["labels"] or "[]")
    d["pending"] = bool(r["pending"])
    if full:
        d["body"], d["body_html"] = r["body"], r["body_html"]
    return d


def _comment_dict(r):
    return {k: r[k] for k in ("id", "issue_number", "body", "body_html", "author",
                              "created_at", "updated_at", "html_url")} | {"pending": bool(r["pending"])}


def _outbox(db, repo):
    out = []
    for r in db.execute("SELECT * FROM gh_outbox WHERE repo=? ORDER BY id", (repo,)):
        p = json.loads(r["payload"] or "{}")
        what = {"create_issue": f"New issue \"{p.get('title', '')}\"",
                "update_issue": f"Change to #{r['issue_number']}",
                "comment": f"Comment on #{r['issue_number']}",
                "edit_comment": f"Comment edit on #{r['issue_number']}",
                "delete_comment": f"Comment delete on #{r['issue_number']}"}.get(r["kind"], r["kind"])
        out.append({"id": r["id"], "kind": r["kind"], "what": what, "issue_number": r["issue_number"],
                    "created_at": r["created_at"], "attempts": r["attempts"], "last_error": r["last_error"]})
    return out


def labels(db, repo):
    try:
        v = json.loads(H.get_setting(db, "gh_labels", "") or "{}")
    except ValueError:
        return []
    return v.get("labels", []) if v.get("repo") == repo else []


def status(db):
    repo, token, locked = config(db)
    return {
        "allowed": ALLOWED,
        "enabled": enabled(db),
        "repo": repo,
        "repo_setting": H.get_setting(db, "issues_repo", "") or "",
        "default_repo": H.github_config(db)[0],
        "locked": sorted(locked),
        "token_set": bool(token),
        "token_kind": ("fine-grained" if token.startswith("github_pat_") else "classic") if token else None,
        "login": H.get_setting(db, "gh_login", "") or "",
        "sync_minutes": _setting_int(db, "issues_sync_minutes", 10),
        "signature": (H.get_setting(db, "issues_signature", "1") or "0") == "1",
        "last_sync": _setting_int(db, "gh_last_sync", 0),
        "last_ok": _setting_int(db, "gh_last_ok", 0),
        "error": H.get_setting(db, "gh_error", "") or None,
        "outbox": _outbox(db, repo),
    }


def summary(db):
    """Tiny block for /api/state: toolbar badge + 'something changed' dot."""
    if not enabled(db):
        return {"enabled": False}
    repo, token, _ = config(db)
    r = db.execute("SELECT SUM(state='open'), MAX(updated_at) FROM gh_issues WHERE repo=?", (repo,)).fetchone()
    c = db.execute("SELECT MAX(updated_at) FROM gh_comments WHERE repo=?", (repo,)).fetchone()
    pend = db.execute("SELECT COUNT(*) FROM gh_outbox WHERE repo=?", (repo,)).fetchone()[0]
    return {"enabled": True, "configured": bool(token), "open": r[0] or 0,
            "latest": max(r[1] or "", c[0] or ""), "pending": pend}


# ----------------------------------------------------------------------------
# Routes
# ----------------------------------------------------------------------------

def _signature(db, text):
    if (H.get_setting(db, "issues_signature", "1") or "0") != "1":
        return text
    try:
        import auth
        authed, _, user = auth.current()
    except Exception:
        authed, user = False, None
    who = f" by {user}" if authed and user else ""
    sig = f"<sub>Sent from {H.app_name(db)}{who}</sub>"
    return f"{text.rstrip()}\n\n{sig}" if text.strip() else sig


def _clean_labels(v):
    if not isinstance(v, list):
        return None
    return [str(x).strip()[:50] for x in v if str(x).strip()][:20]


def register(app, get_db, get_setting, set_setting, github_config, app_name):
    H.get_db, H.get_setting, H.set_setting = get_db, get_setting, set_setting
    H.github_config, H.app_name = github_config, app_name

    def guard(db):
        if not enabled(db):
            return jsonify({"error": "GitHub issues are turned off (Settings -> Maintenance)"}), 400
        repo, _, _ = config(db)
        if not repo or not REPO_RE.match(repo):
            return jsonify({"error": "Set the GitHub repo first (Settings -> Maintenance)"}), 400
        return None

    def push_now(db, repo, token, outbox_id):
        """Try to send what was just queued. Returns (sent?, error, created)."""
        if not token:
            return False, "No GitHub token - saved here, will send once a token is set", {}
        if not _lock.acquire(timeout=20):
            return False, "A sync is running - saved here, will send shortly", {}
        try:
            # send everything queued up to and including this item, in order
            ids = [r[0] for r in db.execute("SELECT id FROM gh_outbox WHERE repo=? AND id<=?",
                                            (repo, outbox_id))]
            _, _, first_err, created = flush(db, repo, token, only=set(ids))
        finally:
            _lock.release()
        row = db.execute("SELECT last_error FROM gh_outbox WHERE id=?", (outbox_id,)).fetchone()
        if row:
            return False, row[0] or first_err or "Waiting for an earlier change to send", created
        return True, None, created

    def result(db, repo, number, sent, err, code=200):
        r = db.execute("SELECT * FROM gh_issues WHERE repo=? AND number=?", (repo, number)).fetchone()
        return jsonify({"ok": True, "sent": sent, "queued": not sent, "error": err,
                        "issue": _issue_dict(r, full=True) if r else None}), code

    @app.route("/api/issues")
    def api_issues():
        db = get_db()
        st = status(db)
        rows = db.execute("SELECT * FROM gh_issues WHERE repo=? ORDER BY (number<0) DESC, updated_at DESC",
                          (st["repo"],)).fetchall() if st["enabled"] else []
        return jsonify({"status": st, "labels": labels(db, st["repo"]),
                        "issues": [_issue_dict(r) for r in rows]})

    @app.route("/api/issues/<int(signed=True):number>")
    def api_issue(number):
        db = get_db()
        bad = guard(db)
        if bad:
            return bad
        repo, token, _ = config(db)
        err = None
        if request.args.get("refresh") and number > 0:
            try:
                refresh_issue(db, repo, token, number)
                db.commit()
            except GHError as e:
                db.rollback()
                err = str(e)
        r = db.execute("SELECT * FROM gh_issues WHERE repo=? AND number=?", (repo, number)).fetchone()
        if not r:
            return jsonify({"error": "Issue not found (deleted or moved on GitHub?)"}), 404
        comments = db.execute("SELECT * FROM gh_comments WHERE repo=? AND issue_number=?"
                              " ORDER BY (id<0), created_at, id", (repo, number)).fetchall()
        return jsonify({"issue": _issue_dict(r, full=True), "comments": [_comment_dict(c) for c in comments],
                        "login": get_setting(db, "gh_login", "") or "", "error": err})

    @app.route("/api/issues", methods=["POST"])
    def api_issue_create():
        db = get_db()
        bad = guard(db)
        if bad:
            return bad
        repo, token, _ = config(db)
        data = request.get_json(force=True, silent=True) or {}
        title = str(data.get("title") or "").strip()[:256]
        if not title:
            return jsonify({"error": "Give the issue a title"}), 400
        body = _signature(db, str(data.get("body") or "")[:60000])
        payload = {"title": title, "body": body}
        lbl = _clean_labels(data.get("labels"))
        if lbl:
            payload["labels"] = lbl
        oid = enqueue(db, repo, "create_issue", payload)
        now = _now_iso()
        db.execute("INSERT INTO gh_issues (repo, number, title, body, state, labels, author,"
                   " created_at, updated_at, pending) VALUES (?,?,?,?,'open',?,?,?,?,1)",
                   (repo, -oid, title, body, json.dumps([{"name": n, "color": ""} for n in lbl or []]),
                    get_setting(db, "gh_login", "") or "you", now, now))
        db.commit()
        sent, err, created = push_now(db, repo, token, oid)
        return result(db, repo, created.get(-oid, -oid), sent, err, 201)

    @app.route("/api/issues/<int:number>", methods=["PATCH"])
    def api_issue_update(number):
        db = get_db()
        bad = guard(db)
        if bad:
            return bad
        repo, token, _ = config(db)
        data = request.get_json(force=True, silent=True) or {}
        cur = db.execute("SELECT * FROM gh_issues WHERE repo=? AND number=?", (repo, number)).fetchone()
        if not cur:
            return jsonify({"error": "Issue not found"}), 404
        payload, local = {}, {}
        if "title" in data:
            t = str(data["title"] or "").strip()[:256]
            if not t:
                return jsonify({"error": "The title can't be empty"}), 400
            payload["title"] = local["title"] = t
        if "body" in data:
            payload["body"] = local["body"] = str(data["body"] or "")[:60000]
            local["body_html"] = ""
        if "state" in data:
            if data["state"] not in ("open", "closed"):
                return jsonify({"error": "state must be open or closed"}), 400
            payload["state"] = local["state"] = data["state"]
            reason = data.get("state_reason") or ("completed" if data["state"] == "closed" else "reopened")
            if reason not in STATE_REASONS:
                return jsonify({"error": "state_reason must be completed, not_planned or reopened"}), 400
            payload["state_reason"] = local["state_reason"] = reason
            local["closed_at"] = _now_iso() if data["state"] == "closed" else ""
        if "labels" in data:
            lbl = _clean_labels(data["labels"])
            if lbl is None:
                return jsonify({"error": "labels must be a list"}), 400
            payload["labels"] = lbl
            known = {l["name"]: l["color"] for l in labels(db, repo)}
            local["labels"] = json.dumps([{"name": n, "color": known.get(n, "")} for n in lbl])
        if not payload:
            return jsonify({"error": "nothing to change"}), 400
        oid = enqueue(db, repo, "update_issue", payload, issue_number=number)
        local["updated_at"] = _now_iso()
        sets = ", ".join(f"{k}=?" for k in local)
        db.execute(f"UPDATE gh_issues SET {sets}, pending=1 WHERE repo=? AND number=?",
                   (*local.values(), repo, number))
        db.commit()
        sent, err, _ = push_now(db, repo, token, oid)
        return result(db, repo, number, sent, err)

    @app.route("/api/issues/<int:number>/comments", methods=["POST"])
    def api_issue_comment(number):
        db = get_db()
        bad = guard(db)
        if bad:
            return bad
        repo, token, _ = config(db)
        body = str((request.get_json(force=True, silent=True) or {}).get("body") or "").strip()
        if not body:
            return jsonify({"error": "Write something first"}), 400
        if not db.execute("SELECT 1 FROM gh_issues WHERE repo=? AND number=?", (repo, number)).fetchone():
            return jsonify({"error": "Issue not found"}), 404
        body = _signature(db, body[:60000])
        oid = enqueue(db, repo, "comment", {"body": body}, issue_number=number)
        now = _now_iso()
        db.execute("INSERT INTO gh_comments (repo, id, issue_number, body, author, created_at,"
                   " updated_at, pending) VALUES (?,?,?,?,?,?,?,1)",
                   (repo, -oid, number, body, get_setting(db, "gh_login", "") or "you", now, now))
        db.execute("UPDATE gh_issues SET updated_at=? WHERE repo=? AND number=?", (now, repo, number))
        db.commit()
        sent, err, _ = push_now(db, repo, token, oid)
        return result(db, repo, number, sent, err, 201)

    @app.route("/api/issues/comments/<int:cid>", methods=["PATCH", "DELETE"])
    def api_comment_change(cid):
        db = get_db()
        bad = guard(db)
        if bad:
            return bad
        repo, token, _ = config(db)
        c = db.execute("SELECT * FROM gh_comments WHERE repo=? AND id=?", (repo, cid)).fetchone()
        if not c:
            return jsonify({"error": "Comment not found"}), 404
        num = c["issue_number"]
        if request.method == "DELETE":
            oid = enqueue(db, repo, "delete_comment", {}, issue_number=num, comment_id=cid)
            db.execute("DELETE FROM gh_comments WHERE repo=? AND id=?", (repo, cid))
        else:
            body = str((request.get_json(force=True, silent=True) or {}).get("body") or "").strip()
            if not body:
                return jsonify({"error": "A comment can't be empty - delete it instead"}), 400
            oid = enqueue(db, repo, "edit_comment", {"body": body[:60000]}, issue_number=num, comment_id=cid)
            db.execute("UPDATE gh_comments SET body=?, body_html='', pending=1, updated_at=?"
                       " WHERE repo=? AND id=?", (body[:60000], _now_iso(), repo, cid))
        db.commit()
        sent, err, _ = push_now(db, repo, token, oid)
        return result(db, repo, num, sent, err)

    @app.route("/api/issues/sync", methods=["POST"])
    def api_issues_sync():
        db = get_db()
        bad = guard(db)
        if bad:
            return bad
        full = bool((request.get_json(force=True, silent=True) or {}).get("full"))
        res = sync(db, full=full)
        return jsonify(res | {"status": status(db)})

    @app.route("/api/issues/outbox/<int:oid>/retry", methods=["POST"])
    def api_outbox_retry(oid):
        db = get_db()
        repo, token, _ = config(db)
        sent, err, _ = push_now(db, repo, token, oid)
        return jsonify({"ok": True, "sent": sent, "error": err, "status": status(db)})

    @app.route("/api/issues/outbox/<int:oid>", methods=["DELETE"])
    def api_outbox_discard(oid):
        """Drop an unsent change and undo it locally (re-read from GitHub)."""
        db = get_db()
        repo, token, _ = config(db)
        row = db.execute("SELECT * FROM gh_outbox WHERE repo=? AND id=?", (repo, oid)).fetchone()
        if not row:
            return jsonify({"error": "Already sent or discarded"}), 404
        db.execute("DELETE FROM gh_outbox WHERE id=?", (oid,))
        if row["kind"] == "create_issue":
            db.execute("DELETE FROM gh_issues WHERE repo=? AND number=?", (repo, -oid))
        elif row["kind"] == "comment":
            db.execute("DELETE FROM gh_comments WHERE repo=? AND id=?", (repo, -oid))
        num = row["issue_number"]
        if num and num > 0 and not _still_pending(db, repo, num):
            db.execute("UPDATE gh_issues SET pending=0 WHERE repo=? AND number=?", (repo, num))
            db.execute("UPDATE gh_comments SET pending=0 WHERE repo=? AND issue_number=? AND id>0", (repo, num))
            if token:
                try:
                    refresh_issue(db, repo, token, num)
                except GHError:
                    pass
        db.commit()
        return jsonify({"ok": True, "status": status(db)})

    @app.route("/api/issues/preview", methods=["POST"])
    def api_issues_preview():
        db = get_db()
        repo, token, _ = config(db)
        text = str((request.get_json(force=True, silent=True) or {}).get("text") or "")[:60000]
        if not text.strip():
            return jsonify({"html": ""})
        try:
            r = gh("POST", "/markdown", token, json={"text": text, "mode": "gfm", "context": repo},
                   headers={"Accept": "application/vnd.github+json"})
        except GHError as e:
            return jsonify({"error": str(e)}), 502
        return jsonify({"html": clean_html(r.text)})

    @app.route("/api/issues/settings", methods=["GET", "PUT"])
    def api_issues_settings():
        db = get_db()
        if request.method == "PUT":
            data = request.get_json(force=True, silent=True) or {}
            _, _, locked = config(db)
            if "issues_enabled" in data:
                set_setting(db, "issues_enabled", "1" if data["issues_enabled"] else "0")
            if "issues_signature" in data:
                set_setting(db, "issues_signature", "1" if data["issues_signature"] else "0")
            if "issues_sync_minutes" in data:
                try:
                    set_setting(db, "issues_sync_minutes", max(0, min(1440, int(float(data["issues_sync_minutes"] or 0)))))
                except (TypeError, ValueError):
                    return jsonify({"error": "Sync interval must be a number of minutes"}), 400
            if "issues_repo" in data and "issues_repo" not in locked:
                repo = re.sub(r"^(https?://)?github\.com/", "", str(data["issues_repo"] or "").strip()).strip("/")
                repo = repo[:-4] if repo.endswith(".git") else repo
                if repo and not REPO_RE.match(repo):
                    return jsonify({"error": "Repo must look like owner/name"}), 400
                set_setting(db, "issues_repo", repo)
            set_setting(db, "gh_login", "")      # re-read who the token belongs to
            db.commit()
            res = None
            if enabled(db) and data.get("sync"):
                res = sync(db)
            return jsonify({"ok": True, "sync": res, "status": status(db)})
        return jsonify({"status": status(db)})
