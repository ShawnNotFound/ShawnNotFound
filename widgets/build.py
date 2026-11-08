"""Builds the profile README widgets and refreshes the open-source table in README.md.

Runs in the GitHub Action every day, or locally with `python widgets/build.py`.
The activity card counts private contributions when GitHub can see them: either the
"Private contributions" setting is on for the profile, or ACTIVITY_TOKEN is a token of
the profile owner. Without a token (local runs) it reads the public calendar page.
"""
import base64
import math
import datetime as dt
import html
import io
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from fontTools import subset
from fontTools.ttLib import TTFont

ROOT = Path(__file__).resolve().parent.parent
WIDGETS = ROOT / "widgets"
ASSETS = ROOT / "assets"
CFG = json.loads((WIDGETS / "config.json").read_text(encoding="utf-8"))
LOGIN = CFG["login"]
TOKEN = os.environ.get("GITHUB_TOKEN", "")

THEMES = {
    "light": dict(bg="#f6f4ef", card="#fdfcf9", ink="#131316", ink2="#3b3b44", muted="#6c6c78", line="rgba(19,19,22,0.10)", line2="rgba(19,19,22,0.18)",
                  accent="#5846f5", soft="rgba(88,70,245,0.10)", a="#0fa3a0", b="#5846f5", c="#f0592f", empty="rgba(19,19,22,0.07)", green="#17b26a"),
    "dark": dict(bg="#09090b", card="#111114", ink="#ededf0", ink2="#c3c3cc", muted="#8b8b97", line="rgba(255,255,255,0.09)", line2="rgba(255,255,255,0.16)",
                 accent="#9a8cff", soft="rgba(154,140,255,0.14)", a="#2dd4bf", b="#8b7cff", c="#ff7a59", empty="rgba(255,255,255,0.07)", green="#34d399"),
}
EASE = "cubic-bezier(0.23, 1, 0.32, 1)"


