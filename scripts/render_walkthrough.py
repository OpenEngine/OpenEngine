"""Render the graphs-and-loops walkthrough video (docs/images/oe-graphs-and-loops.mp4).

The terminal scenes are scripted to match the CLI's output formats; the GitHub
scenes scroll through headless-Chrome captures of real issues and pull requests.
Needs macOS (Menlo, SF and Apple Symbols fonts, Google Chrome) and ffmpeg:

    uv run --with pillow --with fonttools python scripts/render_walkthrough.py docs/images/oe-graphs-and-loops.mp4

Set ELEVENLABS_API_KEY to narrate each subtitle (OE_WALKTHROUGH_VOICE picks the voice);
clips are cached in the temp directory, and without either the video renders silent.
Pass comma-separated times as a second argument to write still PNGs instead of a video.
"""
import hashlib
import json
import math
import os
import re
import subprocess
import sys
import tempfile
import urllib.request
from pathlib import Path

from fontTools.ttLib import TTFont
from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageFont

W, H, FPS = 1920, 1080, 30
OUT = sys.argv[1]
STILLS = [float(x) for x in sys.argv[2].split(",")] if len(sys.argv) > 2 else None

# Catppuccin Mocha
BG = (30, 30, 46)
MANTLE = (24, 24, 37)
FG = (205, 214, 244)
DIM = (127, 132, 156)
GREEN = (166, 227, 161)
CYAN = (137, 220, 235)
YELLOW = (249, 226, 175)
RED = (243, 139, 168)
MAUVE = (203, 166, 247)
BLUE = (137, 180, 250)
PEACH = (250, 179, 135)
ORANGE = (249, 115, 22)

FS = 25
MENLO = "/System/Library/Fonts/Menlo.ttc"
SYMBOLS = "/System/Library/Fonts/Apple Symbols.ttf"
SF = "/System/Library/Fonts/SFNS.ttf"
mono = ImageFont.truetype(MENLO, FS, index=0)
mono_b = ImageFont.truetype(MENLO, FS, index=1)
sym = ImageFont.truetype(SYMBOLS, FS + 4)
braille = ImageFont.truetype(SYMBOLS, FS + 9)
menlo_cmap = TTFont(MENLO, fontNumber=0).getBestCmap()
sym_cmap = TTFont(SYMBOLS).getBestCmap()


def sf(size, weight):
    f = ImageFont.truetype(SF, size)
    # SF axes: width, optical size, grade, weight
    f.set_variation_by_axes([100, min(max(size, 17), 96), 400, weight])
    return f


SUB_FONT = sf(40, 560)
TITLE_FONT = sf(96, 760)
TAG_FONT = sf(44, 430)
SMALL_FONT = sf(30, 500)

CW = mono.getlength("M")
LH = int(FS * 1.5)

# Window geometry
WX, WY, WW, WH = 110, 50, W - 220, 820
PAD_X, PAD_Y, BAR = 34, 26, 52
COLS = int((WW - 2 * PAD_X) // CW)
ROWS = int((WH - BAR - 2 * PAD_Y) // LH)


# ---------- voiceover ----------
# ElevenLabs narration, one clip per subtitle. Without ELEVENLABS_API_KEY (and no cached
# clips) the video renders silent with the original timing.
VOICE = os.environ.get("OE_WALKTHROUGH_VOICE", "nPczCjzI2devNBz1zQrb")  # premade "Brian"
VOICE_MODEL = "eleven_multilingual_v2"
VOICE_DIR = Path(tempfile.gettempdir()) / "oe-walkthrough-voice"
VOICE_GAP = 0.35  # silence kept after each line before the next one starts
# Subtitles that read differently aloud
SPOKEN = {
    "It pins the graph version and pauses at the PR or spend limit you set.":
        "It pins the graph version, and pauses at the pull request or spend limit you set.",
    "See every loop: its cadence, PRs opened and spend against the cap.":
        "See every loop: its cadence, pull requests opened, and spend against the cap.",
    "@-mention it on a PR, and the request goes back to the same run.":
        "Mention it on a pull request, and the request goes back to the same run.",
}


def clip_duration(path):
    out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)],
                         check=True, capture_output=True, text=True)
    return float(out.stdout)


