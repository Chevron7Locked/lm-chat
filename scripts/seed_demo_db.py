# SPDX-License-Identifier: Apache-2.0
"""Seed a fresh LMChat SQLite DB with realistic-looking demo content.

Usage:
    rm -f /tmp/lmchat-demo.db
    DATABASE_URL=sqlite+aiosqlite:////tmp/lmchat-demo.db \\
        uv run python scripts/seed_demo_db.py

The output DB is meant to back Playwright screenshot capture for a public
README. Every visible message reads like a real conversation a curious,
technical operator would have with a local model. No placeholders, no
"As an AI language model..." chaff.

Why this matters: empty-DB screenshots make LMChat look like a prototype.
Curated screenshots make it look like what it is — a polished chat client
tightly integrated with LM Studio.
"""
from __future__ import annotations

import asyncio
import os
import random
import secrets
import struct
import sys
from datetime import UTC, date, datetime, timedelta
from hashlib import blake2b, sha256
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Environment bootstrap
# ---------------------------------------------------------------------------
# Generate an ephemeral per-run secret BEFORE importing config — get_settings()
# raises if the secret is missing or empty. Demo data is encrypted at rest
# with this ephemeral key; it does not need to be stable across runs.
os.environ.setdefault("LM_CHAT_SECRET", secrets.token_urlsafe(32))
os.environ.setdefault(
    "DATABASE_URL",
    "sqlite+aiosqlite:////tmp/lmchat-demo.db",
)

# Make `src/` importable when running the script directly (no install).
_REPO_ROOT = Path(__file__).resolve().parent.parent
_SRC = _REPO_ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from sqlalchemy import insert, text  # noqa: E402
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine  # noqa: E402

import alembic.command  # noqa: E402
import alembic.config  # noqa: E402

from lmchat.db.schema import (  # noqa: E402
    chats,
    document_chunks,
    documents,
    memory_insights,
    messages,
    metadata,
    mcp_servers,
    prompts,
    provider_configs,
    quota_usage,
    quotas,
)
from lmchat.services import auth_service  # noqa: E402
from lmchat.services import projects_service  # noqa: E402

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
ADMIN_USERNAME = os.environ.get("SEED_ADMIN_USERNAME", "admin")
ADMIN_PASSWORD = os.environ.get("SEED_ADMIN_PASSWORD", "demo-password")

# When APPLY_TO_LIVE is truthy, the seed targets an EXISTING DB + user
# instead of bootstrapping a fresh /tmp DB — a one-off live-apply path, not
# a general-purpose tool. LIVE_USERNAME is the target username for the
# APPLY_TO_LIVE path; set via SEED_LIVE_USERNAME.
APPLY_TO_LIVE = os.environ.get("SEED_APPLY_TO_LIVE", "").lower() in {"1", "true", "yes"}
LIVE_USERNAME = os.environ.get("SEED_LIVE_USERNAME", "")

# Two distinct model_ids so the multi-model story renders in the sidebar /
# chat headers. These must be LM Studio catalog keys currently loaded on the
# fleet so the chats are continuable and don't fall through to "model
# unknown" placeholder text. Example fleet IDs — swap for model_ids actually
# loaded on your own LM Studio instance.
MODEL_A = "qwen3.6-35b-a3b-mtp"
MODEL_B = "qwen3.5-122b-a10b-mtp"
EMBEDDING_MODEL = "text-embedding-nomic-embed-text-v1.5"

# Embedding vector dimension for placeholder packed bytes. The real
# nomic-embed-text-v1.5 produces 768-dim; we synthesize the same shape so
# the bytes columns look correct on inspection without invoking LM Studio.
_EMBED_DIM = 768

_NOW = datetime.now(tz=UTC)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _packed_embedding(seed: int) -> bytes:
    """Return deterministic, structurally-valid packed float32 bytes.

    Seeded so re-runs produce identical bytes — helpful for diffing DBs
    between seed runs.
    """
    rng = random.Random(seed)
    vec = [rng.uniform(-0.5, 0.5) for _ in range(_EMBED_DIM)]
    return struct.pack(f"<{_EMBED_DIM}f", *vec)


def _text_hash(s: str) -> str:
    """Match memory_service._text_hash exactly: blake2b 32-byte digest."""
    import re

    normalized = re.sub(r"\s+", " ", s).strip().casefold()
    return blake2b(normalized.encode(), digest_size=32).hexdigest()


def _ago(*, days: int = 0, hours: int = 0, minutes: int = 0) -> datetime:
    return _NOW - timedelta(days=days, hours=hours, minutes=minutes)


def _stamp_alembic_head() -> None:
    """Mark the freshly-created DB as already at the latest migration.

    metadata.create_all() builds tables matching db/schema.py but does
    not populate alembic_version. Without a stamp, the BE's startup
    schema check (db/startup.py) sees no alembic_version row, runs
    `alembic upgrade head`, and crashes because the tables already exist.
    Stamping head closes that gap.
    """
    cfg = alembic.config.Config(str(_REPO_ROOT / "alembic.ini"))
    cfg.set_main_option(
        "script_location", str(_REPO_ROOT / "migrations"),
    )
    # Stamp the SAME DB the engine is pointing at. The project's
    # migrations/env.py uses async_engine_from_config so we pass the async
    # URL through verbatim (NOT a sync-stripped one).
    cfg.set_main_option("sqlalchemy.url", os.environ["DATABASE_URL"])
    alembic.command.stamp(cfg, "head")


# ---------------------------------------------------------------------------
# Conversation corpus — every string here ends up on screen.
# ---------------------------------------------------------------------------

STARGATE_SYSTEM_PROMPT = (
    "You're a Stargate companion — SG-1, Atlantis, Universe. When a fan "
    "theory diverges from canon, name it as theory. Cite the episode + "
    "season when relevant. Be specific: which gate generation, which "
    "species, which jumper variant. Operator watches the show with their "
    "kid, so keep it spoiler-light unless asked otherwise."
)

STARGATE_KB_CHUNKS = [
    (
        "Naquadah — the show's universal McGuffin. Powers the gate, "
        "the staff weapon, the death glider, the Tok'ra tunnel crystals. "
        "Structurally reinforces hand-device kinetic strikes. Detectable "
        "in trace amounts in the blood of any Goa'uld host (canon: 'In "
        "the Line of Duty', S2). Refined naquadah ('weapons-grade' per "
        "Sam Carter, S5) is the fissile variant that drives the Mark IX "
        "naquadah-enhanced nuke. Fan-criticism is fair — naquadah does "
        "the work of carbon, silicon, and uranium combined."
    ),
    (
        "Asgard transporter beams: per onscreen technical exposition "
        "(Thor, multiple episodes), the beam works by establishing a "
        "subspace channel anchored to a transporter platform on the "
        "ship, then de-/re-materializing matter via molecular-scale "
        "subspace stress. Range is bounded by hyperspace-anchor "
        "stability, not by line-of-sight. Beams cannot penetrate active "
        "Goa'uld shields or naquadah-veined planetary crust (notable "
        "exception: post-S8 upgraded Asgard tech in the Daniel Jackson)."
    ),
    (
        "Atlantis season 5 wraps the Wraith arc but doesn't close it. "
        "The gene therapy retrovirus (S2-S3 arc) is reframed as "
        "untenable; the alliance with Todd is left unresolved; the city "
        "returns to Earth in 'Enemy at the Gate'. The 2009 cancellation "
        "left Stargate Extinction (the planned DTV continuation) "
        "unmade. Stargate: Atlantis ends mid-arc by design — the writers "
        "expected another vehicle to land the plane."
    ),
]

