"""Generates the architecture diagrams of the documentation as SVG.

All diagrams share one visual language (UML notation, the Logos brand colours) and
use the container names from docker-compose.yaml and logos-workernode/docker-compose.yml.

Usage (from the repository root):
    python3 docs/static/img/system-design/generate.py

Then check the result (see docs/AGENTS.md, "Diagrams"):
    cd docs && NODE_PATH=../logos-ui/node_modules node static/img/system-design/check-diagrams.cjs

Sizes: the docs show a diagram at most 823 px wide. Text must render at 10 px or more
there, so the smallest font size of a diagram is 10 * viewBox width / 823.
"""

import math
import os

HERE = os.path.dirname(os.path.abspath(__file__))
IMG = os.path.dirname(HERE)

INK, INK2, MUTED, SIGNAL, TINT = "#241c3b", "#3b3160", "#5f5688", "#7c3aed", "#ede9fe"
FRAME_FILL = "rgba(237,233,254,0.55)"
FONT = "Inter, -apple-system, BlinkMacSystemFont, 'Segoe UI', Helvetica, Arial, sans-serif"
COLUMN_PX = 823
MIN_PX = 10


def min_font(width):
    return math.ceil(MIN_PX * width / COLUMN_PX)


class Svg:
    def __init__(self, width, height, title, label, pad=(30, 24)):
        self.w, self.h, self.pad = width, height, pad
        self.out = []
        self.f = min_font(width + 2 * pad[0])  # smallest font size of this diagram
        self.title, self.label = title, label

    def add(self, s):
        self.out.append(s)

    # --- primitives -------------------------------------------------------------
    def text(self, x, y, s, size=None, weight=400, fill=INK, anchor="middle", italic=False):
        size = size or self.f
        style = ' font-style="italic"' if italic else ""
        self.add(
            f'<text x="{x}" y="{y}" text-anchor="{anchor}" font-size="{size}" '
            f'font-weight="{weight}" fill="{fill}"{style}>{s}</text>'
        )

    def icon(self, x, y):
        self.add(
            f'<g transform="translate({x} {y})" fill="#fff" stroke="{INK}" stroke-width="1.6">'
            '<rect x="4" y="0" width="16" height="20"/><rect x="0" y="4" width="8" height="4"/>'
            '<rect x="0" y="12" width="8" height="4"/></g>'
        )

    def comp(self, x, y, w, h, st, name, subs=(), top=False, fill="#fff", dashed=False, icon=True, name_size=None):
        """UML component or subsystem: stereotype, name and optional sub lines."""
        subs = [subs] if isinstance(subs, str) else list(subs)
        dash = ' stroke-dasharray="8 6"' if dashed else ""
        self.add(
            f'<rect x="{x}" y="{y}" width="{w}" height="{h}" fill="{fill}" stroke="{INK}" stroke-width="2"{dash}/>'
        )
        if icon:
            self.icon(x + w - 32, y + 11)
        ns = name_size or self.f + 4
        lh = self.f + 4
        block = (self.f + 6 if st else 0) + ns + lh * len(subs)
        cy = y + 22 if top else y + (h - block) / 2
        if st:
            cy += self.f
            self.text(x + w / 2, cy, st, self.f, 400, INK2)
            cy += 6
        cy += ns
        self.text(x + w / 2, cy, name, ns, 700)
        for line in subs:
            cy += lh
            self.text(x + w / 2, cy, line, self.f, 400, MUTED)

    def frame(self, x, y, w, h, st, name, name_x=None):
        self.add(
            f'<rect x="{x}" y="{y}" width="{w}" height="{h}" fill="{FRAME_FILL}" stroke="{SIGNAL}" stroke-width="2.5"/>'
        )
        self.icon(x + w - 36, y + 12)
        nx = name_x if name_x is not None else x + w / 2
        self.text(nx, y + 12 + self.f, st, self.f, 400, INK2)
        self.text(nx, y + 18 + self.f + self.f + 8, name, self.f + 8, 800, SIGNAL)

    def badge(self, x, y, label):
        w = 18 + len(label) * (self.f * 0.62)
        self.add(
            f'<rect x="{x}" y="{y}" width="{w:.0f}" height="{self.f + 10}" rx="{(self.f + 10) / 2}" fill="{SIGNAL}"/>'
        )
        self.text(x + w / 2, y + self.f + 3, label, self.f, 700, "#fff")

    BALL, SOCKET = 15, 19  # Apollon: ball radius 15, socket = ball + 4 px gap
    SIDES = {"left": (-1, 0), "right": (1, 0), "up": (0, -1), "down": (0, 1)}

    def iface(self, x, y, label, lx, ly, anchor="middle", sockets=("left",)):
        """Provided interface (ball) with one required-interface socket per requiring side.

        As in Apollon, each socket is an arc around the ball centre with a 4 px gap. It spans
        165 degrees, or 75 degrees if more than one component requires the interface.
        """
        span = math.radians(165 if len(sockets) == 1 else 75)
        r = self.SOCKET
        for side in sockets:
            dx, dy = self.SIDES[side]
            mid = math.atan2(dy, dx)
            a0, a1 = mid - span / 2, mid + span / 2
            x0, y0 = x + r * math.cos(a0), y + r * math.sin(a0)
            x1, y1 = x + r * math.cos(a1), y + r * math.sin(a1)
            self.add(
                f'<path d="M {x0:.1f} {y0:.1f} A {r} {r} 0 0 1 {x1:.1f} {y1:.1f}" fill="none" '
                f'stroke="{INK}" stroke-width="2"/>'
            )
        self.add(f'<circle cx="{x}" cy="{y}" r="{self.BALL}" fill="#fff" stroke="{INK}" stroke-width="2"/>')
        for i, line in enumerate(label):
            self.text(lx, ly + i * (self.f + 4), line, self.f + 1, 600, INK, anchor)

    def wire(self, d, dashed=False, arrow=False, color=INK, width=2):
        dash = ' stroke-dasharray="8 6"' if dashed else ""
        mark = ' marker-end="url(#open)"' if arrow else ""
        self.add(f'<path d="{d}" fill="none" stroke="{color}" stroke-width="{width}"{dash}{mark}/>')

    def node(self, x, y, w, h, st, name, depth=16, fill="#fff", stroke=INK):
        """UML deployment node (3D box)."""
        d = depth
        self.add(
            f'<path d="M {x} {y} L {x + d} {y - d} L {x + w + d} {y - d} L {x + w + d} {y + h - d} L {x + w} {y + h} '
            f'L {x + w} {y} Z" fill="{TINT}" stroke="{stroke}" stroke-width="2" stroke-linejoin="round"/>'
        )
        self.add(f'<path d="M {x + w} {y} L {x + w + d} {y - d}" fill="none" stroke="{stroke}" stroke-width="2"/>')
        self.add(f'<rect x="{x}" y="{y}" width="{w}" height="{h}" fill="{fill}" stroke="{stroke}" stroke-width="2"/>')
        self.text(x + w / 2, y + 10 + self.f, st, self.f, 400, INK2)
        self.text(x + w / 2, y + 16 + self.f + self.f + 6, name, self.f + 6, 800, stroke if stroke != INK else INK)

    def klass(self, x, y, w, name, attrs, fill_head=SIGNAL):
        lh = self.f + 10
        head = self.f + 22
        h = head + lh * len(attrs) + 14
        self.add(f'<rect x="{x}" y="{y}" width="{w}" height="{h}" fill="#fff" stroke="{fill_head}" stroke-width="2"/>')
        self.add(f'<rect x="{x}" y="{y}" width="{w}" height="{head}" fill="{fill_head}"/>')
        self.text(x + w / 2, y + head / 2 + (self.f + 3) / 2 - 2, name, self.f + 3, 700, "#fff")
        for i, a in enumerate(attrs):
            self.text(x + 16, y + head + 8 + lh * i + self.f, a, self.f, 400, INK, "start")
        return h

    def action(self, x, y, w, h, title, sub=None, fill="#fff", stroke=INK):
        self.add(
            f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="18" fill="{fill}" stroke="{stroke}" stroke-width="2"/>'
        )
        if sub:
            self.text(x + w / 2, y + h / 2 - 3, title, self.f + 2, 700)
            self.text(x + w / 2, y + h / 2 + self.f + 1, sub, self.f, 400, MUTED)
        else:
            self.text(x + w / 2, y + h / 2 + (self.f + 2) / 2 - 2, title, self.f + 2, 700)

    def save(self, name):
        pw, ph = self.pad
        W, H = self.w + 2 * pw, self.h + 2 * ph
        head = [
            f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {W} {H}" width="{W}" height="{H}" '
            f'font-family="{FONT}" role="img" aria-label="{self.label}">',
            f"<title>{self.title}</title>",
            f'<rect width="{W}" height="{H}" fill="#fff"/>',
            '<defs><marker id="open" viewBox="0 0 12 12" refX="11" refY="6" markerUnits="userSpaceOnUse" '
            f'markerWidth="15" markerHeight="15" orient="auto"><path d="M 1 1 L 11 6 L 1 11" fill="none" '
            f'stroke="{INK}" stroke-width="1.8"/></marker>'
            '<marker id="filled" viewBox="0 0 12 12" refX="11" refY="6" markerUnits="userSpaceOnUse" '
            f'markerWidth="14" markerHeight="14" orient="auto"><path d="M 1 1 L 11 6 L 1 11 Z" fill="{INK}"/>'
            "</marker></defs>",
            f'<g transform="translate({pw} {ph})">',
        ]
        with open(os.path.join(HERE if name != "architecture.svg" else IMG, name), "w") as fh:
            fh.write("\n".join(head + self.out + ["</g>", "</svg>"]) + "\n")


