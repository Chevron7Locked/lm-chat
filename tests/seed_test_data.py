"""Seed lm-chat test database with realistic fake conversations."""
import json, sqlite3, sys, time, uuid, os

DB_PATH = sys.argv[1] if len(sys.argv) > 1 else "test_chats.db"

# Remove existing test db
if os.path.exists(DB_PATH):
    os.remove(DB_PATH)

# Import init_db by adding parent to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["LM_CHAT_DB"] = DB_PATH
import server
server.DB_PATH = DB_PATH
server.init_db()

db = sqlite3.connect(DB_PATH)
db.execute("PRAGMA foreign_keys=ON")

def make_id():
    return f"c{uuid.uuid4().hex[:12]}"

def add_chat(title, messages, model="qwen3-30b-a3b", pinned=0, folder="", hours_ago=0):
    chat_id = make_id()
    now = time.time() - (hours_ago * 3600)
    db.execute(
        "INSERT INTO chats (id,title,model,updated_at,user_id,pinned,folder) VALUES (?,?,?,?,?,?,?)",
        (chat_id, title, model, now, "default", pinned, folder),
    )
    for i, (role, content) in enumerate(messages):
        db.execute(
            "INSERT INTO messages (chat_id,role,content,created_at) VALUES (?,?,?,?)",
            (chat_id, role, content, now + i),
        )
    return chat_id

# ── Family / Life chats ──

add_chat("Mum's 70th birthday ideas", [
    ("user", "Mum turns 70 in June. She's into gardening and watercolour painting. Budget around $500. What are some meaningful gift ideas?"),
    ("assistant", "Here are some ideas that combine her passions:\n\n1. **Custom watercolour garden portrait** — commission a local artist to paint her garden. Check Etsy for artists who work from photos ($150-250)\n\n2. **Royal Horticultural Society membership** — year-round access to RHS gardens plus their magazine ($85/year)\n\n3. **Winsor & Newton professional watercolour set** — the Cotman field plus kit with 24 half pans is gorgeous and portable ($120)\n\n4. **Garden day experience** — Great Dixter or Sissinghurst guided tour + afternoon tea, bring the whole family ($200 for a group)\n\n5. **Personalised garden journal** — Papier do a beautiful linen-bound one you can customise with her name ($45)\n\nThe portrait + membership combo hits $500 perfectly and gives her something lasting plus something ongoing."),
    ("user", "The portrait idea is perfect. She'd cry. How do I find a good artist on Etsy?"),
    ("assistant", "Search Etsy for \"custom watercolour house portrait\" or \"garden painting commission\" — the house portrait artists almost always do gardens too.\n\n**What to look for:**\n- 500+ sales with 4.8+ stars\n- Check their reviews for photos of received work (not just the listing photos)\n- Look for artists who show their process or reference photos alongside finished work\n- Turnaround time — June is tight, so filter for artists with 2-3 week delivery\n\n**When you commission:**\n- Send 3-4 photos of the garden from different angles, in good light\n- Mention specific plants she's proud of (if her roses or dahlias are her thing, say so)\n- Ask for A3 size minimum — small watercolours lose the garden detail\n- Request a digital proof before they post the original\n\nBudget $180-250 for quality work at A3 size. The really cheap ones ($50-80) tend to be digital illustrations made to look like watercolour, which isn't the same."),
], pinned=1, hours_ago=2)

add_chat("Slow cooker lamb shoulder", [
    ("user", "I've got a 1.5kg lamb shoulder and want to slow cook it. What's the best recipe?"),
    ("assistant", "This is my go-to for lamb shoulder — North African inspired, falls apart after 8 hours.\n\n**Ingredients:**\n- 1.5kg lamb shoulder (bone-in is better)\n- 2 onions, quartered\n- 6 garlic cloves, smashed\n- 400g tin chopped tomatoes\n- 1 preserved lemon, chopped (or zest of 2 lemons + 1 tbsp salt)\n- 2 tsp cumin, 2 tsp coriander, 1 tsp cinnamon, 1 tsp smoked paprika\n- 200ml chicken stock\n- Handful of green olives\n- Fresh coriander to finish\n\n**Method:**\n1. Brown the lamb in a hot pan — 3 mins each side, get it properly dark\n2. Layer onions and garlic in the slow cooker\n3. Mix spices into the tomatoes and stock, pour over\n4. Nestle the lamb in, add preserved lemon\n5. Low for 8 hours (or high for 5-6)\n6. Last 30 mins — add olives\n7. Pull the meat apart with forks, stir through the sauce\n\nServe over couscous with the sauce spooned on top and fresh coriander. The bone slides right out."),
], folder="Recipes", hours_ago=26)