STARGATE_CHATS: list[dict[str, Any]] = [
    {
        "title": "Asgard transporter beams — physics or pure handwave?",
        "model_id": MODEL_B,
        "created_ago_days": 1,
        # Lowercase preset *id* (not the "Research" display label) — the FE's
        # getPreset() keys on id and renders preset.label as the chip text.
        "active_preset": "research",
        "messages": [
            ("user", "the asgard beaming tech. molecular transport across interstellar distances, real-time, two-way. does the show give it any underlying physics or is it just pure handwave?"),
            (
                "assistant",
                "It's handwave, but it's *consistent* handwave — which is most of the battle in this show.\n\n"
                "The in-universe rules, pieced together across SG-1 S5-S8 and Atlantis:\n\n"
                "- **Subspace channel, not free-space transport.** Thor's exposition in 'Nemesis' (S3 finale) and Carter's later technobabble in 'Covenant' (S8) both anchor on a subspace pathway. The matter isn't crossing normal space — it's de- and re-materializing through a hyperspace-adjacent layer.\n"
                "- **Anchored, not omnidirectional.** A transporter pad on the ship is the anchor. No anchor → no beam. That's why the Goa'uld can't replicate the tech for ground troops — they couldn't crack the anchor problem.\n"
                "- **Shield interaction.** Beams cannot punch through an active Goa'uld shield or the Lantean shield emitters. Naquadah-veined geology partially blocks it (the trick the Tau'ri use to hide the Antarctic outpost).\n\n"
                "What the writers never settle: whether the de-/rematerialization process is destructive (Star Trek transporter problem) or a continuous translation. The Asgard themselves are clones of clones, so the question matters more for them than for anyone else.",
                "Mapping the in-show technical rules so the operator's mental model lines up with what's actually canon vs fan-extrapolation. Avoiding spoilers beyond S5 since the operator is rewatching with their kid.",
            ),
            ("user", "why couldn't they just beam through the goa'uld shields then? same physics layer as the ha'tak's hyperdrive"),
            (
                "assistant",
                "The show's answer is that the Goa'uld shields are explicitly *subspace-disruptive* — they're not just kinetic deflectors, they actively disrupt the subspace-adjacent layer the Asgard beam needs to operate in.\n\n"
                "Carter spells this out twice:\n"
                "- 'Small Victories' (S4) — when the Asgard explain why they need O'Neill's help even though they're more advanced. The replicators were also subspace-active, which is why Asgard beam-weapons couldn't just zap them.\n"
                "- 'Covenant' (S8) — when the Tau'ri pull a goa'uld off Earth and Carter notes the shield interference is mutual: Asgard beams can't get IN, and the Goa'uld can't fire OUT.\n\n"
                "Fan-quibble: if the shield disrupts subspace, the hyperdrive shouldn't work either. The show's hand-wave is that the shield generates a localized disruption in a specific subspace band, and the hyperdrive operates in a different band. Not satisfying but at least it's a rule, not a one-off.",
                None,
            ),
        ],
    },
    {
        "title": "Tollan vs Tok'ra — who played their hand better",
        "model_id": MODEL_B,
        "created_ago_days": 3,
        "messages": [
            ("user", "rewatched 'between two fires' and 'last stand' back to back. who actually played their political hand better — the tollan with isolationism, or the tok'ra with infiltration? both got crushed."),
            (
                "assistant",
                "Tok'ra, by a wide margin — even with the body count.\n\n"
                "**Tollan strategy: isolationism + technology hoarding.**\n"
                "- Refused tech transfer to Earth on grounds of cultural-readiness (S1 'Enigma', S3 'Pretense').\n"
                "- Single homeworld (Tollana), single fallback (Tollan before Tollana), no diaspora.\n"
                "- When Tanith's faction strikes ('Between Two Fires' S5), they have nowhere to scatter to.\n\n"
                "**Tok'ra strategy: infiltration + redundancy.**\n"
                "- Operatives in every System Lord's court for centuries. Cost: massive — many hosts die undercover.\n"
                "- Decentralized: tunnel networks on 50+ worlds, no single point of failure.\n"
                "- Information flow is the actual product, not territory.\n\n"
                "When the Goa'uld grow hostile (Anubis' rise, post-S5), the Tollan are wiped out in one campaign. The Tok'ra take heavy losses ('Last Stand', 'Allegiance', 'Death Knell') but the *network* survives because there is no network — just operatives.\n\n"
                "The deeper read: the Tollan were playing 1990s realpolitik (NPT, non-proliferation, alliance-of-equals). The Tok'ra were playing intelligence-agency long-game (cells, dead-drops, plausible deniability). Against an asymmetric adversary, the Tok'ra model wins.",
                "The operator's framing is good — both lost a lot, so the question is about whose model survived contact. The Tok'ra model is structurally more resilient even though it looks more costly per-engagement.",
            ),
        ],
    },
    {
        "title": "Best filler episode of SG-1 — operator's pick",
        "model_id": MODEL_A,
        "created_ago_days": 0,
        "created_ago_hours": 8,
        "messages": [
            ("user", "what's your pick for SG-1's best 'filler' episode? not the season finales, not the heller goa'uld arcs — a quiet character episode that does the work."),
            (
                "assistant",
                "'Window of Opportunity' (S4E6).\n\n"
                "Surface read: Groundhog Day pastiche. O'Neill and Teal'c repeat the same day until they figure out who started the loop.\n\n"
                "What it actually is: the show's tightest character episode for Jack O'Neill. The script never moralizes about the time loop or makes him solemn about it. Instead he plays golf through the gate, learns ceramics, kisses Carter (no consequences — loop resets), quits the air force on camera so he can do it without paperwork. The comedy is the character work.\n\n"
                "Teal'c is the load-bearing straight-man here. He doesn't have the angst gear other Jaffa actors play — he plays the loop as a soldier waiting for orders, which gives O'Neill the room to *be* the show's actual main character.\n\n"
                "Honorable mentions: '200' (the meta-episode for the 200th), 'Citizen Joe' (the Daniel/Joe-the-barber bottle), 'The Other Guys' (the Felger one — much better than its reputation).",
                None,
            ),
            ("user", "yeah window of opportunity is the answer. citizen joe is underrated though"),
            (
                "assistant",
                "Citizen Joe is structurally interesting because it's the only SG-1 episode where the show admits its own format constraints.\n\n"
                "The conceit — Daniel's gate-travel memories bleeding into a Midwestern barber's dreams — lets the writers cut a clip show that's actually about clip shows. Joe's frustration with his marriage as he becomes obsessed with SG-1 is the show winking at its own audience.\n\n"
                "The Dan Castellaneta casting is what carries it. Anyone less likable in the Joe role and the episode reads as condescending. He plays the schlub with enough dignity that the ending (Daniel showing up at the barbershop) lands as friendship, not pity.\n\n"
                "Bottom 20% of the season's reviews, top 20% of its rewatch value.",
                None,
            ),
        ],
    },
    {
        "title": "Naquadah — the show's least-consistent McGuffin?",
        "model_id": MODEL_A,
        "created_ago_days": 6,
        "messages": [
            (
                "user",
                "naquadah is the worst worldbuilding offender in stargate. it's a power source, structural metal, tracking compound, way to detect goa'uld in someone's blood, AND it's an explosive. am i wrong",
            ),
            (
                "assistant",
                "You're not wrong about the count — naquadah does at least eight things across the show. But you're a little unfair on the worldbuilding grade. Two defenses:\n\n"
                "**1. Real-world materials are also over-loaded.** Carbon is structural (diamond), conductive (graphite), biological (DNA backbone), an industrial reductant (steel-making), a moderator (graphite reactors), AND drives the climate crisis. A sci-fi material doing five jobs is unremarkable if the jobs share an underlying mechanism.\n\n"
                "**2. The show *does* propose a shared mechanism — sort of.** Naquadah's properties are tied to its subspace coupling (Carter, 'A Hundred Days', S3). The same coupling that makes it useful for the gate makes it useful for shields, weapons, sensors. The handwave is consistent: *this is a material that interacts with subspace, and almost all of the show's exotic tech operates in subspace.* It's the same trick Star Trek pulls with dilithium.\n\n"
                "Where you ARE right: the Mark IX 'naquadah-enhanced' nuke (S8 'Reckoning') has zero technical exposition. The show wants a strategic weapon and reaches for naquadah because that's the universal McGuffin lever. That one's lazy.\n\n"
                "But the worldbuilding floor is mostly held. It's not unobtainium-with-extra-steps.",
                "Pushing back on the operator's framing gently. The worldbuilding-criticism crowd online is harsher on naquadah than it deserves — the subspace-coupling unification is real, even if it's quiet.",
            ),
        ],
    },
    {
        "title": "Atlantis S5 wraith arc — resolved or abandoned?",
        "model_id": MODEL_A,
        "created_ago_days": 9,
        "messages": [
            ("user", "atlantis s5. did the wraith arc actually resolve, or did the show just end with it abandoned?"),
            (
                "assistant",
                "Abandoned, but deliberately abandoned — not accidentally. The writers thought another vehicle would land the plane.\n\n"
                "What S5 actually does:\n"
                "- Closes the gene-therapy retrovirus arc (Michael) by killing Michael in 'The Prodigal'.\n"
                "- Leaves the Todd alliance permanently ambiguous. He's neither friend nor enemy at the end — the show explicitly refuses to commit.\n"
                "- Returns Atlantis to Earth in 'Enemy at the Gate' as a defensive maneuver, not a resolution.\n\n"
                "The Wraith as a species are still numerous, still hungry, still a galactic threat. The arc isn't *closed* — it's *paused on a wide shot*.\n\n"
                "Context: the SG-1 / Atlantis production crew expected the planned DTV film **Stargate: Extinction** to wrap the Wraith story (Joe Mallozzi confirmed this in interviews). When MGM's 2010 bankruptcy killed the DTV pipeline, the wrap-up vanished with it. Universe inherited Atlantis' production crew but not its narrative.\n\n"
                "So if you watched s5 and felt the wraith arc was 'unfinished' — that's the correct read. It was meant to finish elsewhere.",
                None,
            ),
        ],
    },
]

