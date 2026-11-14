"""Per-SHA memo for gate checks whose answer cannot change for a fixed commit.

Each hr-gate run re-read CI and re-walked history from scratch, so a gate took 20 s or
more per PR and every run under the serial lock got slower. Some answers are final for a
commit SHA and can be kept:

  linux CI SUCCESS on head H       final (a failure is NOT kept: a rerun can turn it green;
                                   a run in progress is not final either)
  stack_plain_sha(H, HR heads)     pure: same head and same Human Review heads, same answer

memo() keeps a value only when keep(value) says it is final. The store is hr-gate's own
JSON cache (cache_get/cache_set), under keys that start with "sha:".
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
