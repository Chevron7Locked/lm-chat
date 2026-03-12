#!/usr/bin/env python3
"""Seed a dev database with realistic fake conversations.

Writes to dev.db by default — never touches the production database.
Use LM_CHAT_DB env var to override, but only with explicit --force flag.
"""
import sqlite3
import time
import uuid
import os
import sys

PROD_DB = os.path.join(os.path.dirname(__file__), "chats.db")
DEV_DB = os.path.join(os.path.dirname(__file__), "dev.db")
DB_PATH = os.environ.get("LM_CHAT_DB", DEV_DB)

def ts(hours_ago):
    return time.time() - (hours_ago * 3600)

def make_chat(db, title, folder, messages, hours_ago=1, pinned=0):
    chat_id = str(uuid.uuid4())
    db.execute(
        "INSERT INTO chats (id, title, model, updated_at, user_id, pinned, folder) VALUES (?,?,?,?,?,?,?)",
        (chat_id, title, "qwen3-30b-a3b", ts(hours_ago), "default", pinned, folder),
    )
    for i, (role, content) in enumerate(messages):
        db.execute(
            "INSERT INTO messages (chat_id, role, content, created_at) VALUES (?,?,?,?)",
            (chat_id, role, content, ts(hours_ago) + i),
        )
    return chat_id