TECH_SYSTEM_PROMPT = (
    "You're a home-lab partner — Mac Studio inference, NUCs, Tailscale, "
    "ZFS, terminal tools. Operator is technical (writes Rust, runs the "
    "local model fleet) so skip the hand-holding. Answers should "
    "include actual commands or config snippets when the question is "
    "operational, not just prose. Be honest when something is a known "
    "wart vs an actual misconfiguration."
)

TECH_KB_CHUNKS = [
    (
        "2026-05-18 — Picked up a refurb Mac Studio M2 Max (32GB) at $1450. "
        "Plan: dedicate it to LM Studio as the inference node, leave the "
        "M3 Pro free for dev work. Networking via wired GbE to the office "
        "switch; Tailscale for remote access from the laptop. Power draw "
        "idle ~12W, under load ~40W — quieter than the NUC it's replacing."
    ),
    (
        "2026-05-22 — qwen3.6-35b-a3b-mlx fits in 32GB at 4-bit with ~6GB "
        "headroom for context. ~80 tokens/sec generation, ~250 tokens/sec "
        "prefill. Plenty for the workload. Notable: the MLX runtime "
        "warmup is ~8s the first time a model loads, then ~1s for "
        "subsequent unloads/loads. Plan around that for the panel harness."
    ),
    (
        "2026-06-02 — Storage layout: 2TB internal SSD for OS + models, "
        "external 8TB media drive on USB-C for everything else. Considered "
        "RAID but a single drive with a real backup target is simpler and "
        "I don't need uptime. ZFS on the external; snapshots nightly to "
        "the laptop via syncoid + Tailscale."
    ),
]

TECH_CHATS: list[dict[str, Any]] = [
    {
        "title": "Mac Studio idle power vs the NUC",
        "model_id": MODEL_A,
        "created_ago_days": 2,
        "messages": [
            (
                "user",
                "swapped the NUC out for the mac studio yesterday. wattmeter shows 12W idle vs the NUC at 28W. is that legit or is the meter lying",
            ),
            (
                "assistant",
                "Legit. The M2 Max in idle dynamically downclocks the P-cores and parks the E-cores aggressively — published Mac Studio M2 Max idle is 8-14W depending on background load. 12W with LM Studio loaded but not actively generating is right in band.\n\n"
                "The 28W on the NUC was a typical Intel mobile package at idle. Sounds right for a 12th-gen i7 NUC pulling display + a couple of background daemons.\n\n"
                "Annual cost delta if you want it: (28-12)W × 24h × 365d ÷ 1000 = ~140 kWh/yr saved. At $0.18/kWh that's $25/yr — won't change your life but covers the Tailscale plan you mentioned.\n\n"
                "What's it look like under sustained inference? The published TDP envelope is ~40W for the M2 Max chip under load — your wattmeter should agree if you crank the panel harness on it for 10 minutes.",
                None,
            ),
            (
                "user",
                "yeah ran 5 122b panel seats over 20 min last night, wattmeter peaked at 42W. fans never spun above the default whisper.",
            ),
            (
                "assistant",
                "That's the headline feature for this workload. Five concurrent 35B-class seats at 42W is in laptop-charger territory — one Mac Studio replacing what would be three or four x86 boxes drawing 250W+ each.\n\n"
                "Worth measuring next: ambient temperature delta in the closet/rack. The NUC was dumping 28W idle plus its full TDP under load into the room. The Mac Studio at 12W idle / 42W loaded will warm the closet noticeably less, which feeds back into cooling costs for whatever else lives in there.\n\n"
                "The quiet fans are also doing the voice-notes-from-the-same-room idea you'd mentioned a favor.",
                "Reflecting back the operator's numbers and pulling out the actual headline (perf-per-watt at this scale). Not editorializing — they don't need to be told their setup is good, they already know.",
            ),
        ],
    },
    {
        "title": "ZFS vs btrfs for the media drive",
        "model_id": MODEL_B,
        "created_ago_days": 5,
        "messages": [
            (
                "user",
                "media drive on the home server — ~8TB of mostly read-only stuff. ZFS or btrfs in 2026? not arguing for fun, just want a defensible default.",
            ),
            (
                "assistant",
                "ZFS, for this workload, no contest.\n\n"
                "**Why ZFS wins here:**\n"
                "- **Snapshots that hold up.** Mostly-read-only data + nightly snapshots is ZFS's home turf. `zfs snapshot tank/media@$(date +%F)` is one line and atomic.\n"
                "- **Send/receive over Tailscale** — `syncoid` (sanoid sidecar) just works. Your existing rsync replication becomes incremental block-level streaming for free.\n"
                "- **Mature scrub story.** `zpool scrub` finds and quietly repairs bit rot on read. With 8TB you'll see this matter every couple of years.\n\n"
                "**btrfs caveats specific to 2026:**\n"
                "- RAID5/6 is still marked unstable upstream. For a single-drive media pool that doesn't apply, but the perception around btrfs reliability is downstream of that.\n"
                "- Snapshot deletion is still O(n) on extent count. For mostly-read-only data this is fine; for write-heavy you'll notice.\n\n"
                "Setup sketch on macOS (you're on the Mac Studio, right?):\n\n"
                "```sh\n"
                "# OpenZFSonOSX 2.3.x\n"
                "brew install --cask openzfs\n"
                "sudo zpool create -O compression=lz4 -O atime=off media /dev/disk6\n"
                "sudo zfs create media/library\n"
                "sudo zfs snapshot media/library@$(date +%F)\n"
                "```\n\n"
                "Add sanoid for retention policy and syncoid for replication once the pool is hot.",
                None,
            ),
        ],
    },
    {
        "title": "Tailscale exit node vs full route to home",
        "model_id": MODEL_A,
        "created_ago_days": 4,
        "messages": [
            (
                "user",
                "tailscale exit node from the laptop through the mac studio — is that the same thing as routing the whole subnet, or different",
            ),
            (
                "assistant",
                "Different, and the distinction matters for both perf and security.\n\n"
                "**Exit node:** the Mac Studio NATs *all* your laptop's internet traffic out through its own connection. Your laptop's public IP becomes whatever the home connection's public IP is. Useful for: appearing on a home IP from coffee shops, accessing a service geofenced to your home ISP.\n\n"
                "**Subnet route (`tailscale up --advertise-routes=192.168.1.0/24`):** the Mac Studio advertises that it can reach the home LAN. Your laptop can now ping `192.168.1.100` directly — it doesn't NAT through the Mac Studio for general internet, just for traffic destined to that subnet.\n\n"
                "You almost certainly want the subnet route, not the exit node. Exit-node-ing for general browsing tanks your bandwidth (limited by home upload) and gives you no benefit unless you specifically want the home IP.\n\n"
                "One layered setup that works: subnet route for LAN access, exit node *only when you toggle it* in the menu bar for specific tasks (geofenced sites, troubleshooting home network). Tailscale's UI supports the per-session toggle.",
                "The operator framed it well — these often get confused as the same feature. Subnet routes is what you want by default; exit-node is a niche tool.",
            ),
        ],
    },
]
FAMILY_SYSTEM_PROMPT = (
    "You're a practical thinking partner for household stuff — kid "
    "questions, weekend planning, what's for dinner, books to read with "
    "her. Operator has an 8-year-old who's currently into Stargate. Keep "
    "it concrete and short. Don't moralize, don't suggest 'consulting a "
    "professional' for normal life questions."
)