# --- 1. Top-level design: UML component diagram ------------------------------------
def top_level():
    s = Svg(1530, 850, "Logos top-level design (UML component diagram)", "UML component diagram of Logos")
    f = s.f
    B, S = Svg.BALL, Svg.SOCKET  # a provided line ends on the ball, a required line on the socket

    def stack(x, y, w, h):
        """Two offset copies behind a component: the component runs as several instances."""
        for o in (20, 10):
            s.add(
                f'<rect x="{x + o}" y="{y - o}" width="{w}" height="{h}" fill="#fff" stroke="{INK}" stroke-width="2"/>'
            )

    s.frame(360, 345, 1170, 505, "«subsystem»", "Logos", name_x=510)
    # Request path: clients -> Gateway -> Webservice -> cloud provider or Orchestrator -> workers
    s.wire(f"M 200 490 H {275 - S} M {275 + B} 490 H 410")  # Logos UI -> Management API
    s.wire(f"M 200 660 H {275 - S} M {275 + B} 660 H 410")  # Application -> OpenAI API
    s.wire(f"M 610 525 H {665 - S} M {665 + B} 525 H 720")  # Gateway -> HTTP
    s.wire(f"M 855 410 V {300 + S} M 855 {300 - B} V 215")  # webservice -> cloud provider API
    s.wire(f"M 1010 525 H {1140 - S} M {1140 + B} 525 H 1270")  # webservice -> local inference API
    s.wire(f"M 1365 285 V {316 - S} M 1365 {316 + B} V 430")  # worker -> worker session
    # Supporting connections
    s.wire(f"M 115 200 V {265 - B}")  # Keycloak provides OpenID Connect
    s.wire(f"M 115 440 V {265 + S}")  # Logos UI requires it
    s.wire(f"M 760 410 V 265 H {115 + S}")  # Webservice requires it
    s.wire(f"M 760 620 V {690 - S} M 760 {690 + B} V 730")  # Webservice -> rate limits, pub/sub -> Shared State
    s.wire(f"M 950 620 V 690 H {1125 - S}")  # webservice requires SQL
    s.wire(f"M 1395 620 V 690 H {1125 + S}")  # orchestrator requires SQL
    s.wire(f"M 1125 {690 + B} V 730")  # Database provides SQL
    # External systems
    s.comp(0, 40, 230, 160, "«subsystem»", "Identity Provider", "Keycloak")
    s.comp(685, 40, 340, 175, "«subsystem»", "Cloud AI Provider", "Azure · OpenAI")
    stack(1220, 20, 290, 265)
    s.comp(1220, 20, 290, 265, "«subsystem»", "GPU Node", top=True)
    s.badge(1232, 32, "× N")
    s.comp(1250, 100, 230, 75, None, "Worker Node", "FastAPI", name_size=f + 2, icon=False)
    s.comp(1250, 195, 230, 75, None, "Lane", "vLLM", name_size=f + 2, icon=False)
    s.wire("M 1365 175 V 193", dashed=True, arrow=True)
    s.comp(0, 440, 200, 100, "«subsystem»", "Logos UI", "Angular")
    s.comp(0, 610, 200, 100, "«subsystem»", "Application", "OpenAI SDK")
    # Logos
    s.comp(410, 440, 200, 270, "«component»", "Gateway", "nginx · Traefik")
    stack(720, 430, 270, 190)
    s.comp(720, 430, 270, 190, "«component»", "Webservice", "Spring Boot")
    s.badge(734, 442, "× N")
    s.comp(1270, 430, 250, 190, "«component»", "Orchestrator", "FastAPI", name_size=f + 3)
    s.comp(645, 730, 230, 90, "«component»", "Shared State", "Redis")
    s.comp(1010, 730, 230, 90, "«component»", "Database", "PostgreSQL")
    # Interfaces: ball = provided, socket = required
    s.iface(275, 490, ["Management", "API"], 282, 434)
    s.iface(275, 660, ["OpenAI", "API"], 282, 604)
    s.iface(115, 265, ["OpenID Connect"], 145, 310, anchor="start", sockets=("down", "right"))
    s.iface(855, 300, ["Cloud provider API"], 882, 307, anchor="start", sockets=("down",))
    s.iface(665, 525, ["HTTP"], 665, 575)
    s.iface(1140, 525, ["Local inference API"], 1140, 492)
    s.iface(760, 690, ["Rate limits", "Pub/sub"], 787, 686, anchor="start", sockets=("up",))
    s.iface(1125, 690, ["SQL"], 1125, 664, sockets=("left", "right"))
    s.iface(1365, 316, ["Worker session", "(WebSocket)"], 1340, 314, anchor="end", sockets=("up",))
    s.save("top-level.svg")