add_chat("Weekend camping spots near Sydney", [
    ("user", "Looking for a good campsite within 2 hours of Sydney for next weekend. Somewhere with swimming, not too crowded."),
    ("assistant", "Here are my top picks:\n\n**1. Kangaroo Valley — Bendeela Recreation Area**\n- 2 hours south, free camping, right on the Kangaroo River\n- Deep swimming holes, platypus at dusk\n- Gets busy on long weekends but fine on regular ones\n- No bookings, first come first served — arrive Friday afternoon\n\n**2. Royal National Park — Bonnie Vale**\n- 1 hour south, bookable ($20/night)\n- Beach camping at Port Hacking, kayak access\n- Book NOW at nsw.gov.au — these go fast\n\n**3. Blue Mountains — Euroka Clearing**\n- 1.5 hours west, $12/adult/night\n- Kangaroos literally walk through camp\n- Swimming at Nepean River, 10 min walk\n- Very chill, no generators allowed\n\n**4. Myall Lakes — Mungo Brush**\n- 3 hours north (stretching your limit but worth it)\n- Lakeside + ocean beach, pristine\n- Bookable, usually available midweek\n\nFor a regular weekend, Euroka is hard to beat. Bendeela if you want free and more remote."),
    ("user", "Euroka sounds great. What should I pack?"),
    ("assistant", "For Euroka specifically:\n\n**Essentials:**\n- Tent, sleeping bags, mats (nights drop to 8-10°C this time of year)\n- Water — there are taps but bring 10L backup\n- Firewood — you can buy it at Glenbrook on the way in ($15 a bag from the servo)\n- Torch/headlamp — no lighting at all, it's properly dark\n- Cash for the self-registration station ($12/adult/night, $7 kids)\n\n**Don't forget:**\n- Rope or carabiner for food — kangaroos WILL investigate your esky overnight\n- Insect repellent — mosquitoes near the river at dusk\n- Reef-safe sunscreen for swimming\n- Old sneakers for the river walk (slippery rocks)\n\n**Nice to have:**\n- Camp chairs (there are no picnic tables at most sites)\n- French press + ground coffee for morning\n- Bluetooth speaker for evening (keep it low, sound carries)\n\nDrive in via Glenbrook entrance. The road is sealed all the way to the campground. Get there by 2pm on Saturday to grab a flat spot near the river."),
], hours_ago=48)

# ── Programming chats ──

add_chat("Python asyncio connection pool pattern", [
    ("user", "What's the cleanest pattern for a connection pool in asyncio? I keep getting race conditions with my current approach."),
    ("assistant", "The cleanest pattern uses `asyncio.Queue` as the pool backbone. It handles the concurrency for you — no locks needed.\n\n```python\nimport asyncio\nfrom contextlib import asynccontextmanager\n\nclass Pool:\n    def __init__(self, create_conn, max_size=10):\n        self._create = create_conn\n        self._queue = asyncio.Queue(maxsize=max_size)\n        self._size = 0\n        self._max = max_size\n\n    async def _new_conn(self):\n        conn = await self._create()\n        self._size += 1\n        return conn\n\n    @asynccontextmanager\n    async def acquire(self):\n        # Try to get an existing connection\n        if self._queue.empty() and self._size < self._max:\n            conn = await self._new_conn()\n        else:\n            conn = await self._queue.get()\n        try:\n            yield conn\n        finally:\n            await self._queue.put(conn)\n```\n\nUsage:\n```python\npool = Pool(create_conn=lambda: aiohttp.ClientSession())\n\nasync with pool.acquire() as session:\n    resp = await session.get(url)\n```\n\nThe race condition you're probably hitting: checking `len(pool) < max` and then creating — two coroutines can both pass the check. `Queue.get()` blocks atomically so this pattern avoids it entirely.\n\nFor production, add health checks on acquire (ping the connection, replace if stale) and a `close_all()` method."),
    ("user", "Nice. What about connection health checks? My connections go stale after 30 min idle."),
    ("assistant", "Wrap the acquire with a staleness check:\n\n```python\nimport time\n\nclass PooledConn:\n    __slots__ = ('conn', 'last_used')\n    def __init__(self, conn):\n        self.conn = conn\n        self.last_used = time.monotonic()\n\nMAX_IDLE = 1800  # 30 min\n\n@asynccontextmanager\nasync def acquire(self):\n    while True:\n        if self._queue.empty() and self._size < self._max:\n            raw = await self._create()\n            pc = PooledConn(raw)\n            break\n        pc = await self._queue.get()\n        if time.monotonic() - pc.last_used > MAX_IDLE:\n            await pc.conn.close()\n            self._size -= 1\n            continue  # discard stale, try again\n        break\n    try:\n        yield pc.conn\n    finally:\n        pc.last_used = time.monotonic()\n        await self._queue.put(pc)\n```\n\nThis discards stale connections lazily — only when someone tries to use them. No background health-check task needed.\n\nIf you want proactive cleanup (shrink the pool during quiet periods), add a background task that drains and replaces every 5 minutes. But lazy is usually enough."),
], pinned=1, folder="Code", hours_ago=5)

