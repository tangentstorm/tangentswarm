"""Read `uses:` dependency annotations on prompt guides (port of scialect src/rule-deps.mts).

A guide can list the other rules/ files it uses in a frontmatter block:

    ---
    uses: [commit-guide.md, status-guide.md]
    ---

or the YAML block form (`uses:` followed by `- name` lines).
"""
import re

_FM = re.compile(r'^---\n([\s\S]*?)\n---\n?')
_INLINE = re.compile(r'^uses:\s*\[([^\]]*)\]', re.M)
_BLOCK = re.compile(r'^uses:\s*\n((?:\s*-\s*.+\n?)+)', re.M)


def _unquote(s):
    return re.sub(r"^['\"]|['\"]$", '', s.strip())


def split_frontmatter(text):
    m = _FM.match(text)
    if not m:
        return None, text
    return m.group(1), text[m.end():]


def parse_uses(fm):
    if not fm:
        return []
    m = _INLINE.search(fm)
    if m:
        return [x for x in (_unquote(s) for s in m.group(1).split(',')) if x]
    m = _BLOCK.search(fm)
    if m:
        return [x for x in (_unquote(re.sub(r'^\s*-\s*', '', l)) for l in m.group(1).split('\n')) if x]
    return []


def uses_of(text):
    return parse_uses(split_frontmatter(text)[0])


def resolve_dependencies(start, get_content):
    """Return the transitive closure of `uses:`, not including start. The order is
    breadth-first and alphabetical within a level. Cycles and missing guides are allowed."""
    seen = {start}
    result = []
    frontier = [start]
    while frontier:
        nxt = []
        for name in frontier:
            content = get_content(name)
            if content is None:
                continue
            for dep in sorted(uses_of(content)):
                if dep in seen:
                    continue
                seen.add(dep)
                result.append(dep)
                nxt.append(dep)
        frontier = nxt
    return result