def voice_clip(text):
    """The narration clip for a line and its length, or None when no voice is available."""
    key = hashlib.sha1(f"{VOICE}|{VOICE_MODEL}|{text}".encode()).hexdigest()[:16]
    path = VOICE_DIR / f"{key}.mp3"
    if not path.exists():
        api_key = os.environ.get("ELEVENLABS_API_KEY")
        if not api_key:
            return None
        VOICE_DIR.mkdir(exist_ok=True)
        request = urllib.request.Request(
            f"https://api.elevenlabs.io/v1/text-to-speech/{VOICE}?output_format=mp3_44100_128",
            data=json.dumps({"text": text, "model_id": VOICE_MODEL}).encode(),
            headers={"xi-api-key": api_key, "Content-Type": "application/json"},
        )
        with urllib.request.urlopen(request) as response:
            path.write_bytes(response.read())
    return path, clip_duration(path)


# ---------- timeline ----------
class Script:
    def __init__(self):
        self.t = 0.0
        self.lines = []  # list of list[(text, color, bold)]
        self.snaps = []  # (time, lines, cursor_visible)
        self.subs = []   # (start, end, text)
        self._sub = None
        self.cards = []  # (start, end, kind)
        self.browsers = []  # dicts: t0, t1, name, url, path [(t, y)], hl [(ta, tb, rect)]
        self.voices = []  # (start, clip path)
        self._voice_until = 0.0

    def say(self, text):
        """Start a narration line now; later lines wait for it to finish."""
        self.await_voice()
        clip = voice_clip(text)
        if clip:
            self.voices.append((self.t, clip[0]))
            self._voice_until = self.t + clip[1] + VOICE_GAP

    def await_voice(self):
        """Hold the current frame until the narration line in progress has finished."""
        self.t = max(self.t, self._voice_until)

    def browser(self, name, url, stops, scroll=1.1):
        """Show a captured page, scrolling to each stop: (y, hold, subtitle, highlight rects)."""
        scene = {"t0": self.t, "name": name, "url": url, "path": [], "hl": []}
        for i, (y, hold, sub, rects) in enumerate(stops):
            if i:
                self.t += scroll
            if sub:
                self.sub(sub)
            scene["path"].append((self.t, y))
            for r in rects:
                scene["hl"].append((self.t + 0.3, self.t + hold, r))
            self.t += hold
            self.await_voice()
            scene["path"].append((self.t, y))
        scene["t1"] = self.t
        self.browsers.append(scene)

    def snap(self, cursor=True):
        self.snaps.append((self.t, [list(line) for line in self.lines], cursor))

    def wait(self, s):
        self.t += s

    def sub(self, text):
        self.await_voice()
        if self._sub:
            self.subs.append((self._sub[0], self.t, self._sub[1]))
        self._sub = (self.t, text) if text else None
        if text:
            self.say(SPOKEN.get(text, text))

    def prompt(self):
        self.lines.append([("~/code/openengine ", BLUE, True), ("main ", MAUVE, False), ("❯ ", GREEN, True)])
        self.snap()

    def type(self, text, cps=28):
        for ch in text:
            last = self.lines[-1]
            last.append((ch, FG, False))
            self.t += 1 / cps * (2.2 if ch == " " else 1)
            self.snap()
        self.t += 0.35

    def enter(self):
        self.lines.append([])
        self.snap(False)

    def out(self, lines, delay=0.0):
        for line in lines:
            self.lines.append(line)
            if delay:
                self.t += delay
                self.snap(False)
        self.snap(False)

    def region(self, start, lines):
        """Redraw everything from line index `start` (like the live view)."""
        self.lines = self.lines[:start] + [list(line) for line in lines]
        self.snap(False)

    def clear(self):
        self.lines = []
        self.snap()

    def finish(self):
        self.sub(None)


def seg(text, color=FG, bold=False):
    return (text, color, bold)


SPIN = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"


def mark(status, tick):
    return {
        "done": seg("✓", GREEN),
        "active": seg(SPIN[tick % len(SPIN)], CYAN),
        "pending": seg("○", DIM),
    }[status]


