"""Generates top-level.svg, the UML component diagram of Logos.

The style follows the architecture slide of the Logos doctoral seminar deck:
UML components and subsystems, ball-and-socket interfaces, the Logos brand colours.
Component names are the container names in docker-compose.yaml and
logos-workernode/docker-compose.yml.

Usage (from the repository root):
    python3 docs/static/img/system-design/top-level.py docs/static/img/system-design/top-level.svg
"""

import sys

INK, INK2, MUTED, SIGNAL, TINT = "#241c3b", "#3b3160", "#5f5688", "#7c3aed", "#ede9fe"
FONT = "Inter, -apple-system, BlinkMacSystemFont, 'Segoe UI', Helvetica, Arial, sans-serif"
W, H = 1520, 870
PAD_X, PAD_Y = 40, 24
out = []


def add(s):
    out.append(s)


def icon(x, y):
    add(
        f'<g transform="translate({x} {y})" fill="#fff" stroke="{INK}" stroke-width="1.6">'
        '<rect x="4" y="0" width="16" height="20"/><rect x="0" y="4" width="8" height="4"/>'
        '<rect x="0" y="12" width="8" height="4"/></g>'
    )


def text(x, y, s, size=16, weight=400, fill=INK, anchor="middle"):
    add(
        f'<text x="{x}" y="{y}" text-anchor="{anchor}" font-size="{size}" '
        f'font-weight="{weight}" fill="{fill}">{s}</text>'
    )


def comp(x, y, w, h, st, name, sub=None, top=False, fill="#fff", size=21):
    subs = [sub] if isinstance(sub, str) else (sub or [])
    add(f'<rect x="{x}" y="{y}" width="{w}" height="{h}" fill="{fill}" stroke="{INK}" stroke-width="2"/>')
    icon(x + w - 32, y + 11)
    cy = y + 32 if top else y + h / 2 - 6 - 11 * len(subs)
    text(x + w / 2, cy, st, 15, 400, INK2)
    text(x + w / 2, cy + 26, name, size, 700)
    for i, line in enumerate(subs):
        text(x + w / 2, cy + 50 + i * 20, line, 15, 400, MUTED)


def badge(x, y, label):
    add(f'<rect x="{x}" y="{y}" width="58" height="26" rx="13" fill="{SIGNAL}"/>')
    text(x + 29, y + 18, label, 15, 700, "#fff")


def iface(x, y, label, lx, ly, anchor="middle", socket="left"):
    """Provided interface (ball) with the required-interface socket around it."""
    r = 20
    arcs = {
        "left": f"M {x - 4} {y - r} A {r} {r} 0 0 0 {x - 4} {y + r}",
        "right": f"M {x + 4} {y - r} A {r} {r} 0 0 1 {x + 4} {y + r}",
        "up": f"M {x - r} {y - 4} A {r} {r} 0 0 1 {x + r} {y - 4}",
        "down": f"M {x - r} {y + 4} A {r} {r} 0 0 0 {x + r} {y + 4}",
    }
    add(f'<circle cx="{x}" cy="{y}" r="13" fill="#fff" stroke="{INK}" stroke-width="2"/>')
    add(f'<path d="{arcs[socket]}" fill="none" stroke="{INK}" stroke-width="2"/>')
    for i, line in enumerate(label):
        text(lx, ly + i * 22, line, 17, 600, INK, anchor)


add(
    f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {W} {H}" width="{W}" height="{H}" '
    f'font-family="{FONT}" role="img" aria-label="UML component diagram of Logos">'
)
add("<title>Logos top-level design (UML component diagram)</title>")
add(f'<rect width="{W}" height="{H}" fill="#fff"/>')
add(f'<g transform="translate({PAD_X} {PAD_Y})">')
add(
    '<defs><marker id="open" viewBox="0 0 12 12" refX="11" refY="6" markerUnits="userSpaceOnUse" '
    f'markerWidth="15" markerHeight="15" orient="auto"><path d="M 1 1 L 11 6 L 1 11" fill="none" '
    f'stroke="{INK}" stroke-width="1.8"/></marker></defs>'
)