FAMILY_KB_CHUNKS = [
    (
        "Kid (8) is currently into Stargate (started with SG-1 reruns, "
        "moving to Atlantis when she's ready). Loves the Asgard, Carter "
        "is her favorite character, asks a lot of 'how does the gate "
        "work' questions. Doesn't yet know about the darker Anubis arc."
    ),
    (
        "Household runs on cast-iron + a chest freezer stocked from "
        "Costco runs. Friday-night pizza is non-negotiable. Operator "
        "cooks most weeknights; partner cooks weekends. Picky-eater note: "
        "kid eats almost everything except mushrooms and most cooked "
        "leafy greens."
    ),
]

FAMILY_CHATS: list[dict[str, Any]] = [
    {
        "title": "Kid's birthday — Stargate themed (lol)",
        "model_id": MODEL_A,
        "created_ago_days": 2,
        "messages": [
            (
                "user",
                "the 8yo is OBSESSED with stargate now. birthday in 2 weeks. ideas that aren't cringe and don't require us to rent a soundstage?",
            ),
            (
                "assistant",
                "You can do this with $40 of dollar-store materials and a willingness to commit.\n\n"
                "**The gate.** Hula hoop + tinfoil + cardboard ring, hot-glued. Spray-paint matte gray, dab silver paint on the chevrons. Hang it from a tree or doorway. The kids walk THROUGH it to enter the party — it's the whole bit.\n\n"
                "**Activity 1: dialing.** Make a paper dial with seven chevrons, glyphs printed off the wiki. Each kid gets a 'planet card' with their destination glyphs. They have to dial in order to get their party-favor bag. Twenty minutes, then they're allowed to stop and play.\n\n"
                "**Activity 2: zat'nik'tel tag.** Foam dart guns rebranded as zats. Three shots = vaporized = sit out one round.\n\n"
                "**Cake.** Round chocolate cake, blue gel frosting in the middle for the event horizon. Don't try to pipe chevrons unless you have time — print them on edible paper if you want detail.\n\n"
                "**Skip:** elaborate costumes for the parents (cringe ceiling), trivia (too quiet for 8yos), prop staff weapons (eye-poke hazard with that age group).\n\n"
                "Carter would approve.",
                "Operator wants real ideas, not Pinterest fluff. Hitting the constraints — under-$50, parent-doable, 8-year-old appropriate — and biasing toward physical activity over trivia/lore-heavy stuff that loses kids fast.",
            ),
            (
                "user",
                "ok the hula hoop gate is great. what do we do if it rains and we're stuck inside?",
            ),
            (
                "assistant",
                "Indoor backup plays well:\n\n"
                "**Living room gate:** mount the hoop in a doorway, hang a sheet of blue cellophane across it for the event horizon. Costs $3.\n\n"
                "**Mission briefing:** print a 'Stargate Command' mission folder for each kid — name, planet designation (P3X-numbers, pull from the wiki), objective. Reading the briefing is the bit; making them feel important is the bit.\n\n"
                "**Recon:** scatter 'naquadah crystals' (painted rocks, or just blue marbles) around the house. Each kid has a 'detector' (cardboard PDA + flashlight). First to collect 5 wins.\n\n"
                "**Quiet down:** episode of SG-1 'Window of Opportunity' (S4E6, the time-loop one). Kid-friendly, funny, no scary monsters. Pizza arrives during the golf-through-the-gate scene.\n\n"
                "If you're scrappy this can run two hours with leftover energy for cake. Don't try to fill three.",
                None,
            ),
        ],
    },
    {
        "title": "Pacific Northwest road trip — kid-friendly stops",
        "model_id": MODEL_A,
        "created_ago_days": 7,
        "messages": [
            (
                "user",
                "driving portland → seattle → vancouver with the kid in late july, 10 days. what's worth a stop that ISN'T on every list (skip pike place, skip multnomah falls)?",
            ),
            (
                "assistant",
                "Going off-script. These hold up with an 8-year-old:\n\n"
                "**Portland → Seattle leg:**\n"
                "- **Mount St. Helens, Johnston Ridge Observatory.** Detour off I-5 at Castle Rock. Drive up the volcano. The view of the crater + the blast-zone exhibits land harder than any 'famous' Portland or Seattle thing. Plan a full half-day; the road in is slow.\n"
                "- **Hood Canal at Hoodsport.** Stop for an hour. Tide-pool at low tide if you can time it. Easier-than-Olympic-coast version of the wonder.\n\n"
                "**Seattle leg:**\n"
                "- **Museum of Flight (Boeing Field), NOT the Pacific Science Center.** MOF has actual Concorde / Constellation / Air Force One walkthroughs. Pacific Science Center is fine but it's a 'kids' museum'; MOF is where a curious 8yo gets her brain rewired.\n"
                "- **Discovery Park, west loop.** Free, almost no tourists, a working lighthouse, beach access. Pack lunch.\n\n"
                "**Seattle → Vancouver leg:**\n"
                "- **Birch Bay (border crossing prep).** Roadside-attraction nostalgia, cheap arcade, a chance to stretch before the border line. Skip if you're crossing weekday morning.\n"
                "- **Stanley Park's Vancouver Aquarium** is the obvious kid pull in Vancouver. The non-obvious one: **Lynn Canyon suspension bridge + park.** Free (Capilano isn't), shorter, kid-scale, fewer crowds.\n\n"
                "What I'd cut: any tasting-menu thing, Pike Place (you said skip ✓), Pioneer Square (tired for kids).",
                "The framing 'not on every list' is the load-bearing constraint. Replacing the touristy version of each city with the local one. Mount St. Helens is the unexpected pick that'll be a memory for her.",
            ),
        ],
    },
]