# ───────────── Fonts: measured for layout, subset to the characters each image uses ─────────────
class Face:
    def __init__(self, file, family, weight, style="normal", fallback="sans-serif"):
        self.path = WIDGETS / "fonts" / file
        self.family, self.weight, self.style, self.fallback = family, weight, style, fallback
        font = TTFont(self.path)
        self.upm = font["head"].unitsPerEm
        self.cmap = font.getBestCmap()
        self.adv = {g: m[0] for g, m in font["hmtx"].metrics.items()}

    def width(self, s, size, ls=0.0):
        units = sum(self.adv.get(self.cmap.get(ord(ch), ".notdef"), self.upm // 2) for ch in s)
        return units * size / self.upm + ls * len(s)


FACES = {
    "serif": Face("InstrumentSerif-400.woff", "PS", 400, fallback="Georgia, serif"),
    "serif-i": Face("InstrumentSerif-400i.woff", "PS", 400, "italic", "Georgia, serif"),
    "sans": Face("Inter-400.woff", "PI", 400, fallback="'Segoe UI', Helvetica, Arial, sans-serif"),
    "sans-m": Face("Inter-500.woff", "PI", 500, fallback="'Segoe UI', Helvetica, Arial, sans-serif"),
    "sans-sb": Face("Inter-600.woff", "PI", 600, fallback="'Segoe UI', Helvetica, Arial, sans-serif"),
    "mono": Face("JetBrainsMono-400.woff", "PM", 400, fallback="Consolas, Menlo, monospace"),
    "mono-m": Face("JetBrainsMono-500.woff", "PM", 500, fallback="Consolas, Menlo, monospace"),
}


def font_face_css(key, chars):
    face = FACES[key]
    font = TTFont(face.path)
    opts = subset.Options()
    opts.flavor = "woff"
    opts.name_IDs = ["*"]  # keeps the copyright and OFL licence fields inside the font
    opts.name_languages = ["*"]
    opts.layout_features = ["kern"]
    opts.notdef_outline = True
    sub = subset.Subsetter(opts)
    sub.populate(text="".join(sorted(chars)) + " ")
    sub.subset(font)
    buf = io.BytesIO()
    font.flavor = "woff"
    font.save(buf)
    data = base64.b64encode(buf.getvalue()).decode()
    return f"@font-face{{font-family:{face.family};font-style:{face.style};font-weight:{face.weight};src:url(data:font/woff;base64,{data}) format('woff')}}"


class Svg:
    def __init__(self, w, h, theme):
        self.w, self.h, self.t = w, h, THEMES[theme]
        self.parts, self.defs, self.css, self.used = [], [], [], {}

    def text(self, x, y, s, face, size, fill, anchor="start", ls=0.0, cls=""):
        self.used.setdefault(face, set()).update(s)
        f = FACES[face]
        attrs = f' letter-spacing="{ls}"' if ls else ""
        attrs += f' class="{cls}"' if cls else ""
        self.parts.append(
            f'<text x="{x:.1f}" y="{y:.1f}" font-family="{f.family}, {f.fallback}" font-weight="{f.weight}" font-style="{f.style}" '
            f'font-size="{size}" fill="{fill}" text-anchor="{anchor}"{attrs}>{html.escape(s)}</text>')

    def add(self, s):
        self.parts.append(s)

    def save(self, path, title):
        fonts = "".join(font_face_css(k, v) for k, v in self.used.items())
        css = fonts + "".join(self.css) + "@media (prefers-reduced-motion: reduce){*{animation:none!important}}"
        svg = (f'<svg xmlns="http://www.w3.org/2000/svg" width="{self.w}" height="{self.h}" viewBox="0 0 {self.w} {self.h}" role="img" aria-label="{html.escape(title)}">'
               f'<title>{html.escape(title)}</title><style>{css}</style><defs>{"".join(self.defs)}</defs>{"".join(self.parts)}</svg>')
        path.write_text(svg, encoding="utf-8")
        return len(svg)


def W(face, s, size, ls=0.0):
    return FACES[face].width(s, size, ls)


# ───────────── Data ─────────────
def api(url, data=None):
    headers = {"User-Agent": "profile-widgets", "Accept": "application/vnd.github+json"}
    if TOKEN:
        headers["Authorization"] = f"bearer {TOKEN}"
    req = urllib.request.Request(url, data=json.dumps(data).encode() if data is not None else None, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")[:300]
        raise RuntimeError(f"GitHub API {e.code} for {url}: {detail}") from None


def graphql(query, variables=None):
    res = api("https://api.github.com/graphql", {"query": query, "variables": variables or {}})
    if res.get("errors") and not res.get("data"):
        raise RuntimeError(f"GitHub GraphQL error: {res['errors'][0].get('message')}")
    return res.get("data") or {}


CAL_FIELDS = "contributionsCollection { restrictedContributionsCount contributionCalendar { totalContributions weeks { contributionDays { date contributionCount } } } }"


def contribution_days():
    """Returns ([(date, count)], includes_private, source)."""
    if TOKEN:
        try:
            viewer = graphql("query { viewer { login } }").get("viewer") or {}
        except RuntimeError:
            viewer = {}  # the built-in Actions token is not a user, so it has no viewer
        owner = viewer.get("login", "").lower() == LOGIN.lower()
        if owner:
            cc = graphql(f"query {{ viewer {{ {CAL_FIELDS} }} }}")["viewer"]["contributionsCollection"]
        else:
            user = graphql(f"query($l: String!) {{ user(login: $l) {{ {CAL_FIELDS} }} }}", {"l": LOGIN}).get("user")
            if not user:
                raise RuntimeError(f"GitHub user {LOGIN!r} not found; check \"login\" in widgets/config.json")
            cc = user["contributionsCollection"]
        days = [(d["date"], d["contributionCount"]) for w in cc["contributionCalendar"]["weeks"] for d in w["contributionDays"]]
        return days, owner or cc["restrictedContributionsCount"] > 0, "owner token" if owner else "public API"
    req = urllib.request.Request(f"https://github.com/users/{LOGIN}/contributions", headers={"User-Agent": "Mozilla/5.0"})
    page = urllib.request.urlopen(req, timeout=30).read().decode("utf-8")
    tips = {m.group(1): m.group(2) for m in re.finditer(r'<tool-tip[^>]*for="([^"]+)"[^>]*>([^<]*)</tool-tip>', page)}
    days = []
    for tag in re.findall(r"<td[^>]*data-date[^>]*>", page):
        date = re.search(r'data-date="([\d-]+)"', tag).group(1)
        cid = re.search(r'id="([^"]+)"', tag)
        tip = tips.get(cid.group(1), "") if cid else ""
        m = re.match(r"\s*(\d+) contribution", tip)
        days.append((date, int(m.group(1)) if m else 0))
    return sorted(days), None, "public page"


def clean_title(t):
    t = re.sub(r"^(\[[^\]]*\]\s*)+", "", t).strip()
    t = re.sub(r"^[a-z][\w-]*(\([^)]*\))?:\s*", "", t)
    return t[:1].upper() + t[1:]


def pull_requests():
    oss = CFG["oss"]
    q = f"author:{LOGIN} type:pr -user:{LOGIN} created:>={oss['since']}"
    items = api("https://api.github.com/search/issues?per_page=100&q=" + urllib.parse.quote(q))["items"]
    rows = []
    for it in items:
        repo = it["repository_url"].split("/repos/")[1]
        key = f"{repo}#{it['number']}"
        state = "merged" if (it.get("pull_request") or {}).get("merged_at") else it["state"]
        if key in oss["hide"] or (state == "closed" and not oss["show_closed"]):
            continue
        rows.append(dict(repo=repo, number=it["number"], title=clean_title(it["title"]), url=it["html_url"], state=state, date=it["created_at"][:10], note=""))
    for e in oss["extra"]:
        try:
            pr = api(f"https://api.github.com/repos/{e['repo']}/pulls/{e['number']}")
        except RuntimeError as err:
            print(f"warning: skipped {e['repo']}#{e['number']} from the config extras: {err}", file=sys.stderr)
            continue
        state = "merged" if pr.get("merged_at") else pr["state"]
        rows.append(dict(repo=e["repo"], number=e["number"], title=clean_title(pr["title"]), url=pr["html_url"], state=state, date=pr["created_at"][:10], note=e.get("note", "")))
    order = {"merged": 0, "open": 1, "closed": 2}
    rows.sort(key=lambda r: r["date"], reverse=True)
    rows.sort(key=lambda r: order[r["state"]])
    return rows


# ───────────── Widgets ─────────────
def banner(theme):
    b = CFG["banner"]
    s = Svg(1200, 440, theme)
    t = s.t
    s.add(f'<rect x="0.5" y="0.5" width="1199" height="439" rx="26" fill="{t["bg"]}" stroke="{t["line"]}"/>')
    # Waveform on the right: periodic strands drawn into a mask, painted with a fixed gradient so the colours stay put while the lines flow.
    period, x0, x1, cy = 300, 660, 1200, 214
    strands = [(46, 2.6, 1.0, 11), (36, 1.8, 0.55, 15), (28, 1.5, 0.4, 19), (20, 1.3, 0.28, 24), (13, 1.1, 0.2, 31)]
    paths = []
    for i, (amp, lw, op, dur) in enumerate(strands):
        pts = []
        for k in range(0, 2 * period + (x1 - x0) + 1, 6):
            x = x0 - period + k
            u = 2 * math.pi * x / period
            y = cy + (i - 2) * 9 + amp * (0.55 * math.sin(u + i) + 0.3 * math.sin(2 * u + 1.7 * i) + 0.15 * math.sin(3 * u + 0.6 * i))
            pts.append(f"{x:.1f},{y:.1f}")
        paths.append(f'<g class="f{i}"><polyline points="{" ".join(pts)}" fill="none" stroke="#fff" stroke-opacity="{op}" stroke-width="{lw}" stroke-linejoin="round" stroke-linecap="round"/></g>')
        s.css.append(f".f{i}{{animation:flow{i} {dur}s linear infinite}}@keyframes flow{i}{{to{{transform:translateX(-{period}px)}}}}")
    s.defs.append(f'<linearGradient id="wg" x1="{x0}" y1="0" x2="{x1}" y2="0" gradientUnits="userSpaceOnUse"><stop offset="0" stop-color="{t["a"]}"/><stop offset="0.5" stop-color="{t["b"]}"/><stop offset="1" stop-color="{t["c"]}"/></linearGradient>')
    s.defs.append(f'<linearGradient id="edge" x1="{x0}" y1="0" x2="{x1}" y2="0" gradientUnits="userSpaceOnUse"><stop offset="0" stop-color="#000"/><stop offset="0.34" stop-color="#fff"/><stop offset="0.86" stop-color="#fff"/><stop offset="1" stop-color="#000"/></linearGradient>')
    s.defs.append(f'<mask id="wm" maskUnits="userSpaceOnUse" x="0" y="0" width="1200" height="440">{"".join(paths)}</mask>')
    s.defs.append(f'<mask id="em" maskUnits="userSpaceOnUse" x="0" y="0" width="1200" height="440"><rect x="{x0}" y="0" width="{x1 - x0}" height="440" fill="url(#edge)"/></mask>')
    s.add(f'<g mask="url(#em)"><rect x="{x0}" y="90" width="{x1 - x0}" height="250" fill="url(#wg)" mask="url(#wm)"/></g>')
    # Status pill
    pw = W("sans-m", b["status"], 19) + 58
    s.add(f'<rect x="56" y="50" width="{pw:.0f}" height="42" rx="21" fill="{t["card"]}" stroke="{t["line2"]}"/><circle cx="78" cy="71" r="5" fill="{t["green"]}"/>')
    s.add(f'<circle cx="78" cy="71" r="5" fill="{t["green"]}" class="pulse"/>')
    s.css.append(".pulse{transform-origin:78px 71px;animation:pulse 2.4s ease-out infinite}@keyframes pulse{0%{transform:scale(1);opacity:.6}70%,100%{transform:scale(2.6);opacity:0}}")
    s.text(94, 77.5, b["status"], "sans-m", 19, t["ink"])
    # Site link, top right
    sw = W("mono-m", b["site"] + " ↗", 18) + 40
    s.add(f'<rect x="{1144 - sw:.0f}" y="50" width="{sw:.0f}" height="42" rx="21" fill="{t["soft"]}"/>')
    s.text(1144 - sw / 2, 77, b["site"] + " ↗", "mono-m", 18, t["accent"], anchor="middle")
    # Name, meta, lede, chips
    s.text(50, 222, b["first"], "serif", 140, t["ink"], ls=-3)
    s.text(50 + W("serif", b["first"] + " ", 140, -3), 222, b["last"], "serif-i", 140, t["accent"], ls=-3)
    s.text(56, 268, b["meta"], "mono", 19, t["muted"])
    for i, line in enumerate(b["lede"]):
        s.text(56, 316 + i * 34, line, "sans", 26, t["ink2"])
    x = 56
    for label, value in b["chips"]:
        s.text(x, 404, label, "mono-m", 18, t["ink"])
        x += W("mono-m", label + " ", 18)
        s.text(x, 404, value, "mono", 18, t["muted"])
        x += W("mono", value, 18) + 30
    return s.save(ASSETS / f"banner-{theme}.svg", f"{b['first']} {b['last']}: {b['meta']}. {' '.join(b['lede'])} Visit {b['site']}")


ICONS = {
    "globe": '<circle cx="9" cy="9" r="7.4"/><ellipse cx="9" cy="9" rx="3.3" ry="7.4"/><path d="M1.6 9h14.8"/>',
    "cap": '<path d="M1.4 7 9 3.4 16.6 7 9 10.6Z"/><path d="M4.6 8.7v3.4c0 1.2 2 2.6 4.4 2.6s4.4-1.4 4.4-2.6V8.7"/><path d="M16.6 7v4.2"/>',
    "case": '<rect x="1.6" y="5" width="14.8" height="10.4" rx="2"/><path d="M6.4 5V3.6c0-.6.4-1 1-1h3.2c.6 0 1 .4 1 1V5"/><path d="M1.6 9.6h14.8"/>',
    "medal": '<circle cx="9" cy="11.2" r="4.6"/><path d="M5.6 2.2 9 7.2l3.4-5"/><path d="M9 9.4v3.6"/>',
}


def link_button(link, theme):
    t = THEMES[theme]
    w = 46 + W("sans-m", link["label"], 16) + 20
    s = Svg(round(w), 40, theme)
    s.add(f'<rect x="0.5" y="0.5" width="{round(w) - 1}" height="39" rx="20" fill="{t["card"]}" stroke="{t["line2"]}"/>')
    s.add(f'<g transform="translate(17 11)" fill="none" stroke="{t["accent"]}" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round">{ICONS[link["icon"]]}</g>')
    s.text(46, 25.5, link["label"], "sans-m", 16, t["ink"])
    return s.save(ASSETS / f"link-{link['id']}-{theme}.svg", link["label"])


def lerp_hex(h1, h2, k):
    a = [int(h1[i:i + 2], 16) for i in (1, 3, 5)]
    b = [int(h2[i:i + 2], 16) for i in (1, 3, 5)]
    return "#" + "".join(f"{round(x + (y - x) * k):02x}" for x, y in zip(a, b))


def activity(theme, days, private, today):
    t = THEMES[theme]
    s = Svg(1200, 600, theme)
    counts = [c for _, c in days]
    first = dt.date.fromisoformat(days[0][0])
    start = first - dt.timedelta(days=(first.weekday() + 1) % 7)  # the Sunday on or before the first day
    weeks = []
    for date, c in days:
        offset = (dt.date.fromisoformat(date) - start).days
        while len(weeks) <= offset // 7:
            weeks.append([])
        weeks[offset // 7].append((date, c, offset % 7))
    total, active = sum(counts), sum(1 for c in counts if c)
    longest = run = 0
    for c in counts:
        run = run + 1 if c else 0
        longest = max(longest, run)
    i = len(counts) - 1
    if i >= 0 and counts[i] == 0:
        i -= 1
    current = 0
    while i >= 0 and counts[i] > 0:
        current, i = current + 1, i - 1
    nz = sorted(c for c in counts if c)
    q = [nz[int(len(nz) * f)] if nz else 1 for f in (0.25, 0.5, 0.75)]
    level = lambda c: 0 if c == 0 else 1 + sum(c > x for x in q)

    s.add(f'<rect x="0.5" y="0.5" width="1199" height="599" rx="26" fill="{t["card"]}" stroke="{t["line"]}"/>')
    s.add(f'<circle cx="52" cy="58" r="5" fill="{t["accent"]}"/>')
    s.text(66, 64.5, "GITHUB ACTIVITY", "mono-m", 18, t["muted"], ls=2.4)
    s.text(1148, 64.5, f"Last 12 months  ·  {today.day} {today:%b %Y}", "mono", 18, t["muted"], anchor="end")
    num = f"{total:,}"
    s.text(46, 168, num, "serif", 104, t["ink"], ls=-2)
    s.text(46 + W("serif", num, 104, -2) + 14, 168, "contributions", "sans", 26, t["ink2"])
    if private is not None:
        s.text(50, 206, "Including private work" if private else "Public contributions only", "mono", 18, t["accent"] if private else t["muted"])
    stats = [("Active days", f"{active}", ""), ("Longest streak", f"{longest}", " days" if longest != 1 else " day"), ("Current streak", f"{current}", " days" if current != 1 else " day")]
    for k, (label, value, unit) in enumerate(stats):
        x = 640 + k * 180
        s.text(x, 116, label, "mono", 17, t["muted"])
        s.text(x, 166, value, "serif", 58, t["ink"], ls=-1)
        if unit:
            s.text(x + W("serif", value, 58, -1) + 6, 166, unit.strip(), "sans", 20, t["ink2"])
    # Heatmap
    left, top, right = 46, 272, 1154
    step = (right - left) / max(len(weeks), 1)
    cell = step - 4.2
    last_label_col = -9
    for col, wk in enumerate(weeks):
        d0 = start + dt.timedelta(days=7 * col)
        if d0.day <= 7 and col - last_label_col >= 4:
            s.text(left + col * step, top - 14, f"{d0:%b}", "mono", 16, t["muted"])
            last_label_col = col
        k = col / max(len(weeks) - 1, 1)
        color = lerp_hex(t["a"], t["b"], k * 2) if k < 0.5 else lerp_hex(t["b"], t["c"], (k - 0.5) * 2)
        for date, c, row in wk:
            lv = level(c)
            fill = color if lv else t["empty"]
            op = [1, 0.42, 0.62, 0.82, 1][lv]
            s.add(f'<rect x="{left + col * step:.1f}" y="{top + row * step:.1f}" width="{cell:.1f}" height="{cell:.1f}" rx="3.5" fill="{fill}" fill-opacity="{op}"><title>{c} on {date}</title></rect>')
    # A soft light passes over the grid now and then; at rest it sits off the card.
    s.defs.append('<linearGradient id="sw" x1="0" y1="0" x2="1" y2="0"><stop offset="0" stop-color="#fff" stop-opacity="0"/><stop offset="0.5" stop-color="#fff" stop-opacity="0.55"/><stop offset="1" stop-color="#fff" stop-opacity="0"/></linearGradient>')
    s.defs.append(f'<clipPath id="gc"><rect x="{left}" y="{top}" width="{right - left}" height="{7 * step}"/></clipPath>')
    s.add(f'<g clip-path="url(#gc)"><rect class="sw" x="{left - 180}" y="{top}" width="160" height="{7 * step}" fill="url(#sw)" opacity="{0.55 if theme == "light" else 0.16}"/></g>')
    s.css.append(f".sw{{animation:sw 7s cubic-bezier(0.77,0,0.175,1) 0.6s infinite}}@keyframes sw{{0%{{transform:translateX(0)}}32%,100%{{transform:translateX({right - left + 200}px)}}}}")
    # Weekly pace: a smooth line under the heatmap
    weekly = [sum(c for _, c, _ in wk) for wk in weeks]
    peak = max(weekly) if weekly else 0
    base, height = 534, 66
    pts = [(left + (i + 0.5) * step, base - (height * w / peak if peak else 0)) for i, w in enumerate(weekly)]
    if len(pts) > 1:
        d = f"M{pts[0][0]:.1f},{pts[0][1]:.1f}"
        for i in range(len(pts) - 1):
            p0, p1, p2, p3 = pts[max(i - 1, 0)], pts[i], pts[i + 1], pts[min(i + 2, len(pts) - 1)]
            c1 = (p1[0] + (p2[0] - p0[0]) / 6, p1[1] + (p2[1] - p0[1]) / 6)
            c2 = (p2[0] - (p3[0] - p1[0]) / 6, p2[1] - (p3[1] - p1[1]) / 6)
            d += f" C{c1[0]:.1f},{min(c1[1], base):.1f} {c2[0]:.1f},{min(c2[1], base):.1f} {p2[0]:.1f},{p2[1]:.1f}"
        s.defs.append(f'<linearGradient id="lg" x1="{left}" y1="0" x2="{right}" y2="0" gradientUnits="userSpaceOnUse"><stop offset="0" stop-color="{t["a"]}"/><stop offset="0.5" stop-color="{t["b"]}"/><stop offset="1" stop-color="{t["c"]}"/></linearGradient>')
        s.defs.append(f'<linearGradient id="lf" x1="0" y1="{base - height}" x2="0" y2="{base}" gradientUnits="userSpaceOnUse"><stop offset="0" stop-color="#fff" stop-opacity="0.9"/><stop offset="1" stop-color="#fff" stop-opacity="0"/></linearGradient>')
        s.defs.append(f'<mask id="lm"><path d="{d} L{pts[-1][0]:.1f},{base} L{pts[0][0]:.1f},{base} Z" fill="url(#lf)"/></mask>')
        s.add(f'<rect x="{left}" y="{base - height - 4}" width="{right - left}" height="{height + 4}" fill="url(#lg)" opacity="0.16" mask="url(#lm)"/>')
        s.add(f'<path d="{d}" fill="none" stroke="url(#lg)" stroke-width="2.6" stroke-linecap="round"/>')
        s.add(f'<line x1="{left}" y1="{base}" x2="{right}" y2="{base}" stroke="{t["line"]}" stroke-width="1.2"/>')
        if peak:
            pi = weekly.index(peak)
            px, py = pts[pi]
            s.add(f'<circle cx="{px:.1f}" cy="{py:.1f}" r="5" fill="{t["card"]}" stroke="{t["ink"]}" stroke-width="2"/>')
            label = f"busiest week: {peak}"
            anchor = "end" if px > right - 200 else "start"
            s.text(px + (-12 if anchor == "end" else 12), py - 8, label, "mono", 16, t["ink2"], anchor=anchor)
    s.text(left, 572, "Each week", "mono", 16, t["muted"])
    # Legend
    lx = 1154 - 5 * 21 - W("mono", "More", 16) - 10
    s.text(lx - 10, 572, "Less", "mono", 16, t["muted"], anchor="end")
    for k, op in enumerate([0, 0.42, 0.62, 0.82, 1]):
        fill = t["empty"] if k == 0 else t["b"]
        s.add(f'<rect x="{lx + k * 21:.1f}" y="557" width="16.5" height="16.5" rx="3.5" fill="{fill}" fill-opacity="{op if k else 1}"/>')
    s.text(lx + 5 * 21 + 6, 572, "More", "mono", 16, t["muted"])
    return s.save(ASSETS / f"activity-{theme}.svg", f"GitHub activity: {total} contributions in the last 12 months, {active} active days, longest streak {longest} days")


def oss_table(rows):
    if not rows:
        return "_No open pull requests right now._"
    out = ["| Status | Pull request | What it fixes |", "| :-- | :-- | :-- |"]
    label = {"merged": "**Merged**", "open": "In review", "closed": "Closed"}
    for r in rows:
        note = f" ({r['note']})" if r["note"] else ""
        title = r["title"].replace("|", "\\|")
        out.append(f"| {label[r['state']]} | [{r['repo']}#{r['number']}]({r['url']}) | {title}{note} |")
    return "\n".join(out)


def main():
    ASSETS.mkdir(exist_ok=True)
    today = dt.datetime.now(dt.timezone.utc).date()
    days, private, source = contribution_days()
    if not days:
        raise SystemExit("No contribution days came back from GitHub, so the cards were left unchanged.")
    sizes = {}
    for theme in THEMES:
        sizes[f"banner-{theme}"] = banner(theme)
        for link in CFG["links"]:
            link_button(link, theme)
        sizes[f"activity-{theme}"] = activity(theme, days, private, today)
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    rows = []
    if "<!-- oss:start -->" not in readme or "<!-- oss:end -->" not in readme:
        print("warning: README.md lacks the <!-- oss:start --> and <!-- oss:end --> markers, so the fixes table was not updated", file=sys.stderr)
    else:
        try:
            rows = pull_requests()
            new = re.sub(r"(<!-- oss:start -->)(.*?)(<!-- oss:end -->)", lambda m: f"{m.group(1)}\n{oss_table(rows)}\n{m.group(3)}", readme, flags=re.S)
            (ROOT / "README.md").write_text(new, encoding="utf-8")
        except RuntimeError as err:
            print(f"warning: kept the previous fixes table because the pull request search failed: {err}", file=sys.stderr)
    print(f"calendar from {source}: {len(days)} days, {sum(c for _, c in days)} contributions, private included: {private}")
    print(f"pull requests: {len(rows)}; svg sizes: {sizes}")


if __name__ == "__main__":
    main()