def main():
    # Safety: refuse to touch production DB without explicit --force
    target = os.path.realpath(DB_PATH)
    prod = os.path.realpath(PROD_DB)
    if target == prod:
        if "--force" not in sys.argv:
            print(f"ERROR: refusing to overwrite production database ({PROD_DB})")
            print(f"  seed_db.py writes to dev.db by default.")
            print(f"  To seed prod, run: python seed_db.py --force")
            sys.exit(1)
        print(f"WARNING: --force flag set, writing to production DB: {PROD_DB}")

    # Remove existing target DB (it's dev.db, safe to replace)
    if os.path.exists(DB_PATH):
        os.remove(DB_PATH)
        # Clean up WAL/SHM files too
        for suffix in ("-wal", "-shm"):
            p = DB_PATH + suffix
            if os.path.exists(p):
                os.remove(p)

    # Use server's init_db to create schema (single source of truth)
    sys.path.insert(0, os.path.dirname(__file__))
    original_db = os.environ.get("LM_CHAT_DB")
    os.environ["LM_CHAT_DB"] = DB_PATH
    os.environ.setdefault("LM_CHAT_AUTH", "false")
    from server import init_db
    init_db()
    if original_db is not None:
        os.environ["LM_CHAT_DB"] = original_db
    else:
        del os.environ["LM_CHAT_DB"]

    db = sqlite3.connect(DB_PATH)
    db.execute("PRAGMA foreign_keys=ON")

    # --- Stargate (pinned) ---
    make_chat(db, "Best Stargate episodes ranked", "TV & Movies", [
        ("user", "What are the top 10 Stargate SG-1 episodes? I'm trying to convince a friend to watch."),
        ("assistant", "Great picks for a newcomer! Here's my ranking:\n\n1. **Window of Opportunity** (S4E6) — The time loop episode. Perfect comedy, perfect heart.\n2. **Heroes** (S7E17-18) — Documentary-style two-parter that hits like a truck.\n3. **Abyss** (S6E6) — Jack trapped with Ba'al. RDA at his finest.\n4. **The Fifth Race** (S2E15) — Jack gets Ancient knowledge downloaded. Sets up so much lore.\n5. **Meridian** (S5E21) — Daniel's ascension. Bring tissues.\n6. **2010** (S4E16) — The Aschen dystopia. Brilliant sci-fi premise.\n7. **Unending** (S10E20) — The series finale. Decades on the Odyssey.\n8. **Lost City** (S7E21-22) — The Atlantis lead-in. Jack in the chair.\n9. **Threads** (S8E18) — Resolves so many arcs beautifully.\n10. **Torment of Tantalus** (S1E11) — Ernest's planet. Pure wonder.\n\nStart them with Window of Opportunity — if they don't love it, Stargate might not be for them."),
        ("user", "Perfect. What about Atlantis? Top 5?"),
        ("assistant", "Atlantis hits different — more isolated, higher stakes:\n\n1. **The Siege** (S1E19-20, S2E1) — Three-parter. The Wraith arrive. Pure tension.\n2. **Common Ground** (S3E7) — Sheppard and a Wraith form an alliance. One of the best character episodes.\n3. **The Last Man** (S4E20) — McKay's 48,000-year journey. Heartbreaking.\n4. **Vegas** (S5E19) — The alt-universe noir detective episode. Visually stunning, completely unexpected.\n5. **Rising** (S1E1-2) — The pilot still holds up. That first moment stepping into Atlantis.\n\nHonestly the first three seasons are remarkably consistent. It's the rare spinoff that found its own identity fast."),
    ], hours_ago=2, pinned=1)

    # --- Programming ---
    make_chat(db, "Python async patterns for API servers", "Dev", [
        ("user", "I keep seeing people use asyncio with FastAPI but I'm confused about when to use async vs sync endpoints. When does it actually matter?"),
        ("assistant", "The rule is simpler than most tutorials make it:\n\n**Use `async def` when** your endpoint does I/O that has an async driver — HTTP calls with `httpx.AsyncClient`, async database queries (SQLAlchemy async, asyncpg), Redis with `aioredis`, etc.\n\n**Use plain `def` when** your endpoint uses blocking I/O — standard `requests`, synchronous DB drivers, file system ops. FastAPI automatically runs sync endpoints in a thread pool, so they won't block the event loop.\n\nThe trap people fall into:\n```python\n# BAD — blocks the event loop\n@app.get(\"/data\")\nasync def get_data():\n    result = requests.get(\"https://api.example.com\")  # sync call in async!\n    return result.json()\n\n# GOOD — either go full async\n@app.get(\"/data\")\nasync def get_data():\n    async with httpx.AsyncClient() as client:\n        result = await client.get(\"https://api.example.com\")\n    return result.json()\n\n# ALSO GOOD — or stay sync and let FastAPI thread it\n@app.get(\"/data\")\ndef get_data():\n    result = requests.get(\"https://api.example.com\")\n    return result.json()\n```\n\nThe performance difference only shows under concurrent load. A single request doesn't care. But 100 simultaneous users hitting a blocking async endpoint will queue up."),
        ("user", "That makes way more sense now. So my server.py that uses the standard library http.server — there's no point making that async right?"),
        ("assistant", "Exactly. `http.server` is fundamentally synchronous — one thread per request. Adding asyncio on top would just add complexity for zero benefit.\n\nIf you ever wanted to move to async, you'd switch the entire server to something like FastAPI + uvicorn or even just `aiohttp`. But for a local tool serving one user, `ThreadingHTTPServer` is perfectly fine — it's simple, it works, and there's no concurrency bottleneck when you're the only user hitting it."),
    ], hours_ago=5)

    make_chat(db, "Git rebase vs merge strategy", "Dev", [
        ("user", "My team at work is arguing about rebase vs merge. I come from a merge-only background but the senior devs swear by rebase. What's the actual tradeoff?"),
        ("assistant", "Both are valid — the real question is what you optimize for:\n\n**Merge commits** preserve the exact history of what happened. You can see when branches diverged and converged. The downside is a noisy graph with merge commits everywhere.\n\n**Rebase** gives you a linear history — looks like everyone worked on one branch sequentially. Cleaner `git log`, easier `git bisect`. The tradeoff is you're rewriting history, which means force-pushing feature branches.\n\nThe pattern most successful teams use:\n- **Rebase** your feature branch onto main before merging (keeps it linear)\n- **Merge** (with `--no-ff`) into main so you get a merge commit marking the feature\n- **Never rebase** shared/published branches\n\nThis gives you linear history within features but clear markers of where features landed.\n\n```bash\n# On your feature branch\ngit fetch origin\ngit rebase origin/main\n# Fix conflicts if any, then\ngit push --force-with-lease  # safer than --force\n# Then merge via PR\n```\n\nThe `--force-with-lease` part is key — it refuses to push if someone else has pushed to your branch since you last fetched."),
    ], hours_ago=26)

    # --- Video Games ---
    make_chat(db, "Elden Ring DLC final boss tips", "Gaming", [
        ("user", "I've been stuck on Promised Consort Radahn for two days. Level 175 strength build. Any tips that don't involve respeccing?"),
        ("assistant", "Radahn phase 2 is brutal on pure strength. Some tips without respeccing:\n\n**Positioning is everything** — Stay at medium range. His combo strings are designed to punish both close huggers and far rollers. The sweet spot is just outside his sword reach so you can punish the recovery frames.\n\n**The grab is the run-killer** — When he raises his left hand with the golden glow, dodge INTO him and to the left. Rolling away gets you caught every time because the hitbox extends forward.\n\n**Poise damage matters** — You're strength build, so use that. Two-hand your heaviest weapon for jump attacks during his longer recovery windows. He staggers after ~3 good hits, and the critical hit window is generous.\n\n**The meteor phase transition** — Don't panic. Sprint perpendicular to the first meteor, then immediately roll the follow-up. There's a safe window to heal after the second lands.\n\n**Spirit ashes** — Mimic Tear is fine but Tiche is actually better here. She dodges his AoEs and her percent-based damage is great against his massive HP pool.\n\nMost importantly — learn phase 1 until you can do it hitless. You need all your flasks for phase 2."),
        ("user", "The grab was exactly what kept killing me. Dodging INTO him worked. Beat him on the third try after that. Thanks!"),
        ("assistant", "Let's go! That grab is the #1 wall for strength builds because the instinct is always to roll away from big scary attacks. Fromsoft designed it specifically to punish that instinct.\n\nNow go enjoy the ending cutscene — it's one of the best in the series."),
    ], hours_ago=8)

    make_chat(db, "Baldur's Gate 3 honor mode run", "Gaming", [
        ("user", "Starting an honor mode run in BG3. One save, permadeath. What class would you recommend for someone who's beaten tactician but wants the safest possible honor clear?"),
        ("assistant", "For the safest honor mode clear, it's hard to beat:\n\n**Swords Bard 10 / Fighter 2** — Here's why:\n\n- Extra Attack from Swords Bard + Action Surge from Fighter = massive burst turns\n- Full caster spell list for utility and emergency saves (Counterspell, Hypnotic Pattern, Dimension Door)\n- Defensive Flourish adds to AC on attacks — you can hit 25+ AC reliably\n- Jack of All Trades means you rarely fail skill checks, which prevents a lot of honor mode deaths from failed dialogue/exploration checks\n- Healing Word as a bonus action for emergency revives\n\nThe biggest honor mode killers aren't the boss fights — they're the ambushes and forced encounters where you get caught off guard. Bard handles those with Cutting Words (reaction to reduce enemy attack/damage), high initiative, and versatile spell list.\n\n**Key items to rush:**\n- Boots of Speed from Thulla in the Underdark (free Dash every turn)\n- Staff of Crones for the extra spell slot\n- Cloak of Protection (Dammon, Act 2)\n\n**Fights to be most careful with:**\n- Grym (the Adamantine Forge) — have a clear strategy before engaging\n- Balthazar — can combo kill you if initiative goes wrong\n- The Netherbrain final phase if you haven't stacked enough saving throws\n\nSave your inspiration points. In honor mode, they're literally life insurance."),
    ], hours_ago=48)

    # --- Family ---
    make_chat(db, "Anniversary dinner ideas", "Personal", [
        ("user", "It's our 5th anniversary next Saturday. She loves Italian food but we've done every Italian restaurant in the city. Need something creative."),
        ("assistant", "Five years — congrats! Here are some ideas beyond the usual restaurant:\n\n**1. Private pasta-making class** — Many cities have chefs who do in-home pasta workshops for couples. You learn to make fresh ravioli or tagliatelle together, then eat what you made. It's interactive and memorable.\n\n**2. Progressive Italian dinner** — Book three different spots for one course each. Aperitivo at a wine bar, primi at that new place downtown, dolce at a gelateria. Turn dinner into an evening adventure.\n\n**3. Cook it yourself, elevated** — Order from a premium ingredient supplier (Eataly ships nationwide): fresh burrata, real San Marzanos, imported pasta. Cook together at home with good wine and candles. The effort itself is romantic.\n\n**4. Italian supper club** — If your city has underground dining or supper clubs, look for an Italian-themed one. Intimate, unique, and usually BYOB.\n\n**5. Vineyard dinner** — Some local wineries do Italian-style multi-course pairing dinners on weekends. Beautiful setting, great wine, no reservations drama.\n\nThe pasta class is my top pick — you get an experience, not just a meal. And you'll have a new skill you can use for anniversary #6."),
        ("user", "The pasta class is brilliant. She's been saying she wants to learn. Booked one for Saturday. Thanks!"),
        ("assistant", "That's going to be a great night. Pro tip: if the class offers a wine pairing add-on, take it. Nothing pairs with fresh pasta like a good Barolo or Chianti Classico, and it keeps the evening flowing while you wait for things to cook.\n\nHappy anniversary!"),
    ], hours_ago=72)

    make_chat(db, "Weekend trip with the kids", "Personal", [
        ("user", "Looking for a low-key weekend trip idea. Two kids, ages 4 and 7. Within 3-4 hours driving distance. We're in the Bay Area. Not Disneyland."),
        ("assistant", "Perfect distance and age range for these:\n\n**Monterey / Carmel** (~2 hrs) — The aquarium is incredible for both ages. The 4-year-old will be mesmerized by the jellyfish and the touch pools, the 7-year-old can handle the full exhibits. Carmel beach is beautiful and flat for running around. Stay overnight and do the 17-Mile Drive the next morning.\n\n**Yosemite Valley** (~3.5 hrs) — Stay at Curry Village (canvas tent cabins — the kids will think it's an adventure). The valley floor is flat and stroller-friendly. Mirror Lake is an easy walk, and the 7-year-old can do lower Yosemite Falls trail. Skip Half Dome, obviously.\n\n**Santa Cruz** (~1.5 hrs) — Beach Boardwalk is free to enter (pay per ride). The 7-year-old is tall enough for the classic wooden roller coaster. Natural Bridges State Beach for tide pools. Easy and cheap.\n\n**Lake Tahoe South Shore** (~3.5 hrs) — In summer, the beaches are calm and the water is clear enough the kids can see the bottom. Heavenly gondola ride is a hit at any age. Pope Beach is the best for families.\n\nMy pick for your ages: **Monterey**. The aquarium alone fills a full day, Carmel is walkable and chill, and there's enough variety that both kids stay engaged without anyone melting down."),
    ], hours_ago=120)

    # --- TV & Movies ---
    make_chat(db, "Severance S2 theories", "TV & Movies", [
        ("user", "Just finished Severance season 2. What do you think is going on with Helly's outtie? The board scene was insane."),
        ("assistant", "That board scene changed everything. Here's my read:\n\n**Helly R. is playing a longer game than anyone realizes.** Her outie Helena Eagan didn't just go along with the severance floor reopening — she engineered it. The way she manipulated the board vote suggests she's been planning a hostile takeover of Lumon from the inside.\n\nThe key detail most people missed: when Helena's father addressed the board, the camera lingered on her expression. That wasn't loyalty — that was someone counting votes. She already knew the outcome.\n\n**The bigger theory:** I think Helena and Helly are more aligned than we're led to believe. The show has been hinting that the severance procedure isn't as clean a split as Lumon claims. Both halves share emotional residue — Mark S. cries without knowing why, Irving paints. Helly's outie might be feeling her innie's rebellion as a growing conviction that Lumon is wrong.\n\nThe revolving door scene in the finale was the most terrifying visual the show has done. That slow realization that the hallway loops...\n\n**For season 3:** I think we're heading toward a full convergence — innies and outies meeting. The technology is breaking down, and Cobel knows it."),
        ("user", "The revolving door scene literally gave me chills. This show is doing things nobody else in TV is doing right now."),
        ("assistant", "It really is. Ben Stiller's direction is extraordinary — he uses architecture and space as psychological horror. The Lumon building itself feels like an antagonist.\n\nWhat's remarkable is how the show makes corporate bureaucracy genuinely terrifying. The waffle party, the music dance experience, the egg bar — they're simultaneously absurd and deeply unsettling because they feel like real corporate wellness programs taken to their logical extreme.\n\nIf you haven't already, rewatch the season 1 finale back-to-back with the S2 premiere. There are visual parallels that are clearly intentional and hint at things that don't pay off until the S2 finale."),
    ], hours_ago=12)

    make_chat(db, "Movie recommendations like Arrival", "TV & Movies", [
        ("user", "I love Arrival. The linguistics angle, the slow burn, the emotional gut punch at the end. What else should I watch?"),
        ("assistant", "If Arrival is your benchmark, you want sci-fi that's more cerebral than spectacle:\n\n**Annihilation** (2018) — Natalie Portman leads an expedition into a mysterious expanding zone. Like Arrival, it's about confronting something truly alien. The lighthouse sequence is unforgettable.\n\n**Contact** (1997) — Jodie Foster, based on Carl Sagan's novel. The closest spiritual predecessor to Arrival. Science vs. faith, first contact, and a deeply personal story underneath.\n\n**Interstellar** (2014) — You've probably seen it, but it shares Arrival's thesis: love as a dimension of physics. The docking scene and the bookshelf scene hit the same way Louise's choice does.\n\n**Ex Machina** (2014) — Smaller scale but equally cerebral. Every conversation is a chess move. The ending will sit with you for days.\n\n**Solaris** (1972, Tarkovsky) — The original slow-burn first contact film. It's long and meditative, but if you loved Arrival's pacing, this is the grandfather of that approach.\n\n**Her** (2013) — Not aliens, but it's about communication across a fundamental divide. Spike Jonze's most human film.\n\nStart with Annihilation if you haven't seen it — it's the closest match in tone and ambition."),
    ], hours_ago=168)

    # --- Music / kima-hub ---
    make_chat(db, "Building a music library manager", "Dev", [
        ("user", "I've been working on kima-hub, my open source music library app. It just hit 1,100 stars on GitHub which is wild. Trying to figure out the best approach for audio fingerprinting to detect duplicates across different formats."),
        ("assistant", "1,100 stars — that's awesome, congrats! Audio fingerprinting for duplicate detection is a great feature. Here are the main approaches:\n\n**Chromaprint / AcoustID** — The gold standard for music fingerprinting. It generates a compact fingerprint from the audio's spectral characteristics. Two files of the same song in MP3 and FLAC will produce nearly identical fingerprints. The `pyacoustid` library wraps it nicely.\n\n```python\nimport acoustid\n\ndef get_fingerprint(path):\n    duration, fingerprint = acoustid.fingerprint_file(path)\n    return duration, fingerprint\n\n# Compare two files\nfp1 = get_fingerprint(\"song.mp3\")\nfp2 = get_fingerprint(\"song.flac\")\nscore = acoustid.compare_fingerprints(fp1[1], fp2[1])\n# score > 0.85 = almost certainly the same recording\n```\n\n**Perceptual hashing** — Faster but less accurate. Good for a first pass before running Chromaprint on candidates. Libraries like `imagehash` work on spectrograms.\n\n**Metadata-first approach** — Before fingerprinting (which is CPU-intensive), do a quick pass matching on MusicBrainz ID, then artist+title+duration. Only fingerprint the ambiguous cases.\n\nFor a library manager like kima-hub, I'd recommend the hybrid approach: metadata matching first (instant), then Chromaprint for unresolved matches (batch process in background). That way users get immediate results for obvious duplicates and thorough results for everything else.\n\nThe Chromaprint binaries are available via Homebrew (`brew install chromaprint`) so your Mac users are covered."),
        ("user", "The hybrid approach makes a lot of sense. Right now I'm doing pure metadata matching which catches maybe 70% of dupes. The other 30% are the remastered versions, different encodings, slight title variations. Chromaprint would nail those."),
        ("assistant", "Exactly — and those edge cases are where users get the most value. Nobody needs help finding two files literally named the same thing. The real win is when kima-hub says \"hey, this 2009 remaster of Dark Side of the Moon is probably the same album as your 1973 FLAC rip.\"\n\nOne implementation tip: cache the fingerprints in your DB alongside the file metadata. Chromaprint is deterministic for a given audio stream, so you only need to compute it once per file. Then duplicate detection becomes a database query instead of re-scanning the whole library.\n\n```sql\nALTER TABLE tracks ADD COLUMN fingerprint TEXT;\nALTER TABLE tracks ADD COLUMN fingerprint_hash TEXT; -- for fast lookups\nCREATE INDEX idx_tracks_fphash ON tracks(fingerprint_hash);\n```\n\nThe `fingerprint_hash` column is a truncated hash for fast candidate lookup — find potential matches quickly, then do the full comparison only on candidates. Keeps it snappy even for large libraries."),
    ], hours_ago=36)

    # --- More Stargate ---
    make_chat(db, "Stargate Atlantis vs Universe debate", "TV & Movies", [
        ("user", "Hot take: SGU was actually better than Atlantis. Fight me."),
        ("assistant", "I won't fight you because there's a real argument there — but it depends on what you value in Stargate.\n\n**SGU did character work that SG-1 and Atlantis never attempted.** Rush is one of the most complex characters in the franchise. The power dynamics on Destiny felt real in a way that Atlantis's relatively harmonious team never did. Eli's arc from slacker to essential crew member was genuinely earned.\n\n**But Atlantis nailed the adventure tone.** Sheppard's team felt like the spiritual successor to SG-1 in a way SGU deliberately wasn't. McKay's character development across 5 seasons is actually the best arc in the franchise — he goes from insufferable genius to someone you'd genuinely want on your team.\n\n**Where SGU lost people:** The Battlestar Galactica-inspired drama wasn't what Stargate fans signed up for. The communication stones plots were almost universally disliked. And it took until mid-season 2 to find its footing — by then, viewership had cratered.\n\n**The tragedy of SGU** is that season 2 was genuinely excellent. The drone episodes, the Eli-Rush dynamic, Destiny's true purpose reveal — it was building to something great. If it had started at that quality level, it might still be running.\n\nMy ranking: SG-1 > SGU S2 > Atlantis > SGU S1. But I completely understand anyone who puts Atlantis above all of SGU."),
    ], hours_ago=4)

    # --- Programming ---
    make_chat(db, "SQLite WAL mode explained", "Dev", [
        ("user", "My server.py uses PRAGMA journal_mode=WAL. I set it because the internet said to but I don't actually understand what it does. ELI5?"),
        ("assistant", "Great question — WAL is one of those things everyone copies without understanding.\n\n**Without WAL (default \"delete\" mode):**\nSQLite writes changes to a separate journal file, then copies them into the main DB file when done. While writing, **nobody can read**. Readers block writers, writers block readers. One operation at a time.\n\n**With WAL (Write-Ahead Logging):**\nSQLite appends changes to a separate `-wal` file. Readers keep reading the main DB file (they see the state before the write). The writer doesn't touch the main file until a checkpoint happens.\n\nResult: **readers never block writers, writers never block readers.** Multiple threads can read simultaneously while one thread writes.\n\nFor your HTTP server handling multiple requests, this is huge — without WAL, two users loading chats simultaneously would queue up. With WAL, they both read concurrently.\n\n**The tradeoff:** WAL is slightly slower for single-threaded write-heavy workloads, and the `-wal` file can grow large if you have lots of writes without checkpoints. But for a web app (many reads, occasional writes), it's a no-brainer.\n\n```\nDefault mode:  Read ←→ Write  (take turns)\nWAL mode:      Read → (anytime)  Write → (anytime, just not two at once)\n```\n\nThat one line in your code is probably preventing most of the \"database is locked\" errors you'd otherwise see."),
    ], hours_ago=52)

    # --- Personal ---
    make_chat(db, "Home office desk setup advice", "Personal", [
        ("user", "I'm redoing my home office. Mac Studio setup. Need a desk that can handle two 27\" monitors and still have space for a mic arm and audio interface. Budget around $800."),
        ("assistant", "For a Mac Studio dual-monitor setup with audio gear, you want depth and stability:\n\n**Top pick: Uplift V2 Standing Desk (60\" x 30\")** — ~$600\n- 30\" depth gives you room for monitors pushed back + audio interface in front\n- 355 lb capacity, rock solid\n- Programmable height presets (standing breaks are real)\n- Cable management tray included\n- Grommet holes for mic arm clamp\n\n**Runner up: IKEA IDÅSEN (63\" x 31.5\")** — ~$450\n- Deeper than most IKEA desks\n- Sit/stand with app control\n- Saves money for a nice monitor arm\n\n**If you want fixed height: Fully Jarvis Hardwood (60\" x 30\")** — ~$380\n- Beautiful bamboo or walnut top\n- Very stable (no wobble since no motor)\n- Use the savings on a good dual monitor arm\n\n**Essential add-ons regardless of desk:**\n- Dual monitor arm (Ergotron LX Dual ~$300, or Amazon Basics ~$110) — frees up massive desk space\n- Cable management tray underneath\n- Felt desk mat for the keyboard area\n\nThe Uplift V2 is the move if you can swing it. The standing option isn't a gimmick — when you're mixing in Logic Pro for hours, being able to switch positions prevents fatigue. And at 60\" wide, you'll have room for both monitors, the audio interface, and still have clear desk space for notes or a second keyboard."),
    ], hours_ago=200)

    # --- Gaming ---
    make_chat(db, "Best roguelikes 2025-2026", "Gaming", [
        ("user", "What are the essential roguelikes right now? I've played Hades 1 and 2, Slay the Spire, and Dead Cells."),
        ("assistant", "You've got great taste. Here's what to play next:\n\n**Balatro** — Poker-based roguelike deckbuilder. Sounds weird, plays incredible. Each run you build a deck of poker hands enhanced by Joker cards that multiply scores in absurd ways. Simple to learn, endlessly deep. This is the one that consumed my life in 2024.\n\n**Noita** — Every pixel is simulated physics. You're a wizard in procedurally generated caves where fire spreads, water flows, acid melts, and everything interacts. Dying to your own recklessness is the core experience. It's punishing but nothing else feels like it.\n\n**Caves of Qud** — Old-school ASCII roguelike with modern design sensibility. Set in a far-future post-apocalyptic world. You can be a mutant plant person with telekinesis. The world-building is extraordinary.\n\n**Vampire Survivors** — The most fun you'll have with a $5 game. Technically an auto-battler roguelike. Runs are 30 minutes, perfect for \"one more run\" at midnight.\n\n**Enter the Gungeon** — If you liked Dead Cells' action, this is the twin-stick shooter equivalent. Hundreds of guns, some incredibly creative.\n\n**Inscryption** — Part roguelike deckbuilder, part... something else entirely. Go in blind. Trust me.\n\nStart with **Balatro** if you want something fresh, or **Noita** if you want something that'll make you feel things."),
    ], hours_ago=96)

    db.commit()
    count = db.execute("SELECT COUNT(*) FROM chats").fetchone()[0]
    folders = db.execute("SELECT COUNT(DISTINCT folder) FROM chats WHERE folder IS NOT NULL").fetchone()[0]
    db.close()
    print(f"Seeded {DB_PATH} with {count} conversations across {folders} folders")

if __name__ == "__main__":
    main()