BOOKS_SYSTEM_PROMPT = (
    "You're a book-recommendation companion who knows the operator's "
    "taste: military sci-fi, character-driven space opera, the cozy "
    "side of Becky Chambers, hard non-fiction on cryptography / "
    "infrastructure. They've read most of the obvious stuff — go "
    "deeper than 'have you tried The Expanse'. When you recommend, say "
    "*why* it'll land for them specifically."
)

BOOKS_KB_CHUNKS = [
    (
        "Operator's confirmed loves: Becky Chambers (Wayfarers series), "
        "Martha Wells Murderbot Diaries, John Scalzi Old Man's War, "
        "James S.A. Corey Expanse (all 9), Adrian Tchaikovsky Children "
        "of Time. They love when a sci-fi setting commits — wants the "
        "Stargate-shaped 'team-of-specialists encounters wonder' beat. "
        "Will DNF anything where the prose is gray-paste."
    ),
    (
        "Operator's confirmed non-fiction taste: Bruce Schneier, "
        "anything Wired Magazine-adjacent in the cypherpunk lineage, "
        "Tracy Kidder Soul of a New Machine, Lewis Mumford. Doesn't "
        "love business-book pop social science (Gladwell-shaped)."
    ),
]

BOOKS_CHATS: list[dict[str, Any]] = [
    {
        "title": "Books that scratch the Stargate itch",
        "model_id": MODEL_B,
        "created_ago_days": 1,
        "messages": [
            (
                "user",
                "books that actually feel like stargate. not just 'military sci-fi' — that specific 'team of specialists finds wonder' beat. what am I missing",
            ),
            (
                "assistant",
                "Five that hit different facets of the Stargate beat. Listed in priority order for your specific taste.\n\n"
                "**1. *A Memory Called Empire* — Arkady Martine.** A first-contact-shaped diplomatic thriller. The protagonist is the ambassador from a small space-mining station to a vast, opaque empire. The wonder is *cultural* — language, memory, what an empire does to small cultures — rather than tech. The 'team' is small (her + the empire's liaison) but the dynamic IS Stargate. Won the Hugo for a reason.\n\n"
                "**2. *Children of Time* — Adrian Tchaikovsky.** You said you read this — the *sequel* (Children of Ruin) is what I'm recommending. The wonder beat scales up. The team is larger. The encounter is stranger. If you didn't bounce off Time, Ruin is the one you don't yet know you want.\n\n"
                "**3. *Ancillary Justice* — Ann Leckie.** Smaller team (one protagonist, splintered across bodies), but the slow reveal of how the Radch empire works is *exactly* the slow reveal of Goa'uld politics across SG-1 seasons 1-4. The prose is denser than Stargate; commit to the first 80 pages.\n\n"
                "**4. *The Long Way to a Small, Angry Planet* — Becky Chambers.** You've read Chambers. This one *is* a team-of-specialists on a slow journey through wonder. Lower stakes than Stargate (no Goa'uld, no Wraith) but the team chemistry is the bullseye.\n\n"
                "**5. *Project Hail Mary* — Andy Weir.** Solo + AI specialist + alien specialist makes a team of three. The Stargate beat is the alien-encounter chapter — possibly the best first-contact scene written in the last decade. Tone is closer to Weir's chatty Martian voice than to Stargate's military flavor; not for everyone.\n\n"
                "Honorable mention: *Anathem* (Neal Stephenson) — wrong shape but the *wonder* density is unmatched.",
                "The operator's specific taste rules out the easy answers (no Expanse rec — they've read it). Going for the 'wonder' beat over the 'mil sci-fi' beat because the framing was 'specific' to Stargate. Memory Called Empire is the top pick because it does the cultural-encounter wonder thing without being a war book.",
            ),
        ],
    },
    {
        "title": "Murderbot Diaries — worth starting?",
        "model_id": MODEL_A,
        "created_ago_days": 4,
        "messages": [
            (
                "user",
                "murderbot diaries. people i trust keep recommending. worth starting? i'm picky about first-person narrators",
            ),
            (
                "assistant",
                "Yes — and for your stated picky-ness, *especially* yes.\n\n"
                "Murderbot's first-person voice is the whole bit. It's a self-aware corporate security construct that downloads soap operas to avoid having to think about its own personhood. The voice is wry, self-deprecating, observational — closer to Catherynne Valente or Becky Chambers than to standard sci-fi.\n\n"
                "Start with **All Systems Red** (novella, ~150 pages). If the voice doesn't catch you in the first 30 pages, it never will and you're free to bounce.\n\n"
                "What it'll do for you specifically:\n"
                "- Same 'competent specialist on a survey team' beat as Stargate.\n"
                "- Murderbot's attempts to suppress its growing affection for the humans it's protecting is the engine. If you liked Carter-Teal'c dynamics, you'll like this.\n"
                "- Short, episodic books that read in 2-3 sittings each.\n\n"
                "Hidden expectation-setter: the first four are novellas, then book 5 (Network Effect) is a full novel. The novellas are stronger as introductions; the novel is stronger as a payoff. Don't start with the novel.",
                None,
            ),
        ],
    },
    {
        "title": "Non-fiction: recent crypto books for a non-expert",
        "model_id": MODEL_A,
        "created_ago_days": 11,
        "messages": [
            (
                "user",
                "want a crypto/security book for the kindle that's not 'how to use signal' tier and not a research monograph. recent-ish. what's good",
            ),
            (
                "assistant",
                "Three at different depths.\n\n"
                "**1. *Tracers in the Dark* — Andy Greenberg (2022).** True-crime crypto. How the FBI cracked Silk Road, AlphaBay, the Welcome to Video case. Reads like a thriller. Light on the math, heavy on the operational and human story. Closest match to your Wired-Schneier taste.\n\n"
                "**2. *Crypto Wars* — Craig Jarvis (2020).** Policy history of the export-controls fight from the 90s through Snowden. Drier than Greenberg but the load-bearing text on how the political and legal landscape we live in got built. If you liked Steven Levy's *Crypto* you'll like this — same beat, three decades later.\n\n"
                "**3. *Real-World Cryptography* — David Wong (2021).** Actually-explains-the-math without becoming a graduate textbook. Closer to a craft manual than a popular-science book. Read it if you want to engage with the primitives (Curve25519, Argon2, the post-quantum candidates) rather than just understand the politics.\n\n"
                "Skip: anything titled '*\\* Wars*' written by a non-journalist (vibes are bad). Anything that name-drops 'cryptocurrency' in the subtitle without naming a primitive (signal-to-noise will fail you).\n\n"
                "If you only want one, **Tracers in the Dark** is the safe pick.",
                None,
            ),
        ],
    },
]
DETACHED_CHATS: list[dict[str, Any]] = [
    {
        "title": "quick — async vs threads for a 10-req/s scraper",
        "model_id": MODEL_A,
        "created_ago_days": 0,
        "created_ago_hours": 14,
        "slash": "/research",
        "messages": [
            (
                "user",
                "/research async vs threadpool for a scraper that does ~10 reqs/sec, all IO-bound, python. which actually scales better in 2026?",
            ),
            (
                "assistant",
                "For 10 req/s of pure I/O work, both approaches will hit your target with room to spare. The pick comes down to ecosystem rather than raw throughput.\n\n"
                "**Async (httpx / aiohttp):**\n"
                "- Lower memory: one event loop, one OS thread, ~1KB per in-flight task.\n"
                "- Better for >100 concurrent in-flight requests.\n"
                "- Cost: every library in your stack must be async-aware. Mixing in a sync DB driver kills the gains.\n\n"
                "**Threads (requests + ThreadPoolExecutor):**\n"
                "- Higher memory: each thread is ~8MB of stack.\n"
                "- Limit is OS thread count (~1000s on Linux, practically ~50-100 before scheduler overhead).\n"
                "- Cost: the GIL pauses are negligible because your work is all I/O, but you do pay context-switch tax.\n\n"
                "At 10 req/s you're nowhere near either ceiling. Pick async if your downstream stack (DB, queue, downstream APIs) is already async; pick threads if any piece of your stack is sync-only.\n\n"
                "The thing that actually scales: a connection pool with HTTP keep-alive on. httpx and requests both default to this — verify it's on, set max_connections to something like 20, and you'll get linear scaling to whatever your target server tolerates.\n\n"
                "(Caveat on the 2026 question: PEP 703 — no-GIL Python — is now usable in 3.13/3.14, which makes the thread story stronger than it was when the async/threads debate was fresh. If your stack is already sync, the no-GIL path is increasingly viable.)",
                "Researching the current state — checking httpx defaults, PEP 703 status, and whether the OS thread cost has materially changed.",
            ),
        ],
        "followups": [
            "what does max_connections actually limit — total or per-host?",
            "how do I check if requests is reusing the connection pool",
        ],
    },
    {
        "title": "tailscale ssh — when does the device-key handshake happen",
        "model_id": MODEL_B,
        "created_ago_days": 11,
        "messages": [
            (
                "user",
                "tailscale ssh — when I run `tailscale ssh user@machine`, does it hit my login server or does the device key handshake happen peer-to-peer?",
            ),
            (
                "assistant",
                "Peer-to-peer for the actual SSH session, but the control plane runs through the login server (in the hosted case, login.tailscale.com).\n\n"
                "Flow:\n"
                "1. Your `tailscale` CLI checks the local tailscaled daemon's cached ACL: am I allowed to SSH to this machine as that user?\n"
                "2. If yes, the CLI opens a WireGuard tunnel to the target (peer-to-peer if NAT traversal works; DERP-relayed otherwise).\n"
                "3. SSH session runs over that tunnel, terminated by tailscaled on the remote side, which acts as the SSH server.\n"
                "4. The remote tailscaled verifies the session token against ITS cached ACL.\n\n"
                "The login server is contacted on initial node auth (once per machine), on ACL changes (which propagate as cache updates), and on re-key events (every ~180 days for the node key by default). It is NOT contacted on every SSH session.\n\n"
                "If you run a self-hosted Headscale, the same flow applies but the login server is whatever you've pointed your nodes at.",
                None,
            ),
        ],
    },
]