def yaml_line(text):
    """Tiny YAML highlighter: keys, strings, comments."""
    stripped = text.lstrip()
    indent = text[: len(text) - len(stripped)]
    if stripped.startswith("#"):
        return [seg(text, DIM)]
    if ":" in stripped:
        key, _, rest = stripped.partition(":")
        color = MAUVE if not indent else BLUE
        out = [seg(indent), seg(key, color, not indent), seg(":", DIM)]
        rest_color = GREEN if rest.strip().startswith(("[", "{")) is False and rest.strip() else PEACH
        if rest.strip().startswith("{"):
            rest_color = PEACH
        return out + [seg(rest, rest_color)]
    return [seg(text, GREEN)]


def table(s, headers, rows, widths, colors, delay=0.07):
    head = [seg(f"{h:<{w}}", DIM, True) for h, w in zip(headers, widths)]
    s.out([head], delay=delay)
    for r in rows:
        s.out([[seg(f"{v:<{w}}", c) for v, w, c in zip(r, widths, colors)]], delay=delay)


def watch_frame(elapsed, tick, review):
    a, b = ("active", "active") if review == 0 else ("done", "active") if review == 1 else ("done", "done")
    lines = [[seg(f"multi-tenancy v2 · run-3f42e39c7ac9 · running · {int(elapsed // 60)}:{int(elapsed % 60):02d}", FG, True)], []]
    lines.append([seg("  "), mark("done", tick), seg(" Plan", GREEN), seg(" ─── ", DIM), mark("done", tick),
                  seg(" Implement", GREEN), seg(" ─── ", DIM), mark("active", tick), seg(" Review", CYAN, True)])
    lines.append([])
    g1 = [seg("  Graph  ", DIM)]
    for i, n in enumerate(["workspace", "pick", "scope", "implement", "publish", "ci"]):
        g1 += [mark("done", tick), seg(" " + n), seg(" → ", DIM)]
    lines.append(g1)
    lines.append([seg("         "), mark(a, tick), seg(" adversarial", FG, a == "active"), seg(" + ", DIM),
                  mark(b, tick), seg(" bugs", FG, b == "active"), seg(" + ", DIM), mark("done", tick), seg(" cli-review"),
                  seg(" → ", DIM), mark("pending", tick), seg(" refute"), seg(" → ", DIM), mark("pending", tick), seg(" reranker")])
    lines.append([seg("  Now    ", DIM), seg("reviewers-bugs · codex · attempt 1", CYAN)])
    lines.append([seg("  ✓ ci · all 14 checks passed on PR #1243", DIM)])
    lines.append([])
    lines.append([seg("  ┃ ", DIM), seg("Tracing project scoping through registry queries for a cross-project leak.")])
    lines.append([seg("  ┃ ", DIM), seg("⚙ Read packages/registry/queries.py", YELLOW)])
    return lines


