"""Per-SHA memo for gate checks whose answer cannot change for a fixed commit.

Each hr-gate run re-read CI and re-walked history from scratch, so a gate took 20 s or
more per PR and every run under the serial lock got slower. Some answers are final for a
commit SHA and can be kept:

  linux CI SUCCESS on head H       final (a failure is NOT kept: a rerun can turn it green;
                                   a run in progress is not final either)
  stack_plain_sha(H, HR heads)     pure: same head and same Human Review heads, same answer

memo() keeps a value only when keep(value) says it is final. The store is hr-gate's own
JSON cache (cache_get/cache_set), under keys that start with "sha:".

tree_memo() also keeps a final answer under the commit's TREE. A new head with the same
tree as a tested commit (an amended message, a re-push, a -vN tip, a main merge that
changed nothing) reuses that answer instead of waiting for a new CI run. A CI pass on a
PR head was never tied to one base either, so this is no weaker than the per-SHA key.
"""
import hashlib

TTL = 7 * 24 * 3600  # final answers; the TTL only bounds the cache file


def key(kind, head, *deps):
    """'sha:<kind>:<head>' plus a short hash of the sorted dependency SHAs, if any."""
    k = f"sha:{kind}:{head}"
    if deps:
        k += ':' + hashlib.sha1(','.join(sorted(str(d) for d in deps)).encode()).hexdigest()[:12]
    return k


MISS = object()


def memo(get, put, k, fn, keep=lambda v: True):
    """get(k) -> cached value or None; else fn(), stored with put(k, v) when keep(v)."""
    hit = get(k)
    if hit is not None:
        return hit['v']
    v = fn()
    if keep(v):
        put(k, {'v': v})
    return v


def ci_final(result):
    """linux_ci returns (ok, why). Only a pass is final."""
    return bool(result and result[0])


def tree_memo(get, put, kind, head, tree, fn, keep=lambda v: True, note=None):
    """memo() by head SHA, then by tree SHA. note(v, other_head) -> v marks a tree hit."""
    hk = key(kind, head)
    hit = get(hk)
    if hit is not None:
        return hit['v']
    tk = key(kind + '-tree', tree) if tree else None
    if tk:
        hit = get(tk)
        if hit is not None:
            v = hit['v']
            return note(v, hit.get('head', '')) if note else v
    v = fn()
    if keep(v):
        put(hk, {'v': v})
        if tk:
            put(tk, {'v': v, 'head': head})
    return v


def ci_tree_note(result, other):
    """linux_ci's (ok, why) marked as reused from another commit with the same tree."""
    ok, why = result
    return ok, f"{why} (same tree as tested commit {other[:8]})"