MEMORY_INSIGHTS = [
    "Operator prefers terse, action-oriented replies. Lead with the answer, no narrative preamble.",
    "Lab fleet: Mac Studio M2 Max 32GB (inference node), MacBook Pro M3 Pro (dev), wired GbE + Tailscale.",
    "Default local stack: LM Studio + qwen3.6-35b-a3b-mlx and qwen3.5-122b-a10b for heavier work. Embedding model is nomic-embed-text-v1.5.",
    "Operator's 8-year-old is currently into Stargate (SG-1 first, then Atlantis). Keep spoilers light unless she's already there.",
    "Reading taste: military sci-fi, Becky Chambers, Murderbot, hard non-fiction on cryptography + infrastructure. Will DNF gray-paste prose.",
    "When reviewing code: soundness first (panics, lifetimes, unsafe), then correctness, then style. No fluff comments.",
]


# ---------------------------------------------------------------------------
# Live-apply helpers — used when SEED_APPLY_TO_LIVE=1
# ---------------------------------------------------------------------------
async def _wipe_user_content(engine: AsyncEngine, user_id: int) -> None:
    """Remove the user's chats / projects / memory / documents / prompts
    before re-seeding the live DB. Keeps the user account intact.

    Order matters: child rows first (messages → chats; chunks → documents)
    so foreign keys don't slap us.
    """
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "DELETE FROM messages WHERE chat_id IN "
                "(SELECT id FROM chats WHERE user_id = :uid)"
            ),
            {"uid": user_id},
        )
        await conn.execute(text("DELETE FROM chats WHERE user_id = :uid"), {"uid": user_id})
        await conn.execute(
            text(
                "DELETE FROM document_chunks WHERE document_id IN "
                "(SELECT id FROM documents WHERE user_id = :uid)"
            ),
            {"uid": user_id},
        )
        await conn.execute(text("DELETE FROM documents WHERE user_id = :uid"), {"uid": user_id})
        await conn.execute(text("DELETE FROM projects WHERE user_id = :uid"), {"uid": user_id})
        await conn.execute(text("DELETE FROM memory_insights WHERE user_id = :uid"), {"uid": user_id})
        await conn.execute(text("DELETE FROM prompts WHERE user_id = :uid"), {"uid": user_id})
        # Quotas + usage are reseeded below; clear so the inserts don't collide.
        await conn.execute(text("DELETE FROM quota_usage WHERE user_id = :uid"), {"uid": user_id})
        await conn.execute(text("DELETE FROM quotas WHERE user_id = :uid"), {"uid": user_id})


async def _drop_test_residue_users(engine: AsyncEngine) -> int:
    """Drop residual test/audit/pentest user accounts and their cascading
    rows. Matches usernames by well-known prefixes set by past test scripts.
    """
    prefixes = ("audit", "auditA", "auditB", "auditC", "auditX", "auditZ",
                "auditapi", "auditc", "auditx", "audit2", "dosbomb")
    cleared = 0
    async with engine.begin() as conn:
        result = await conn.execute(
            text(
                "SELECT id FROM users WHERE "
                + " OR ".join(f"username LIKE '{p}%'" for p in prefixes)
            )
        )
        ids = [row[0] for row in result.fetchall()]
        for uid in ids:
            # Same cascade order as _wipe_user_content
            await conn.execute(
                text(
                    "DELETE FROM messages WHERE chat_id IN "
                    "(SELECT id FROM chats WHERE user_id = :uid)"
                ),
                {"uid": uid},
            )
            await conn.execute(text("DELETE FROM chats WHERE user_id = :uid"), {"uid": uid})
            await conn.execute(
                text(
                    "DELETE FROM document_chunks WHERE document_id IN "
                    "(SELECT id FROM documents WHERE user_id = :uid)"
                ),
                {"uid": uid},
            )
            await conn.execute(text("DELETE FROM documents WHERE user_id = :uid"), {"uid": uid})
            await conn.execute(text("DELETE FROM projects WHERE user_id = :uid"), {"uid": uid})
            await conn.execute(text("DELETE FROM memory_insights WHERE user_id = :uid"), {"uid": uid})
            await conn.execute(text("DELETE FROM prompts WHERE user_id = :uid"), {"uid": uid})
            await conn.execute(text("DELETE FROM quota_usage WHERE user_id = :uid"), {"uid": uid})
            await conn.execute(text("DELETE FROM quotas WHERE user_id = :uid"), {"uid": uid})
            await conn.execute(text("DELETE FROM users WHERE id = :uid"), {"uid": uid})
            cleared += 1
    return cleared