def build():
    s = Script()
    s.t = 0.3
    s.say("OpenEngine. Graphs define the work. Loops keep it going.")
    s.t = 3.2
    s.await_voice()
    s.cards.append((0.0, s.t + 0.4, "title"))

    # 1. A graph
    s.prompt()
    s.wait(0.4)
    s.sub("A graph is a workflow of agent steps, written in YAML.")
    s.type("cat graphs/dependabot-ci-fix.yaml")
    s.enter()
    yaml_src = [
        "name: dependabot-ci-fix",
        "description: Fix Dependabot PRs whose CI is failing.",
        "plan:",
        "  triage:",
        "    agent: claude",
        "    model: sonnet",
        "    prompt: Find open Dependabot PRs with failing CI ...",
        "implementation:",
        "  fix:",
        "    agent: claude",
        "    prompt: Push a fix onto each PR's own branch ...",
        "review:",
        "  verify:",
        "    agent: {not: fix}",
        "    prompt: Wait for CI and confirm each PR goes green ...",
        "loop:",
        "  every: 8h",
    ]
    for line in yaml_src:
        s.out([yaml_line(line)], delay=0.05)
    s.wait(1.6)
    s.sub("Each step picks its agent and prompt. Here a different agent checks the fix.")
    s.wait(3.2)

    # 2. Register
    s.clear()
    s.prompt()
    s.sub("Register it. Every edit becomes a new version.")
    s.type("oe graph add graphs/dependabot-ci-fix.yaml")
    s.enter()
    s.out([[seg("registered ", GREEN, True), seg("dependabot-ci-fix v2 "), seg("(g-f577c59e40ad, version gv-3e8b0d71a2c4) in project default", DIM)]])
    s.wait(1.4)
    s.out([[]])
    s.prompt()
    s.sub("Build up a library of graphs for the work you repeat.")
    s.type("oe graphs")
    s.enter()
    table(s, ["NAME", "VERSION", "DESCRIPTION"], [
        ("dependabot-ci-fix", "2", "Fix Dependabot PRs whose CI is failing."),
        ("implementation-review", "3", "Implement, check CI, review in parallel, rerank."),
        ("issue-label-grooming", "2", "Size and label open issues from the taxonomy."),
        ("multi-tenancy", "2", "Work a milestone one reviewed PR at a time."),
        ("security-review", "4", "Probe the API, reproduce findings, file issues."),
        ("tdd-guide", "5", "One agent writes failing tests, another passes them."),
        ("v2-parity", "2", "Work the V2 parity tracker, ticket by ticket."),
    ], [24, 9, 60], [FG, FG, DIM])
    s.wait(3.6)

    # 3. Loops
    s.clear()
    s.prompt()
    s.sub("A loop runs a graph on a schedule, like grooming tickets every day.")
    s.type("oe loop add issue-label-grooming --every 1d --max-spend 20")
    s.enter()
    s.out([[seg("issue-label-grooming (loop-9621f0af7ae7)  "), seg("active", GREEN, True)],
           [seg("graph issue-label-grooming v2 every 1d, next 2026-10-30T21:09:01+00:00", DIM)],
           [seg("pull requests 0/∞, spend $0.00/$20.00", DIM)]], delay=0.1)
    s.wait(1.6)
    s.sub("It pins the graph version and pauses at the PR or spend limit you set.")
    s.wait(3.0)
    s.out([[]])
    s.prompt()
    s.sub("See every loop: its cadence, PRs opened and spend against the cap.")
    s.type("oe loops")
    s.enter()
    table(s, ["NAME", "STATE", "EVERY", "ACTIVE RUN", "PRS", "SPEND"], [
        ("dependabot-ci-fix", "active", "8h", "-", "0/1", "$0.00/$50.00"),
        ("issue-label-grooming", "active", "1d", "-", "0/∞", "$0.00/$20.00"),
        ("multi-tenancy", "active", "1h", "run-3f42e39c7ac9", "1/25", "$0.02/$100.00"),
        ("v2-parity", "active", "1h", "-", "5/30", "$8.18/$100.00"),
    ], [23, 9, 7, 19, 7, 14], [FG, GREEN, FG, CYAN, FG, PEACH])
    s.wait(3.6)

    # 4. Watch a loop's run
    s.clear()
    s.prompt()
    s.sub("Watch any run live, including one a loop started.")
    s.type("oe run watch run-3f42e39c7ac9")
    s.enter()
    base = len(s.lines)
    tick, elapsed = 0, 25 * 60 + 10
    for review, dur in ((0, 2.6), (1, 2.6)):
        if review == 1:
            s.sub("Reviewers attack the change in parallel; a refute step tests every finding.")
        for _ in range(int(dur / 0.12)):
            s.region(base, watch_frame(elapsed, tick, review))
            s.wait(0.12)
            tick += 1
            elapsed += 1
    s.wait(1.4)

    # 5. What a loop decided, and control
    s.clear()
    s.prompt()
    s.sub("Each run reports what it did.")
    s.type("oe loop get issue-label-grooming")
    s.enter()
    s.out([[seg("issue-label-grooming (loop-9621f0af7ae7)  "), seg("active", GREEN, True)],
           [seg("graph issue-label-grooming v2 every 1d, next 2026-10-30T21:09:01+00:00", DIM)],
           [],
           [seg("latest output: apply of run-55f7e7a81c31", DIM)],
           [seg("summary: ", BLUE), seg("Applied all 25 proposed label additions to OpenEngine issues;")],
           [seg("all verified by read-back, no repo files touched.")],
           [],
           [seg("#1221  ", PEACH), seg("+bug +security +size:L", GREEN)],
           [seg("       security: ", BLUE), seg("reproduced exploit against an unauthenticated broker", DIM)],
           [seg("       size:L: ", BLUE), seg("spans the supervisor, messaging and every consumer", DIM)]], delay=0.08)
    s.wait(1.4)
    s.sub("Labels come with the evidence behind them.")
    s.wait(2.6)

    # 6. On GitHub
    s.clear()
    s.browser("issue1046", "github.com/OpenEngine/OpenEngine/issues/1046", [
        (0, 2.6, "On GitHub, assign an issue to OpenEngine to start work.", [(1270, 450, 1490, 500), (1275, 545, 1495, 592)]),
        (1330, 3.4, "It picks the issue up, starts a run and links it.", [(140, 1693, 640, 1737), (115, 1770, 1250, 1945)]),
    ])
    s.browser("pr1217", "github.com/OpenEngine/OpenEngine/pull/1217", [
        (0, 2.4, "Its pull requests say what changed and how it was tested.", []),
        (2000, 3.6, "Adversarial reviewers post findings on the exact line.", [(225, 2390, 1205, 2640)]),
        (2440, 3.6, "An impact analysis rates the risk and says what to test.", [(195, 2752, 1210, 3170)]),
    ])
    s.browser("pr1214", "github.com/OpenEngine/OpenEngine/pull/1214", [
        (0, 1.6, "Reviewers can reply to it like a teammate.", []),
        (2330, 4.2, "@-mention it on a PR, and the request goes back to the same run.", [(195, 2375, 1210, 2537), (195, 2580, 1210, 2690)]),
    ])
    s.clear()
    s.prompt()
    s.sub("Pause, resume or stop any loop when you need to.")
    s.type('oe loop pause v2-parity --reason "release freeze"')
    s.enter()
    s.out([[seg("v2-parity (loop-3f6a31967303)  "), seg("paused", YELLOW, True), seg(": release freeze")],
           [seg("graph v2-parity v2 every 1h, next -", DIM)],
           [seg("pull requests 5/30, spend $8.18/$100.00", DIM)]], delay=0.08)
    s.wait(3.2)
    s.sub(None)
    end = s.t
    s.t += 0.4
    s.say("Write a graph, put it on a loop, and get started at openengine dot c c.")
    s.t = end + 3.6
    s.await_voice()
    s.t += 0.6
    s.cards.append((end, s.t, "end"))
    s.finish()
    return s