add_chat("kima-hub deployment checklist", [
    ("user", "I'm about to deploy kima-hub to production. Can you give me a pre-deploy checklist? It's a FastAPI app with PostgreSQL, Redis for caching, and S3 for file storage."),
    ("assistant", "Here's a systematic pre-deploy checklist for kima-hub:\n\n**Database:**\n- [ ] Run `alembic upgrade head` against a staging copy first\n- [ ] Check for missing indexes on foreign keys (Postgres doesn't auto-create these)\n- [ ] Verify connection pool settings — `pool_size=20, max_overflow=10` is a good starting point\n- [ ] Confirm `pg_dump` backup ran within the last hour\n- [ ] Test rollback: `alembic downgrade -1` should work cleanly\n\n**API:**\n- [ ] All env vars set: `DATABASE_URL`, `REDIS_URL`, `S3_BUCKET`, `SECRET_KEY`\n- [ ] CORS origins list updated for production domain\n- [ ] Rate limiting configured on auth endpoints\n- [ ] Health check endpoint (`/health`) returns 200 with db+redis connectivity check\n- [ ] OpenAPI docs disabled in production (`docs_url=None` in prod)\n\n**Redis:**\n- [ ] Cache TTLs reviewed — nothing set to infinite\n- [ ] Redis maxmemory-policy set to `allkeys-lru`\n- [ ] Test what happens when Redis is down (app should degrade, not crash)\n\n**S3:**\n- [ ] Bucket policy reviewed — no public access unless intentional\n- [ ] Pre-signed URL expiry set appropriately (15 min for uploads, 1 hour for downloads)\n- [ ] CORS on the bucket allows your frontend origin\n\n**Infrastructure:**\n- [ ] SSL termination configured (Caddy/nginx/ALB)\n- [ ] Container health check in Dockerfile: `HEALTHCHECK CMD curl -f http://localhost:8000/health`\n- [ ] Log aggregation working (stdout → CloudWatch/Datadog)\n- [ ] Graceful shutdown: `uvicorn --timeout-graceful-shutdown 30`\n\n**Smoke tests post-deploy:**\n- [ ] Can create account, login, refresh token\n- [ ] Can upload file, retrieve file\n- [ ] Can list/search/filter main entities\n- [ ] Websocket connections establish and receive events"),
    ("user", "Good list. What about the zero-downtime deploy strategy? We're on ECS."),
    ("assistant", "For ECS zero-downtime:\n\n**Service config:**\n```json\n{\n  \"deploymentConfiguration\": {\n    \"minimumHealthyPercent\": 100,\n    \"maximumPercent\": 200\n  },\n  \"healthCheckGracePeriodSeconds\": 60\n}\n```\n\nThis means ECS spins up new tasks *before* draining old ones. At peak you'll have 2x tasks running briefly.\n\n**ALB target group:**\n- Health check path: `/health`\n- Healthy threshold: 2 (two consecutive 200s)\n- Interval: 10s\n- Deregistration delay: 30s (matches your graceful shutdown timeout)\n\n**The deploy flow:**\n1. ECS starts new task(s) with updated image\n2. New tasks register with ALB target group\n3. ALB health checks pass (2 × 10s = 20s)\n4. ALB starts routing traffic to new tasks\n5. Old tasks enter DRAINING — ALB stops sending new requests\n6. 30s deregistration delay — in-flight requests complete\n7. ECS sends SIGTERM → uvicorn graceful shutdown (30s)\n8. Old tasks terminated\n\n**Critical for kima-hub specifically:**\n- If you have long-running websocket connections, bump deregistration delay to 120s\n- Alembic migrations must be backward-compatible (old code runs against new schema during rollover)\n- Redis cache keys should be version-namespaced if you're changing serialization formats\n\nTest this on staging first — do a deploy while running `hey -z 60s -c 50 https://staging.kima-hub.dev/health` to confirm zero dropped requests."),
], folder="Code", hours_ago=12)