# --- 2. Deployment: UML deployment diagram -------------------------------------------
def deployment():
    s = Svg(1480, 700, "Logos deployment (UML deployment diagram)", "UML deployment diagram of Logos")
    f = s.f
    # User device
    s.node(0, 150, 270, 300, "«device»", "User device")
    s.comp(25, 250, 220, 80, "«artifact»", "Browser", icon=False)
    s.comp(25, 350, 220, 80, "«artifact»", "Application", icon=False)
    # Core node with its containers
    s.node(420, 40, 620, 640, "«device»", "Core node", stroke=SIGNAL)
    cols, rows = (445, 745), (125, 220, 315, 410, 505, 590)
    boxes = [
        ("logos-rate-gateway", None, False),
        ("traefik", None, False),
        ("logos-webservice", "× N replicas", False),
        ("logos-orchestrator", "× 1", False),
        ("logos-redis", None, False),
        ("logos-db", "PostgreSQL 17", False),
        ("logos-ui", None, False),
        ("keycloak", "development only", False),
        ("logos-agent", "optional profile", True),
        ("logos-agent-gateway", "optional profile", True),
    ]
    for i, (name, sub, dashed) in enumerate(boxes):
        x, y = cols[i % 2], rows[i // 2]
        s.comp(x, y, 270, 76, None, name, sub or (), dashed=dashed, name_size=f + 2, icon=False)
    # GPU worker node
    s.node(1190, 40, 290, 330, "«device»", "GPU worker node")
    s.text(1335, 400, "0 … N per core node", f, 400, MUTED)
    s.comp(1212, 125, 246, 76, None, "logos-workernode", "vLLM lanes", name_size=f + 2, icon=False)
    s.comp(1212, 230, 246, 76, None, "logos-node-manager", "optional profile", dashed=True, name_size=f + 2, icon=False)
    # External systems
    s.node(1190, 470, 290, 90, "«external system»", "Cloud AI provider")
    s.node(1190, 600, 290, 80, "«external system»", "Organisation IdP")
    # Communication paths
    s.wire("M 270 300 H 420")
    s.text(345, 286, "HTTPS 443", f + 1, 600)
    s.wire("M 1190 200 H 1040")
    s.text(1115, 166, "WebSocket", f + 1, 600)
    s.text(1115, 188, "outbound", f, 400, MUTED)
    s.wire("M 1040 515 H 1190")
    s.text(1115, 503, "HTTPS", f + 1, 600)
    s.wire("M 1040 640 H 1190")
    s.text(1115, 628, "OIDC", f + 1, 600)
    s.save("deployment.svg")


# --- 3. Data model: UML class diagram -----------------------------------------------
def data_model():
    s = Svg(
        1520,
        830,
        "Logos data model (UML class diagram, simplified)",
        "Simplified class diagram of the Logos data model",
    )
    f = s.f
    W = 290
    cx = (0, 410, 820, 1230)
    s.klass(cx[0], 0, W, "User", ["username", "role", "OIDC subject"])
    s.klass(cx[1], 0, W, "Team", ["name", "budget", "default limits"])
    s.klass(cx[2], 0, W, "ApiKey", ["key value (secret)", "key type", "rate and budget limits", "priority"])
    s.klass(cx[3], 0, W, "Policy", ["privacy, priority", "topic", "team or API key scope"], fill_head=MUTED)
    s.klass(cx[0], 300, W, "Model", ["name, aliases", "capabilities", "routing weights", "token prices"])
    s.klass(
        cx[1],
        300,
        W,
        "Provider",
        ["type: cloud | logosnode", "base URL, privacy", "runtime snapshot", "model profiles (VRAM)"],
    )
    s.klass(
        cx[2],
        300,
        W,
        "LogEntry",
        ["timestamps, status", "API key, team, user", "model, provider", "usage tokens", "settled cost"],
    )
    s.klass(
        cx[2], 600, W, "BatchObject", ["status, progress", "execution: logos | cloud", "API key, team"], fill_head=MUTED
    )
    s.klass(
        cx[0],
        600,
        700,
        "Permissions",
        ["team_model_permissions", "api_key_model_permissions", "model_provider (catalogue)"],
        fill_head=MUTED,
    )
    mid = f + 22 + 8 + (f + 10)  # y of the first attribute line centre
    # associations
    s.wire(f"M {cx[0] + W} {mid} H {cx[1]}", arrow=True)
    s.text((cx[0] + W + cx[1]) / 2, mid - 12, "member of", f, 400, MUTED)
    s.wire(f"M {cx[1] + W} {mid} H {cx[2]}", arrow=True)
    s.text((cx[1] + W + cx[2]) / 2, mid - 12, "owns", f, 400, MUTED)
    s.wire(f"M {cx[3]} {mid} H {cx[2] + W}", dashed=True, arrow=True)
    s.text((cx[2] + W + cx[3]) / 2, mid - 12, "applies to", f, 400, MUTED)
    ak_bottom = f + 22 + (f + 10) * 4 + 14
    s.wire(f"M {cx[2] + W / 2} {ak_bottom} V 300", arrow=True)
    s.text(cx[2] + W / 2 + 14, (ak_bottom + 300) / 2 + 6, "logged in", f, 400, MUTED, "start")
    s.wire(f"M {cx[0] + W} {300 + mid} H {cx[1]}", arrow=True)
    s.text((cx[0] + W + cx[1]) / 2, 300 + mid - 12, "served by", f, 400, MUTED)
    s.wire(f"M {cx[1] + W} {300 + mid} H {cx[2]}", arrow=True)
    s.text((cx[1] + W + cx[2]) / 2, 300 + mid - 12, "used in", f, 400, MUTED)
    le_bottom = 300 + f + 22 + (f + 10) * 5 + 14
    s.wire(f"M {cx[2] + W / 2} 600 V {le_bottom}", dashed=True, arrow=True)
    s.text(cx[2] + W / 2 + 14, (le_bottom + 600) / 2 + 6, "creates", f, 400, MUTED, "start")
    s.text(
        0,
        795,
        "Simplified. The schema also has agent sessions, calibration probes, hourly statistics and prices.",
        f,
        400,
        MUTED,
        "start",
    )
    s.text(0, 825, "Source of truth: the Liquibase changelogs of logos-webservice.", f, 400, MUTED, "start")
    s.save("data-model.svg")


# --- 4. Inference request flow: UML activity diagram --------------------------------
def request_flow():
    s = Svg(
        1400, 900, "Logos inference request flow (UML activity diagram)", "Activity diagram of an inference request"
    )
    f = s.f
    y0, h = 60, 90
    xs = (0, 330, 660, 990)
    s.add(f'<circle cx="16" cy="{y0 + h / 2}" r="12" fill="{INK}"/>')
    s.wire(f"M 28 {y0 + h / 2} H 60", arrow=True)
    s.action(60, y0, 240, h, "Client", "sends request + API key")
    s.action(xs[1] + 30, y0, 280, h, "logos-rate-gateway", "per-IP rate limit")
    s.action(xs[2] + 30, y0, 280, h, "traefik", "routes by path, balances")
    s.action(xs[3] + 30, y0, 380, h, "logos-webservice", "authenticates, checks budget", stroke=SIGNAL)
    for a, b in ((300, xs[1] + 30), (xs[1] + 310, xs[2] + 30), (xs[2] + 310, xs[3] + 30)):
        s.wire(f"M {a} {y0 + h / 2} H {b}", arrow=True)
    dx, dy = 1210, 310  # decision diamond centre
    s.wire(f"M {dx} {y0 + h} V {dy - 60}", arrow=True)
    s.add(
        f'<path d="M {dx} {dy - 60} L {dx + 110} {dy} L {dx} {dy + 60} L {dx - 110} {dy} Z" '
        f'fill="#fff" stroke="{SIGNAL}" stroke-width="2.5"/>'
    )
    s.text(dx, dy - 4, "cloud-only", f + 1, 700)
    s.text(dx, dy + f + 2, "model?", f + 1, 700)
    # Path A: cloud only
    ax = 300
    s.wire(f"M {dx - 110} {dy} H {ax} V 440", arrow=True)
    s.text(ax + 14, 400, "[yes]", f + 1, 700, SIGNAL, "start")
    s.action(ax - 170, 440, 340, h, "logos-webservice", "forwards directly")
    s.wire(f"M {ax} {440 + h} V 590", arrow=True)
    s.action(ax - 170, 590, 340, h, "Cloud AI provider", "Azure, OpenAI")
    # Path B: local or mixed
    s.wire(f"M {dx} {dy + 60} V 440", arrow=True)
    s.text(dx + 14, 410, "[no: local or mixed]", f + 1, 700, SIGNAL, "start")
    s.action(dx - 170, 440, 340, h, "logos-orchestrator", "classifies, schedules")
    s.wire(f"M {dx} {440 + h} V 590", arrow=True)
    s.action(dx - 170, 590, 340, h, "GPU worker node", "vLLM lane")
    # Merge and settlement
    my = 770
    s.wire(f"M {ax} {590 + h} V {my} H 540", arrow=True)
    s.wire(f"M {dx} {590 + h} V {my} H 860", arrow=True)
    s.action(540, my - h / 2, 320, h, "logos-db", "logs and settles usage")
    s.wire(f"M 700 {my + h / 2} V {my + h / 2 + 40}", arrow=True)
    s.add(f'<circle cx="700" cy="{my + h / 2 + 58}" r="16" fill="#fff" stroke="{INK}" stroke-width="2"/>')
    s.add(f'<circle cx="700" cy="{my + h / 2 + 58}" r="9" fill="{INK}"/>')
    s.save("request-flow.svg")


# --- 5. Request routing and replication (architecture page) --------------------------
def routing():
    s = Svg(
        1450, 680, "Logos request routing and replication", "Request routing to replicated logos-webservice instances"
    )
    f = s.f
    s.comp(0, 170, 210, 100, "«subsystem»", "Application", "SDK · tool · agent")
    s.comp(0, 320, 210, 100, "«subsystem»", "Logos UI", "browser")
    s.comp(310, 140, 230, 310, "«component»", "Gateway", ["logos-rate-gateway", "traefik"])
    s.wire("M 210 220 H 310", arrow=True)
    s.wire("M 210 370 H 310", arrow=True)
    reps = [(70, "1"), (240, "2"), (410, "N")]
    s.wire("M 540 295 H 600 M 600 115 V 455")
    for y, n in reps:
        s.wire(f"M 600 {y + 45} H 660", arrow=True)
        s.comp(660, y, 290, 90, None, f"logos-webservice {n}", name_size=f + 2)
    s.text(805, 223, "⋮", f + 8, 700, MUTED)
    s.text(805, 393, "⋮", f + 8, 700, MUTED)
    s.text(620, 40, "load-balanced replicas", f + 1, 700, SIGNAL, "start")
    # Shared state of the replicas
    s.wire("M 805 500 V 560")
    s.comp(690, 560, 230, 80, None, "logos-redis", "shared rate limits", name_size=f + 2)
    # Right-hand bus
    s.wire("M 950 115 H 1010 M 950 285 H 1010 M 950 455 H 1010 M 1010 115 V 455")
    s.wire("M 1010 115 V 85 H 1160", arrow=True)
    s.text(1018, 70, "cloud, direct", f + 1, 600, INK, "start")
    s.comp(1160, 30, 290, 110, "«external system»", "Cloud AI provider", icon=False)
    s.wire("M 1010 285 H 1160", arrow=True)
    s.text(1030, 272, "local, mixed", f + 1, 600, INK, "start")
    s.comp(1160, 240, 290, 90, None, "logos-orchestrator", "× 1", name_size=f + 2)
    s.comp(1160, 470, 290, 100, "«subsystem»", "GPU worker nodes", "0 … N")
    s.wire("M 1305 470 V 330", arrow=True)
    s.text(1320, 410, "WebSocket", f + 1, 600, INK, "start")
    s.text(1320, 432, "outbound", f, 400, MUTED, "start")
    s.save("architecture.svg")


if __name__ == "__main__":
    top_level()
    deployment()
    data_model()
    request_flow()
    routing()