# ---------- drawing ----------
def background():
    img = Image.new("RGB", (W, H))
    px = img.load()
    for y in range(H):
        for x in range(0, W):
            u, v = x / W, y / H
            r = int(17 + 30 * u + 10 * v)
            g = int(17 + 8 * v)
            b = int(38 + 40 * (1 - u) + 20 * v)
            px[x, y] = (r, g, b)
    return img


def window_base(bg):
    img = bg.copy()
    shadow = Image.new("L", (W, H), 0)
    ImageDraw.Draw(shadow).rounded_rectangle((WX, WY + 18, WX + WW, WY + WH + 18), 22, fill=170)
    shadow = shadow.filter(ImageFilter.GaussianBlur(28))
    img.paste((5, 5, 12), (0, 0), shadow)
    d = ImageDraw.Draw(img)
    d.rounded_rectangle((WX, WY, WX + WW, WY + WH), 18, fill=BG, outline=(69, 71, 90), width=2)
    d.rounded_rectangle((WX + 1, WY + 1, WX + WW - 1, WY + BAR), 18, fill=MANTLE)
    d.rectangle((WX + 1, WY + BAR - 18, WX + WW - 1, WY + BAR), fill=MANTLE)
    for i, c in enumerate([(243, 139, 168), (249, 226, 175), (166, 227, 161)]):
        cx = WX + 30 + i * 30
        d.ellipse((cx - 8, WY + BAR // 2 - 8, cx + 8, WY + BAR // 2 + 8), fill=c)
    title = "oe — ~/code/openengine"
    tw = SMALL_FONT.getlength(title) * 0.8
    d.text((WX + WW / 2 - tw / 2, WY + BAR / 2), title, font=sf(24, 500), fill=DIM, anchor="lm")
    return img


def draw_char(d, x, y, ch, color, bold):
    cp = ord(ch)
    if cp in menlo_cmap:
        d.text((x, y), ch, font=mono_b if bold else mono, fill=color)
    elif 0x2800 <= cp <= 0x28FF:
        d.text((x + CW / 2, y + FS * 0.82), ch, font=braille, fill=color, anchor="mm")
    elif cp in sym_cmap:
        d.text((x + CW / 2, y + FS * 0.62), ch, font=sym, fill=color, anchor="mm")
    else:
        d.text((x, y), ch, font=mono, fill=color)


def draw_terminal(img, lines, cursor_on):
    d = ImageDraw.Draw(img)
    visible = lines[-ROWS:]
    x0, y0 = WX + PAD_X, WY + BAR + PAD_Y
    cx = cy = None
    for r, line in enumerate(visible):
        col = 0
        for text, color, bold in line:
            for ch in text:
                if col >= COLS:
                    break
                if ch != " ":
                    draw_char(d, x0 + col * CW, y0 + r * LH, ch, color, bold)
                col += 1
        cx, cy = col, r
    if cursor_on and cy is not None:
        x = x0 + cx * CW
        y = y0 + cy * LH
        d.rectangle((x, y + 2, x + CW - 1, y + FS + 6), fill=(245, 224, 220))


def draw_sub(img, text, alpha):
    if not text or alpha <= 0:
        return
    layer = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    tw = SUB_FONT.getlength(text)
    cx, cy = W / 2, WY + WH + (H - WY - WH) / 2 + 2
    d.rounded_rectangle((cx - tw / 2 - 30, cy - 38, cx + tw / 2 + 30, cy + 38), 16, fill=(10, 10, 20, int(190 * alpha)))
    d.text((cx, cy), text, font=SUB_FONT, fill=(255, 255, 255, int(255 * alpha)), anchor="mm")
    img.paste(layer, (0, 0), layer)


LOGO_SVG = Path(__file__).resolve().parents[1] / "site/static/favicon.svg"
_logo = {}


def logo(size):
    """The OpenEngine mark from the site favicon: straight-line subpaths plus one circle, filled even-odd."""
    if size not in _logo:
        k = 8 * size / 100
        source = LOGO_SVG.read_text()
        path = re.search(r' d="([^"]+)"', source).group(1)
        fill = tuple(int(c, 16) for c in re.findall(r"[0-9a-f]{2}", re.search(r'rect[^>]*fill="#([0-9a-f]{6})"', source).group(1)))

        def point(x, y):
            return (16 + 0.68 * x) * k, (16 + 0.68 * y) * k

        mask = Image.new("L", (8 * size, 8 * size), 0)
        for sub in re.findall(r"M[^M]+", path):
            shape = Image.new("L", mask.size, 0)
            if "a" in sub:
                x, y = map(float, re.match(r"M([\d.]+) ([\d.]+)", sub).groups())
                r = float(re.search(r"a([\d.]+)", sub).group(1))
                (x0, y0), (x1, y1) = point(x, y - r), point(x + 2 * r, y + r)
                ImageDraw.Draw(shape).ellipse((x0, y0, x1, y1), fill=255)
            else:
                pts = [point(float(a), float(b)) for a, b in re.findall(r"([\d.]+) ([\d.]+)", sub)]
                ImageDraw.Draw(shape).polygon(pts, fill=255)
            mask = ImageChops.difference(mask, shape)
        tile = Image.new("RGBA", mask.size, (*fill, 255))
        ImageDraw.Draw(tile).bitmap((0, 0), mask, fill=(255, 255, 255, 255))
        corners = Image.new("L", mask.size, 0)
        ImageDraw.Draw(corners).rounded_rectangle((0, 0, *mask.size), 18 * k, fill=255)
        tile.putalpha(corners)
        _logo[size] = tile.resize((size, size), Image.LANCZOS)
    return _logo[size]


def draw_card(img, kind, alpha):
    layer = Image.new("RGBA", (W, H), (17, 17, 32, int(255 * alpha)))
    d = ImageDraw.Draw(layer)
    a = int(255 * alpha)
    # Both cards share the logo, title and site line; the middle differs.
    mark = Image.new("RGBA", layer.size, (0, 0, 0, 0))
    mark.paste(logo(150), (W // 2 - 75, H // 2 - 335))
    mark.putalpha(ImageChops.multiply(mark.getchannel("A"), Image.new("L", layer.size, a)))
    layer.alpha_composite(mark)
    d.text((W / 2, H / 2 - 100), "OpenEngine", font=TITLE_FONT, fill=(*FG, a), anchor="mm")
    d.text((W / 2, H / 2 + 220), "openengine.cc", font=TAG_FONT, fill=(*ORANGE, a), anchor="mm")
    if kind == "title":
        d.text((W / 2, H / 2 + 55), "Graphs define the work. Loops keep it going.", font=TAG_FONT, fill=(*FG, a), anchor="mm")
    else:
        cmd1 = "oe graph add my-graph.yaml"
        cmd2 = "oe loop add my-graph --every 1d --max-spend 20"
        f = ImageFont.truetype(MENLO, 36)
        for i, (c, col) in enumerate([(cmd1, GREEN), (cmd2, CYAN)]):
            y = H / 2 + 20 + i * 70
            d.text((W / 2, y), c, font=f, fill=(*col, a), anchor="mm")
    img.paste(layer, (0, 0), layer)


CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
GH = Path(tempfile.gettempdir()) / "oe-walkthrough-pages"
# Captured page name -> (path on github.com/OpenEngine/OpenEngine, window height in CSS pixels)
PAGES = {"issue1046": ("issues/1046", 2100), "pr1217": ("pull/1217", 3700), "pr1214": ("pull/1214", 3300)}
# Regions to blur in a capture (capture pixels), such as private run URLs
BLUR = {"pr1214": [(565, 2640, 1110, 2684)]}


def capture(name):
    """Screenshot a public GitHub page in dark mode at 1.25x."""
    path, height = PAGES[name]
    GH.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory() as profile:
        subprocess.run([
            CHROME, "--headless=new", "--disable-gpu", "--hide-scrollbars", "--force-dark-mode",
            "--force-device-scale-factor=1.25", "--virtual-time-budget=15000", f"--window-size=1400,{height}",
            f"--user-data-dir={profile}", f"--screenshot={GH / f'{name}.png'}",
            f"https://github.com/OpenEngine/OpenEngine/{path}",
        ], check=True, capture_output=True)
CONTENT = (WX + 2, WY + BAR, WX + WW - 2, WY + WH - 2)
_pages = {}


def page(name):
    if name not in _pages:
        if not (GH / f"{name}.png").exists():
            capture(name)
        im = Image.open(GH / f"{name}.png").convert("RGB")
        for box in BLUR.get(name, []):
            im.paste(im.crop(box).filter(ImageFilter.GaussianBlur(9)), box[:2])
        w = CONTENT[2] - CONTENT[0]
        _pages[name] = (im.resize((w, int(im.height * w / im.width)), Image.LANCZOS), w / im.width)
    return _pages[name]


def ease(u):
    return u * u * (3 - 2 * u)


def browser_base(bg, url):
    img = window_base(bg)
    d = ImageDraw.Draw(img)
    d.rectangle((WX + 300, WY + 6, WX + WW - 300, WY + BAR - 6), fill=MANTLE)
    pill = (WX + WW / 2 - 380, WY + 10, WX + WW / 2 + 380, WY + BAR - 10)
    d.rounded_rectangle(pill, 10, fill=(49, 50, 68))
    d.text((WX + WW / 2, WY + BAR / 2), url, font=sf(22, 500), fill=FG, anchor="mm")
    return img


_mask = None


def content_mask():
    global _mask
    if _mask is None:
        w, h = CONTENT[2] - CONTENT[0], CONTENT[3] - CONTENT[1]
        _mask = Image.new("L", (w, h), 0)
        ImageDraw.Draw(_mask).rounded_rectangle((0, -30, w - 1, h - 1), 16, fill=255)
    return _mask


def draw_browser(img, scene, t, bases):
    pim, scale = page(scene["name"])
    path = scene["path"]
    y = path[-1][1]
    for (ta, ya), (tb, yb) in zip(path, path[1:]):
        if ta <= t <= tb:
            y = ya if tb == ta else ya + (yb - ya) * ease((t - ta) / (tb - ta))
            break
    h = CONTENT[3] - CONTENT[1]
    top = int(min(max(y * scale, 0), pim.height - h))
    img.paste(bases[scene["url"]], (0, 0))
    view = pim.crop((0, top, pim.width, top + h))
    layer = Image.new("RGBA", view.size, (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    for ta, tb, (x0, y0, x1, y1) in scene["hl"]:
        if ta <= t < tb:
            a = min(1, (t - ta) / 0.3, (tb - t) / 0.3)
            box = (x0 * scale, y0 * scale - top, x1 * scale, y1 * scale - top)
            d.rounded_rectangle(box, 10, fill=(203, 166, 247, int(28 * a)), outline=(203, 166, 247, int(255 * a)), width=3)
    view.paste(layer, (0, 0), layer)
    img.paste(view, (CONTENT[0], CONTENT[1]), content_mask())


def main():
    s = build()
    total = s.t
    bg = background()
    base = window_base(bg)
    bases = {sc["url"]: browser_base(bg, sc["url"]) for sc in s.browsers}
    audio = []
    for _, clip in s.voices:
        audio += ["-i", str(clip)]
    if s.voices:
        delays = "".join(f"[{i + 1}:a]adelay={int(start * 1000)}:all=1[v{i}];" for i, (start, _) in enumerate(s.voices))
        mix = "".join(f"[v{i}]" for i in range(len(s.voices)))
        audio += ["-filter_complex", f"{delays}{mix}amix=inputs={len(s.voices)}:normalize=0[vo]",
                  "-map", "0:v", "-map", "[vo]", "-c:a", "aac", "-b:a", "160k", "-t", f"{total:.2f}"]
    proc = None if STILLS is not None else subprocess.Popen([
        "ffmpeg", "-y", "-loglevel", "error", "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{W}x{H}",
        "-r", str(FPS), "-i", "-", *audio, "-c:v", "libx264", "-preset", "slow", "-crf", "18", "-pix_fmt", "yuv420p",
        "-movflags", "+faststart", OUT,
    ], stdin=subprocess.PIPE)
    si = 0
    n = int(total * FPS)
    for f in range(n):
        t = f / FPS
        while si + 1 < len(s.snaps) and s.snaps[si + 1][0] <= t:
            si += 1
        img = base.copy()
        if s.snaps and s.snaps[0][0] <= t:
            _, lines, cur = s.snaps[si]
            blink = cur and (math.floor(t * 1.8) % 2 == 0 or (si + 1 < len(s.snaps) and s.snaps[si + 1][0] - t < 0.3))
            draw_terminal(img, lines, blink)
        for scene in s.browsers:
            if scene["t0"] <= t < scene["t1"]:
                draw_browser(img, scene, t, bases)
        for a, b, text in s.subs:
            if a <= t < b:
                alpha = min(1, (t - a) / 0.25, (b - t) / 0.25)
                draw_sub(img, text, alpha)
        for a, b, kind in s.cards:
            if a <= t < b:
                fade = 0.6
                alpha = min(1, (t - a) / fade if kind == "end" else 1, (b - t) / fade if kind == "title" else 1)
                draw_card(img, kind, max(0, alpha))
        if STILLS is not None:
            if any(abs(t - x) < 0.5 / FPS for x in STILLS):
                img.save(f"still_{t:05.1f}.png")
            continue
        proc.stdin.write(img.tobytes())
    if proc:
        proc.stdin.close()
        proc.wait()
    print(f"{total:.1f}s, {n} frames -> {OUT}")


if __name__ == "__main__":
    main()