add_chat("Git rebase vs merge — when to use which", [
    ("user", "I always get confused about when to rebase vs merge. What's the rule of thumb?"),
    ("assistant", "Simple rule:\n\n**Rebase** when you're updating YOUR branch with changes from main. This keeps your branch's history clean — your commits sit on top of the latest main.\n\n```bash\ngit checkout feature-branch\ngit rebase main\n```\n\n**Merge** when you're integrating a FINISHED branch into main. This preserves the branch history and creates a merge commit.\n\n```bash\ngit checkout main\ngit merge --no-ff feature-branch\n```\n\n**Never rebase:**\n- A branch that other people have pulled / are working on\n- main/master itself\n- After you've pushed and others have built on your commits\n\n**The mental model:** Rebase rewrites history (moves commits to a new base). If that history only exists on your machine, no harm done. If others have copies, you've just created a parallel universe that will cause merge conflicts.\n\n**My workflow:**\n1. Branch from main\n2. Work, commit, push to my fork\n3. Before PR: `git rebase main` to catch up\n4. PR reviewed → squash merge into main (GitHub button)\n\nSquash merge is the best of both worlds for PRs — clean main history, branch detail preserved in the PR itself."),
], hours_ago=72)

# ── Brainstorming / Daily driver chats ──

add_chat("Home office desk setup", [
    ("user", "I'm redoing my home office. Budget $2000 for the desk and monitor setup. I do programming and music production (Logic Pro). What do you recommend?"),
    ("assistant", "For coding + Logic Pro, you want screen real estate and a desk that handles audio gear:\n\n**Desk: Fully Jarvis standing desk — $700**\n- 180×80cm bamboo top (big enough for monitors + audio interface + speakers)\n- Programmable height presets (sit/stand with one button)\n- Cable management tray included\n- The bamboo is gorgeous and doesn't show fingerprints like the laminate\n\n**Monitor: LG 40WP95C-W 40\" ultrawide 5K2K — $1100**\n- 5120×2160 resolution — fits a full DAW timeline + mixer + code editor side by side\n- Thunderbolt 4 input — one cable to your Mac (powers it too if laptop)\n- 72W PD, built-in KVM switch\n- Nano IPS, 98% DCI-P3 — accurate for both code and waveforms\n- This is the monitor Apple engineers use internally\n\n**Remaining $200:**\n- Monitor arm: Ergotron LX ($130) — gets the monitor off the desk, frees space for speakers\n- Cable ties + under-desk cable tray ($40)\n- Felt desk mat ($30) — protects the bamboo and dampens keyboard noise\n\nIf Logic Pro is serious for you, consider whether you need space for studio monitors (Yamaha HS5 or similar). The ultrawide on an arm leaves plenty of room for a pair flanking it.\n\nTotal: $700 + $1100 + $200 = $2000 flat."),
], hours_ago=96)

add_chat("Best way to learn piano at 35", [
    ("user", "I'm 35 and want to learn piano. Never played an instrument. Is it too late? What's the best approach?"),
    ("assistant", "Not remotely too late. Adults actually learn theory and technique faster than kids — you just need to practice consistently (even 20 minutes daily beats 2 hours on Saturday).\n\n**Get a keyboard first:**\n- Yamaha P-145 ($550) — 88 weighted keys, feels like a real piano, compact\n- Don't buy a toy keyboard — weighted/hammer action keys matter for building proper finger strength\n\n**Learning path:**\n\n**Month 1-2: Foundations**\n- Simply Piano app or Pianote — gamified, keeps you motivated\n- Focus on: hand position, reading treble clef, C major scale, simple melodies\n- Learn 1-2 songs you actually like (even simplified versions)\n\n**Month 3-6: Building blocks**\n- Basic chord progressions: I-V-vi-IV in C, G, F major\n- Left hand independence (hardest part — your brain fights it)\n- Simple songs with both hands: Coldplay, Beatles, Adele have great beginner arrangements\n\n**Month 6-12: Getting musical**\n- Minor keys, basic music theory (intervals, chord inversions)\n- Dynamic control (playing soft/loud expressively)\n- At this point you'll be playing recognisable songs confidently\n\n**One real teacher lesson per month** makes a huge difference — they catch bad habits (wrist tension, finger curling) that apps miss. Even over Zoom.\n\n**The secret nobody tells you:** Month 2-3 is the hardest. You'll feel like you're not progressing. That's your brain building neural pathways. Push through and month 4 suddenly clicks."),
], hours_ago=120)