# ---------------------------------------------------------------------------
# Main seed routine
# ---------------------------------------------------------------------------
async def main() -> None:
    database_url = os.environ["DATABASE_URL"]
    print(f"[seed] DATABASE_URL = {database_url}")
    print(f"[seed] APPLY_TO_LIVE = {APPLY_TO_LIVE}")

    engine: AsyncEngine = create_async_engine(database_url)

    if APPLY_TO_LIVE:
        # Live DB already has schema + alembic state. Skip bootstrap. Pull
        # the existing operator user and wipe their content before re-seed.
        async with engine.begin() as conn:
            result = await conn.execute(
                text("SELECT id, is_admin FROM users WHERE username = :u"),
                {"u": LIVE_USERNAME},
            )
            row = result.fetchone()
        if row is None:
            raise RuntimeError(
                f"APPLY_TO_LIVE set but no user named {LIVE_USERNAME!r} exists "
                f"in {database_url}. Refusing to register a new admin in live mode."
            )

        # Build a duck-typed user reference compatible with the rest of
        # main() (which only reads `.id` + `.is_admin`). SimpleNamespace
        # keeps pyright happy without committing to the auth_service.User
        # dataclass shape.
        from types import SimpleNamespace
        user = SimpleNamespace(id=int(row[0]), is_admin=bool(row[1]))
        print(f"[seed] using existing user {LIVE_USERNAME!r} (id={user.id}, admin={user.is_admin})")

        cleared_test_users = await _drop_test_residue_users(engine)
        print(f"[seed] dropped {cleared_test_users} residual audit/pentest users")

        await _wipe_user_content(engine, user_id=user.id)
        print(f"[seed] wiped {LIVE_USERNAME!r}'s chats/projects/memory/documents/prompts")
    else:
        # /tmp mode — schema bootstrap + register the demo admin user.
        async with engine.begin() as conn:
            await conn.run_sync(metadata.create_all)
        await asyncio.to_thread(_stamp_alembic_head)
        print("[seed] schema created + stamped head")

        user = await auth_service.register(
            username=ADMIN_USERNAME,
            password=ADMIN_PASSWORD,
            engine=engine,
        )
        print(
            f"[seed] user {ADMIN_USERNAME!r} (id={user.id}, admin={user.is_admin}) "
            "— DEMO ONLY, change the password before real use"
        )

    # ------------------------------------------------------------------
    # Projects — Stargate (the centerpiece), Tech, Family, Books
    # ------------------------------------------------------------------
    proj_svc = projects_service.ProjectsService(engine=engine)

    stargate = await proj_svc.create(
        user_id=user.id,
        name="Stargate companion",
        description="Show-along reference for the SG-1 / Atlantis rewatch — lore, characters, in-universe tech.",
        system_prompt=STARGATE_SYSTEM_PROMPT,
    )
    tech = await proj_svc.create(
        user_id=user.id,
        name="Tech tinkering",
        description="Home-lab thinking partner — Mac Studio, ZFS, Tailscale, a local model fleet.",
        system_prompt=TECH_SYSTEM_PROMPT,
    )
    family = await proj_svc.create(
        user_id=user.id,
        name="Family stuff",
        description="The household — kid questions, weekend plans, what's for dinner.",
        system_prompt=FAMILY_SYSTEM_PROMPT,
    )
    books = await proj_svc.create(
        user_id=user.id,
        name="Book recs",
        description="What to read next — military sci-fi, character-driven space opera, hard non-fiction.",
        system_prompt=BOOKS_SYSTEM_PROMPT,
    )

    # Backfill embedding_model_id + default_model_id on projects so the
    # Projects page renders the model pin. ProjectsService.create doesn't
    # expose these, so we patch directly.
    async with engine.begin() as conn:
        for project_id, default_model in (
            (stargate.id, MODEL_B),
            (tech.id, MODEL_A),
            (family.id, MODEL_A),
            (books.id, MODEL_B),
        ):
            await conn.execute(
                text(
                    "UPDATE projects SET embedding_model_id = :emb, "
                    "default_model_id = :mdl WHERE id = :pid"
                ),
                {"emb": EMBEDDING_MODEL, "mdl": default_model, "pid": project_id},
            )

    print(
        f"[seed] projects: "
        f"{stargate.id}={stargate.name!r}, {tech.id}={tech.name!r}, "
        f"{family.id}={family.name!r}, {books.id}={books.name!r}"
    )

    # ------------------------------------------------------------------
    # Documents + chunks — one document per project, ~2-3 chunks each.
    # ------------------------------------------------------------------
    doc_seed_specs = [
        (stargate.id, "Stargate canon — annotated reference.md", STARGATE_KB_CHUNKS),
        (tech.id, "Home-server build journal — May 2026.md", TECH_KB_CHUNKS),
        (family.id, "Household notes — taste, kid, baseline.md", FAMILY_KB_CHUNKS),
        (books.id, "Reading taste profile.md", BOOKS_KB_CHUNKS),
    ]

    doc_count = 0
    chunk_count = 0
    async with engine.begin() as conn:
        for project_id, title, kb_chunks in doc_seed_specs:
            raw = "\n\n".join(kb_chunks).encode()
            doc_sha = sha256(raw).hexdigest()
            result = await conn.execute(
                insert(documents).values(
                    user_id=user.id,
                    title=title,
                    mime_type="text/markdown",
                    byte_size=len(raw),
                    chunk_count=len(kb_chunks),
                    embedding_model_id=EMBEDDING_MODEL,
                    sha256=doc_sha,
                    project_id=project_id,
                    uploaded_at=_ago(days=12),
                )
            )
            doc_pk = result.inserted_primary_key
            if doc_pk is None:
                raise RuntimeError("INSERT documents returned no PK")
            doc_id = int(doc_pk[0])
            doc_count += 1

            for ordinal, chunk_text in enumerate(kb_chunks):
                await conn.execute(
                    insert(document_chunks).values(
                        document_id=doc_id,
                        ordinal=ordinal,
                        text=chunk_text,
                        text_hash=_text_hash(chunk_text),
                        embedding=_packed_embedding(doc_id * 100 + ordinal),
                    )
                )
                chunk_count += 1
    print(f"[seed] documents={doc_count}, chunks={chunk_count}")

    # ------------------------------------------------------------------
    # Chats + messages
    # ------------------------------------------------------------------
    chat_count = 0
    message_count = 0

    async def insert_chat_block(
        *,
        project_id: int | None,
        spec: dict[str, Any],
    ) -> None:
        nonlocal chat_count, message_count

        created_at = _ago(
            days=spec.get("created_ago_days", 0),
            hours=spec.get("created_ago_hours", 0),
        )
        settings_blob: dict[str, Any] = {}
        if "slash" in spec:
            # /research and /code render an active sub-session badge.
            settings_blob["active_slash"] = spec["slash"]
        if "followups" in spec:
            settings_blob["last_followups"] = spec["followups"]
        if "active_preset" in spec:
            # Preset chip rendered on assistant turns in the chat view.
            settings_blob["active_preset"] = spec["active_preset"]

        async with engine.begin() as conn:
            chat_result = await conn.execute(
                insert(chats).values(
                    user_id=user.id,
                    title=spec["title"],
                    model_id=spec["model_id"],
                    project_id=project_id,
                    created_at=created_at,
                    updated_at=created_at,
                    settings=settings_blob,
                )
            )
            chat_pk = chat_result.inserted_primary_key
            if chat_pk is None:
                raise RuntimeError("INSERT chats returned no PK")
            chat_id = int(chat_pk[0])
            chat_count += 1

            msg_offset = timedelta(seconds=0)
            for entry in spec["messages"]:
                role = entry[0]
                content = entry[1]
                reasoning = entry[2] if len(entry) > 2 else None
                msg_offset += timedelta(minutes=1)
                model_for_msg = spec["model_id"] if role == "assistant" else None
                await conn.execute(
                    insert(messages).values(
                        chat_id=chat_id,
                        role=role,
                        content=content,
                        reasoning_content=reasoning,
                        model_id=model_for_msg,
                        # state defaults vary by column; explicit "final"
                        # so the BE doesn't treat seeded rows as in-flight
                        # drafts and refuse new streams with 409.
                        state="final",
                        created_at=created_at + msg_offset,
                    )
                )
                message_count += 1

    for spec in STARGATE_CHATS:
        await insert_chat_block(project_id=stargate.id, spec=spec)
    for spec in TECH_CHATS:
        await insert_chat_block(project_id=tech.id, spec=spec)
    for spec in FAMILY_CHATS:
        await insert_chat_block(project_id=family.id, spec=spec)
    for spec in BOOKS_CHATS:
        await insert_chat_block(project_id=books.id, spec=spec)
    for spec in DETACHED_CHATS:
        await insert_chat_block(project_id=None, spec=spec)

    print(f"[seed] chats={chat_count}, messages={message_count}")

    # ------------------------------------------------------------------
    # Memory insights — pinned, so the Memory page is populated.
    # ------------------------------------------------------------------
    async with engine.begin() as conn:
        for idx, insight_text in enumerate(MEMORY_INSIGHTS):
            await conn.execute(
                insert(memory_insights).values(
                    user_id=user.id,
                    text=insight_text,
                    text_hash=_text_hash(insight_text),
                    pinned=True,
                    category="context",
                    created_at=_ago(days=20 - idx * 2),
                )
            )
    print(f"[seed] memory_insights={len(MEMORY_INSIGHTS)}")

    # ------------------------------------------------------------------
    # MCP integrations — the tools list the composer shows. Defined in
    # alembic migrations 0009 + 0015, NOT in db/schema.py, so /tmp seed
    # mode (which uses metadata.create_all) has to CREATE TABLE first.
    # Live-apply mode finds the table already there from alembic.
    #
    # Why this exists: commit 99f1785 dropped ~/.lmstudio/mcp.json file
    # discovery, so fresh installs no longer auto-surface MCP servers.
    # The composer integrations panel reads from this DB table; without
    # it the user sees "no tools available" even with a healthy MCP
    # config on the LM Studio side. Seeding a sensible default list
    # closes that gap for both demos AND for operator re-installs.
    # ------------------------------------------------------------------
    mcp_integrations_seed = [
        ("mcp/context7", 10, False),
        ("mcp/deepwiki", 20, False),
        ("mcp/crawl4ai", 30, False),
        ("mcp/searxng", 40, True),
        ("mcp/playwright", 50, False),
        ("mcp/wolfram", 60, False),
        ("mcp/paper-search-mcp", 70, False),
        ("mcp/sequential-thinking", 80, False),
        ("mcp/filesystem", 90, False),
    ]
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "CREATE TABLE IF NOT EXISTS mcp_integrations_list ("
                "id INTEGER NOT NULL PRIMARY KEY, "
                "value VARCHAR(255) NOT NULL UNIQUE, "
                "sort_order INTEGER NOT NULL, "
                "created_at DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL, "
                "updated_at DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL, "
                "enabled_by_default INTEGER DEFAULT 0 NOT NULL"
                ")"
            )
        )
        # Clear any existing rows so re-seeds are idempotent.
        await conn.execute(text("DELETE FROM mcp_integrations_list"))
        for value, sort_order, enabled in mcp_integrations_seed:
            await conn.execute(
                text(
                    "INSERT INTO mcp_integrations_list "
                    "(value, sort_order, enabled_by_default) "
                    "VALUES (:v, :s, :e)"
                ),
                {"v": value, "s": sort_order, "e": 1 if enabled else 0},
            )
    print(f"[seed] mcp_integrations={len(mcp_integrations_seed)}")

    # ------------------------------------------------------------------
    # Quotas — healthy band so the chips look populated, not empty.
    # ------------------------------------------------------------------
    today = date.today()
    async with engine.begin() as conn:
        await conn.execute(
            insert(quotas).values(
                user_id=user.id,
                tokens_per_day=50_000,
                requests_per_day=500,
                model_admin_ops_per_day=100,
            )
        )
        await conn.execute(
            insert(quota_usage).values(
                user_id=user.id,
                date=today,
                tokens_consumed=1_240,
                requests_consumed=18,
                model_admin_ops_consumed=2,
            )
        )
    print("[seed] quotas + quota_usage (1.2K / 50K tokens today)")

    # ------------------------------------------------------------------
    # Prompts — a small library so the Prompts page isn't empty.
    # ------------------------------------------------------------------
    prompt_seed = [
        (
            "Stargate canon check",
            "Was that scene canon or did I hallucinate it? Tell me episode + "
            "season if it exists, or say plainly if it doesn't. Don't make up "
            "an episode number to fill the gap.",
        ),
        (
            "Compare two options",
            "Compare these two options as A vs B. Lead with which I should pick "
            "and why. Then one paragraph each on the trade-offs.",
        ),
        (
            "Book rec — but specifically",
            "Recommend a book for me. Constraints: I just finished [BOOK]. Don't "
            "recommend the obvious sequel or the obvious 'people who liked X "
            "also liked Y' answer. Tell me what's adjacent that I won't have "
            "found on my own.",
        ),
    ]
    async with engine.begin() as conn:
        for name, content in prompt_seed:
            await conn.execute(
                insert(prompts).values(
                    user_id=user.id,
                    name=name,
                    content=content,
                )
            )
    print(f"[seed] prompts={len(prompt_seed)}")

    # ------------------------------------------------------------------
    # Provider configs — one LM Studio (auto-wired) + one OpenRouter row
    # so the Providers settings page has content to screenshot.
    # api_key_enc is a placeholder sentinel — not a real secret.
    # ------------------------------------------------------------------
    async with engine.begin() as conn:
        # OR IGNORE: in APPLY_TO_LIVE mode the operator's real provider config
        # already exists (it's not user-scoped, so the content wipe leaves it).
        # Keep the existing row rather than clobbering it with this placeholder.
        await conn.execute(
            insert(provider_configs).prefix_with("OR IGNORE").values(
                provider="openrouter",
                base_url="https://openrouter.ai/api/v1",
                # NULL api_key_enc — key not yet entered (screenshot shows
                # the row + "Test Connection" button; no real secret stored).
                api_key_enc=None,
                default_model="openai/gpt-4o",
                allowed_models=None,
                extra_headers={"HTTP-Referer": "http://localhost:3001", "X-Title": "LMChat"},
                enabled=True,
            )
        )
    print("[seed] provider_configs: openrouter")

    # ------------------------------------------------------------------
    # MCP servers — seed context7 and filesystem as "installed" so the
    # MCP Store page shows an installed-servers list, not empty state.
    # ------------------------------------------------------------------
    async with engine.begin() as conn:
        await conn.execute(
            insert(mcp_servers).prefix_with("OR IGNORE").values(
                slug="context7",
                name="Context7",
                transport="stdio",
                command="npx",
                args=["-y", "@upstash/context7-mcp"],
                url=None,
                secrets_enc=None,
                enabled=True,
                source="catalog",
                trust="catalog",
                consented=True,
                tool_policy=None,
            )
        )
        await conn.execute(
            insert(mcp_servers).prefix_with("OR IGNORE").values(
                slug="filesystem",
                name="Filesystem",
                transport="stdio",
                command="npx",
                args=["-y", "@modelcontextprotocol/server-filesystem", "/tmp"],
                url=None,
                secrets_enc=None,
                enabled=True,
                source="catalog",
                trust="catalog",
                consented=True,
                tool_policy=None,
            )
        )
    print("[seed] mcp_servers: context7, filesystem")

    # ------------------------------------------------------------------
    # Summary
    # ------------------------------------------------------------------
    print(
        f"\n[seed] DONE — 1 user / 4 projects / {chat_count} chats / "
        f"{message_count} messages / {doc_count} documents / "
        f"{len(MEMORY_INSIGHTS)} memory insights"
    )

    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