# Logos subsystem frame (behind everything)
FX, FY, FW, FH = 330, 340, 1110, 470
add(
    f'<rect x="{FX}" y="{FY}" width="{FW}" height="{FH}" fill="rgba(237,233,254,0.55)" '
    f'stroke="{SIGNAL}" stroke-width="2.5"/>'
)
icon(FX + FW - 36, FY + 12)
text(1060, FY + 30, "«subsystem»", 16, 400, INK2)
text(1060, FY + 58, "Logos", 25, 800, SIGNAL)

# Wires (drawn before the shapes so that the shapes cover the line ends)
wires = [
    "M 180 470 H 380",  # Logos UI -> Management API -> Gateway
    "M 180 630 H 380",  # Application -> OpenAI-compatible API -> Gateway
    "M 110 200 V 245",  # Keycloak -> OpenID Connect ball
    "M 110 285 V 425",  # OpenID Connect socket -> Logos UI
    "M 125 265 H 700 V 410",  # OpenID Connect socket -> logos-webservice
    "M 780 250 V 287",  # Cloud provider -> Cloud provider API ball
    "M 780 313 V 410",  # Cloud provider API socket -> logos-webservice
    "M 560 515 H 660",  # Gateway -> API routes -> logos-webservice
    "M 880 515 H 1190",  # logos-webservice -> Local request API -> logos-orchestrator
    "M 670 620 V 690",  # logos-webservice -> Rate-limit window -> logos-redis
    "M 870 620 V 730 H 940",  # logos-webservice -> Persistence -> logos-db
    "M 1300 680 V 790 H 892 V 730",  # logos-orchestrator -> Persistence
    "M 1075 260 V 300 H 1320 V 260",  # Worker nodes join
    "M 1300 300 V 395",  # Join -> Worker session ball
    "M 1300 435 V 470",  # Worker session socket -> logos-orchestrator
]
add(f'<g fill="none" stroke="{INK}" stroke-width="2">' + "".join(f'<path d="{d}"/>' for d in wires) + "</g>")

# External systems (top)
comp(0, 40, 220, 160, "«subsystem»", "Keycloak", "identity provider")
comp(620, 40, 320, 210, "«subsystem»", "Cloud AI provider", top=True)
comp(660, 115, 240, 105, "«component»", "Azure · OpenAI", "cloud models")
for x in (955, 1200):
    comp(x, 0, 240, 260, "«subsystem»", "GPU worker node", top=True)
    comp(x + 20, 72, 200, 70, "«component»", "logos-workernode", size=19)
    comp(x + 20, 172, 200, 70, "«component»", "vLLM lane", size=19)
    add(
        f'<path d="M {x + 120} 142 V 170" stroke="{INK}" stroke-width="2" stroke-dasharray="7 6" '
        'marker-end="url(#open)" fill="none"/>'
    )

# Clients (left)
comp(0, 425, 180, 90, "«subsystem»", "Logos UI", "browser")
comp(0, 585, 180, 90, "«subsystem»", "Application", "SDK · tool · agent")

# Logos containers. Every client request enters through the gateway; only
# logos-webservice runs as several replicas behind it.
comp(380, 410, 180, 330, "«component»", "Gateway", ["logos-rate-gateway", "traefik"])
comp(660, 410, 220, 210, "«component»", "logos-webservice", "inference gateway", size=20)
badge(790, 394, "× N")
comp(1190, 470, 220, 210, "«component»", "logos-orchestrator", "classify · schedule", size=20)
comp(580, 690, 180, 80, "«component»", "logos-redis", size=19)
comp(940, 690, 200, 80, "«component»", "logos-db", "PostgreSQL", size=19)

# Interfaces
iface(255, 470, ["Management API"], 255, 438, socket="left")
iface(255, 630, ["OpenAI-compatible", "API"], 245, 576, socket="left")
iface(110, 265, ["OpenID Connect"], 100, 300, anchor="end", socket="down")
iface(780, 300, ["Cloud provider API"], 806, 306, anchor="start", socket="down")
iface(610, 515, ["API", "routes"], 610, 560, socket="left")
iface(1035, 515, ["Local request API"], 1035, 483, socket="left")
iface(670, 655, ["Rate-limit window"], 694, 661, anchor="start", socket="up")
iface(915, 730, ["Persistence"], 860, 703, anchor="end", socket="left")
iface(1300, 415, ["Worker session", "(WebSocket)"], 1276, 410, anchor="end", socket="up")

add("</g>")
add("</svg>")
open(sys.argv[1], "w").write("\n".join(out) + "\n")