add_chat("Explain quantum computing to me like I'm 12", [
    ("user", "Can you explain quantum computing in a way a 12 year old would understand?"),
    ("assistant", "Imagine you're trying to find your way through a huge maze.\n\nA **regular computer** is like sending one person into the maze. They try one path, hit a dead end, go back, try another path. Eventually they find the exit, but they had to check paths one at a time.\n\nA **quantum computer** is like having a magical version of yourself that can split into thousands of copies and try EVERY path at the same time. The copies that hit dead ends just vanish, and the one that finds the exit waves and says \"over here!\"\n\nThe trick is something called a **qubit**. In a regular computer, a bit is like a light switch — it's either OFF (0) or ON (1). A qubit is like a coin spinning in the air — it's BOTH heads and tails at the same time until you catch it. This \"both at once\" thing is called superposition.\n\nWhen you have lots of qubits working together, they can explore tons of possibilities simultaneously. 50 qubits can represent 2^50 states at once — that's over a quadrillion combinations.\n\n**The catch:** The coin only works while it's spinning. The moment you look at it (measure it), it picks one answer. So quantum computers need very clever tricks to make sure the right answer is the one that comes out.\n\n**Also:** qubits are incredibly fragile. They need to be colder than outer space to work (-273°C). A single vibration or stray photon ruins everything. That's why quantum computers look like giant gold chandeliers in freezers.\n\n**What they're good at:** Breaking codes, simulating molecules for new medicines, optimising delivery routes with millions of stops.\n\n**What they're NOT good at:** Running Minecraft. Seriously — regular computers are still better for most everyday tasks."),
], hours_ago=168)

# ── A pinned reference chat ──

add_chat("Mac Studio server notes", [
    ("user", "What are the key specs and settings for my Mac Studio LLM server?"),
    ("assistant", "Here's your setup summary:\n\n**Hardware:**\n- Mac Studio (Mac15,14) — Apple M3 Ultra\n- 28 CPU cores (20P + 8E), 60 GPU cores\n- 256 GB unified memory\n- macOS Tahoe 26.4\n\n**LLM Server:**\n- LM Studio at `localhost:1234`\n- Models: `~/.lmstudio/models/` (Spotlight indexing excluded)\n- Best formats: GGUF Q4_K_M or Q5_K_M for speed/quality balance\n- MLX format 30-50% faster than llama.cpp on Apple Silicon\n- Metal GPU acceleration automatic\n\n**Power settings (optimised for always-on):**\n- System sleep: disabled on AC\n- Disk sleep: disabled (avoids model reload delays)\n- Power Nap: disabled\n- Display sleep: 10 min\n\n**Key rule:** Model must fit entirely in 256 GB unified memory — no swap. Memory bandwidth is the bottleneck, not compute.\n\n**For llama.cpp CPU inference:** `-t 20` (P-cores only, never include E-cores)"),
], pinned=1, hours_ago=240)

# ── A chat with tool use messages ──

add_chat("What's the weather in Melbourne today?", [
    ("user", "What's the weather like in Melbourne right now?"),
    ("assistant", "Let me look that up for you."),
], hours_ago=1)
# Add a tool message manually
now = time.time() - 3600
db.execute(
    "INSERT INTO messages (chat_id,role,content,name,output,created_at) VALUES (?,?,?,?,?,?)",
    (
        db.execute("SELECT id FROM chats WHERE title='What''s the weather in Melbourne today?'").fetchone()[0],
        "tool",
        None,
        "brave_search",
        "Melbourne, Australia: 14°C, partly cloudy, wind 15 km/h SW. UV index 2 (low). Forecast: clearing in the afternoon, high of 17°C.",
        now + 2,
    ),
)
# Add the assistant follow-up
db.execute(
    "INSERT INTO messages (chat_id,role,content,created_at) VALUES (?,?,?,?)",
    (
        db.execute("SELECT id FROM chats WHERE title='What''s the weather in Melbourne today?'").fetchone()[0],
        "assistant",
        "Melbourne right now: **14°C and partly cloudy**, with a light southwest wind at 15 km/h. UV is low (2), so no sunscreen stress.\n\nIt should clear up this afternoon with a high of 17°C. Not bad for winter!",
        now + 3,
    ),
)

db.commit()
db.close()
print(f"Seeded test database: {DB_PATH}")
print("10 chats: 2 pinned, 2 in folders, 1 with tool use, mix of topics")
