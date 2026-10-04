"""
Channel-powered shop bot (free Render friendly).
"""
import asyncio
import difflib
import os
import re
import time
import unicodedata

import discord
from aiohttp import web
from discord.ext import commands, tasks

# ======================= SETTINGS =======================
TOKEN = os.environ.get("DISCORD_TOKEN", "")
CATALOG_CHANNEL_ID = int(os.environ.get("CATALOG_CHANNEL_ID", "1554872115541319830"))
VOICE_CHANNEL_ID = int(os.environ.get("VOICE_CHANNEL_ID", "1517623589799596354"))
PORT = int(os.environ.get("PORT", "10000"))
PREFIX = "!"
HISTORY_LIMIT = 1000
COOLDOWN = 5
DELETE_AFTER = 60
BOT_VERSION = "v3.9"   # v3.9 = blacklist — bot ignores these channels

# Channels where the bot must NOT auto-reply. Everywhere else it works normally.
BLACKLIST_CHANNEL_IDS = {
    1506644600096559105,
    1506644501618233497,
    1524393860086894752,
    1547924934716686367,
    1506644665590349984,
    1506644782527545495,
    1506646093977489589,
    1506644846058934373,
    1506644904808415323,
    1524429980967239910,
}
# ========================================================

PAYMENT_TRIGGERS = [
    "bkash", "bikash", "bkas", "nagad", "nogod", "nagod", "rocket", "upay",
    "binance", "baince", "balance", "payment", "paypal",
]
EXACT_PAYMENT_TRIGGERS = ["pay"]
PAYMENT_MARKERS = {
    "bkash", "bikash", "nagad", "nogod", "rocket", "upay", "binance",
    "payment", "paypal", "usdt", "crypto",
}


# ------------------------ text helpers ------------------------
def fold(text):
    return unicodedata.normalize("NFKC", text).lower()


def words_of(text):
    return re.findall(r"\w+", fold(text))[:80]


def norm(text):
    return "".join(re.findall(r"\w+", fold(text)))


def threshold(length):
    if length <= 3:
        return 1.0
    return 0.8


def fuzzy_hit(words, target, max_size):
    need = threshold(len(target))
    for size in range(1, max_size + 1):
        for i in range(len(words) - size + 1):
            cand = "".join(words[i:i + size])
            if abs(len(cand) - len(target)) > 3:
                continue
            if cand == target:
                return True
            if difflib.SequenceMatcher(None, cand, target).ratio() >= need:
                return True
    return False


def name_mentioned(words, name):
    target = norm(name)
    if not target:
        return False
    max_size = min(len(name.split()) + 1, 4)
    return fuzzy_hit(words, target, max_size)


CATEGORY_WORDS = {"ios", "android", "pc", "phone", "mobile", "windows"}


def product_mentioned(words, product):
    names = [product["key"]] + product["aliases"]
    if any(name_mentioned(words, n) for n in names):
        return True
    base_words = [w for w in words_of(product["name"]) if w not in CATEGORY_WORDS]
    base = "".join(base_words)
    return len(base) >= 4 and fuzzy_hit(words, base, min(len(base_words) + 1, 4))


GENERIC_WORDS = {"ios", "android", "pc", "windows", "apk", "mod", "mods", "price", "list", "pro", "vip", "key"}

GENERIC_NAME_WORDS = {
    "premium", "license", "licence", "vip", "pro", "standard", "basic",
    "price", "list", "pricing", "package", "pack", "plan", "plans",
    "updated", "update", "new", "latest",
}


def partial_mentioned(words, product):
    parts = [w for w in words_of(product["name"]) if len(w) >= 4 and w not in GENERIC_WORDS]
    return any(fuzzy_hit(words, w, 1) for w in parts)


def category_mentioned(words, category):
    cat = norm(category)
    return bool(cat) and fuzzy_hit(words, "all" + cat, 2)


def payment_mentioned(words):
    if any(w in EXACT_PAYMENT_TRIGGERS for w in words):
        return True
    return any(fuzzy_hit(words, pw, 2) for pw in PAYMENT_TRIGGERS)


def infer_category(name):
    w = words_of(name)
    for c in ("ios", "android", "pc", "phone"):
        if c in w:
            return c
    if "mobile" in w:
        return "phone"
    if "windows" in w:
        return "pc"
    return ""


# ------------------- reading the catalog channel -------------------
DURATION_RE = re.compile(
    r"(?:(?<![\d,.])(\d+)\s*"
    r"(?:days?|d|weeks?|w|months?|mo|years?|y|lifetime|hours?|hrs?|mins?|minutes?)\b"
    r"|\b(?:lifetime|monthly|weekly|daily|yearly|annual|hourly)\b)",
    re.IGNORECASE,
)

PRICE_HINT_RE = re.compile(r"[$৳€£]|\b(?:tk|taka|usd|bdt|inr)\b", re.IGNORECASE)
META_RE = re.compile(r"^\s*(aliases?|category|cat)\s*[:=]\s*(.+?)\s*$", re.IGNORECASE)
NOTE_LINE_RE = re.compile(r"^\s*note\s*[:=]\s*(.*)$", re.IGNORECASE)
CUSTOM_EMOJI_RE = re.compile(r"<a?:\w+:\d+>")


def strip_emojis(text):
    text = CUSTOM_EMOJI_RE.sub("", text)
    text = re.sub(
        "["
        "\U0001F300-\U0001F5FF"
        "\U0001F600-\U0001F64F"
        "\U0001F680-\U0001F6FF"
        "\U0001F700-\U0001F77F"
        "\U0001F780-\U0001F7FF"
        "\U0001F800-\U0001F8FF"
        "\U0001F900-\U0001F9FF"
        "\U0001FA00-\U0001FAFF"
        "\U00002600-\U000026FF"
        "\U00002700-\U000027BF"
        "\U0001F1E6-\U0001F1FF"
        "]+", "", text)
    return text


def clean_name(text):
    text = strip_emojis(text)
    text = re.sub(r"[\s\-–—:|]*\bprice(?:\s*list)?\b", "", text, flags=re.IGNORECASE)
    for line in text.splitlines():
        line = re.sub(r"^[^\w]+|[^\w]+$", "", line.strip())
        if not line:
            continue
        w = words_of(line)
        if w and all(word in GENERIC_NAME_WORDS for word in w):
            continue
        return line
    return ""


def is_header(line):
    if PRICE_HINT_RE.search(line) or DURATION_RE.search(line):
        return False
    w = words_of(strip_emojis(line))
    return "payment" in w or any(x.startswith("price") for x in w)


def pull_meta(block):
    aliases, category, keep = [], "", []
    for line in block.splitlines():
        m = META_RE.match(line)
        if not m:
            keep.append(line)
            continue
        kind, value = m.group(1).lower(), m.group(2)
        if kind.startswith("alias"):
            aliases += [a.strip() for a in re.split(r"[,|;]", value) if a.strip()]
        else:
            category = norm(value)
    return "\n".join(keep).strip(), aliases, category


def is_product_block(block):
    first = block.strip().splitlines()[0] if block.strip() else ""
    if is_header(first) and any(x.startswith("price") for x in words_of(first)):
        return True
    return bool(DURATION_RE.search(block)) and bool(PRICE_HINT_RE.search(block))


def is_payment_text(text):
    return any(w in PAYMENT_MARKERS for w in words_of(text))


def parse_message(text, msg_id, has_files):
    result = {"note": None, "products": [], "payment": None}

    lines = []
    for line in text.strip().splitlines():
        m = NOTE_LINE_RE.match(line)
        if m:
            value = m.group(1).strip()
            result["note"] = "" if value.lower() == "off" else value
        else:
            lines.append(line)

    heads = []
    for i, l in enumerate(lines):
        if not is_header(l):
            continue
        start = i
        if not clean_name(l) and "payment" not in words_of(l):
            j = i - 1
            while j >= 0:
                s = lines[j].strip()
                if not s:
                    j -= 1
                    continue
                if not re.search(r"\w", strip_emojis(s)):
                    j -= 1
                    continue
                w = words_of(strip_emojis(s))
                if w and all(word in GENERIC_NAME_WORDS for word in w):
                    j -= 1
                    continue
                if (is_header(s) or META_RE.match(s)
                        or PRICE_HINT_RE.search(s) or DURATION_RE.search(s)):
                    break
                start = j
                break
        if start not in heads:
            heads.append(start)
    if heads:
        heads.append(len(lines))
        sections = ["\n".join(lines[heads[k]:heads[k + 1]]) for k in range(len(heads) - 1)]
    else:
        sections = ["\n".join(lines)]

    for section in sections:
        section, aliases, category = pull_meta(section)
        if not section:
            continue
        if is_product_block(section):
            first = section.splitlines()[0]
            d = DURATION_RE.search(section)
            name = clean_name(first) if is_header(first) else ""
            if not name:
                name = clean_name(section[:d.start()] if d else section)
            if not name:
                continue
            result["products"].append({
                "name": name,
                "key": fold(name).strip(),
                "category": category or infer_category(name),
                "aliases": aliases,
                "text": section,
                "msg_id": msg_id,
                "has_files": has_files,
            })
        elif is_payment_text(section):
            result["payment"] = {"text": section, "msg_id": msg_id, "has_files": has_files}
    return result


def full_text(msg):
    parts = [msg.content or ""]
    for e in msg.embeds:
        if e.title:
            parts.append(e.title)
        if e.description:
            parts.append(e.description)
        for f in e.fields:
            parts.append(f"{f.name}\n{f.value}")
    return "\n".join(p for p in parts if p)


catalog = {"products": {}, "payment": None, "note": "", "error": "", "seen": 0}
refresh_lock = asyncio.Lock()


async def refresh_catalog():
    async with refresh_lock:
        try:
            ch = bot.get_channel(CATALOG_CHANNEL_ID) or await bot.fetch_channel(CATALOG_CHANNEL_ID)
            products, payment, note, seen = {}, None, "", 0

            recent_titles = []

            async for msg in ch.history(limit=HISTORY_LIMIT, oldest_first=True):
                if msg.author.id == bot.user.id:
                    continue
                seen += 1
                text = full_text(msg)
                if not text.strip():
                    continue

                r = parse_message(text, msg.id, bool(msg.attachments))

                is_bare_price_list = (
                    not r["products"]
                    and not r["payment"]
                    and DURATION_RE.search(text)
                    and PRICE_HINT_RE.search(text)
                )

                if is_bare_price_list and recent_titles:
                    for (t_text, t_id, t_files) in reversed(recent_titles):
                        combined = t_text + "\n\n" + text
                        rc = parse_message(combined, t_id, t_files or bool(msg.attachments))
                        if rc["products"]:
                            r = rc
                            recent_titles = []
                            break

                if r["note"] is not None:
                    note = r["note"]
                if r["payment"]:
                    payment = r["payment"]
                for entry in r["products"]:
                    products[entry["key"]] = entry

                is_title_like = (
                    not DURATION_RE.search(text)
                    and not PRICE_HINT_RE.search(text)
                    and not is_payment_text(text)
                    and not r["products"]
                )
                if is_title_like:
                    recent_titles.append((text, msg.id, bool(msg.attachments)))
                    if len(recent_titles) > 3:
                        recent_titles.pop(0)
                elif r["products"] or r["payment"]:
                    recent_titles = []

            catalog.update(products=products, payment=payment, note=note, error="", seen=seen)
            print(f"[{BOT_VERSION}] Catalog loaded: {seen} msgs, {len(products)} products, "
                  f"payment={'yes' if payment else 'no'}")
        except Exception as e:
            catalog["error"] = f"{type(e).__name__}: {e}"
            print("Could not read catalog channel:", catalog["error"])


_pending = None


def schedule_refresh():
    global _pending
    if _pending and not _pending.done():
        _pending.cancel()

    async def later():
        await asyncio.sleep(2)
        await refresh_catalog()

    _pending = asyncio.create_task(later())


# ---------------------------- bot ----------------------------
async def health(request):
    return web.Response(text=f"ok {BOT_VERSION}")


class ShopBot(commands.Bot):
    async def setup_hook(self):
        app = web.Application()
        app.router.add_get("/", health)
        app.router.add_get("/health", health)
        runner = web.AppRunner(app)
        await runner.setup()
        await web.TCPSite(runner, "0.0.0.0", PORT).start()
        print(f"[{BOT_VERSION}] Web server listening on port", PORT)


intents = discord.Intents.default()
intents.message_content = True
bot = ShopBot(command_prefix=PREFIX, intents=intents, help_command=None)

last_reply = {}


def on_cooldown(channel_id, kind):
    now = time.time()
    key = (channel_id, kind)
    if now - last_reply.get(key, 0) < COOLDOWN:
        return True
    last_reply[key] = now
    return False


def chunk_text(text, limit=1900):
    chunks, current = [], ""
    for block in text.split("\n\n"):
        piece = block if not current else current + "\n\n" + block
        if len(piece) <= limit:
            current = piece
            continue
        if current:
            chunks.append(current)
        while len(block) > limit:
            chunks.append(block[:limit])
            block = block[limit:]
        current = block
    if current:
        chunks.append(current)
    return chunks


async def get_files(entries):
    files = []
    for e in entries:
        if not e["has_files"]:
            continue
        try:
            ch = bot.get_channel(CATALOG_CHANNEL_ID) or await bot.fetch_channel(CATALOG_CHANNEL_ID)
            msg = await ch.fetch_message(e["msg_id"])
            files += [await a.to_file() for a in msg.attachments]
        except Exception as ex:
            print("Could not fetch attachment:", ex)
    return files[:10]


async def reply_entries(message, entries, add_note=False):
    text = "\n\n".join(e["text"] for e in entries)
    if add_note and catalog["note"]:
        text += "\n\n" + catalog["note"]
    files = await get_files(entries)
    parts = chunk_text(text)
    for i, part in enumerate(parts):
        last = i == len(parts) - 1
        kwargs = {"delete_after": DELETE_AFTER}
        if last and files:
            kwargs["files"] = files
        if i == 0:
            await message.reply(part, mention_author=False, **kwargs)
        else:
            await message.channel.send(part, **kwargs)


# ------------------- stay in the voice channel 24/7 -------------------
@tasks.loop(seconds=30)
async def keep_in_voice():
    try:
        channel = bot.get_channel(VOICE_CHANNEL_ID) or await bot.fetch_channel(VOICE_CHANNEL_ID)
        vc = channel.guild.voice_client
        
        if vc is None:
            await channel.connect(reconnect=True, self_deaf=True)
            print("Joined voice channel:", channel.name)
        elif vc.channel.id != channel.id:
            await vc.move_to(channel)
            print("Moved to voice channel:", channel.name)
        elif not vc.is_connected():
            print("Voice disconnected. Waiting 10s for auto-reconnect...")
            await asyncio.sleep(10)
            if not vc.is_connected():
                print("Auto-reconnect failed. Forcing clean reconnect...")
                try:
                    await vc.disconnect(force=True)
                except Exception:
                    pass
                await asyncio.sleep(2)
                await channel.connect(reconnect=True, self_deaf=True)
                print("Re-joined voice channel after force disconnect")
    except Exception as e:
        print("Voice check problem:", e)


@bot.event
async def on_ready():
    print(f"[{BOT_VERSION}] Logged in as {bot.user}")
    await refresh_catalog()
    if not keep_in_voice.is_running():
        keep_in_voice.start()


@bot.event
async def on_raw_message_edit(payload):
    if payload.channel_id == CATALOG_CHANNEL_ID:
        schedule_refresh()


@bot.event
async def on_raw_message_delete(payload):
    if payload.channel_id == CATALOG_CHANNEL_ID:
        schedule_refresh()


@bot.event
async def on_raw_bulk_message_delete(payload):
    if payload.channel_id == CATALOG_CHANNEL_ID:
        schedule_refresh()


@bot.event
async def on_message(message):
    if message.guild is None:
        return

    # Catalog channel — keep learning, don't auto-reply
    if message.channel.id == CATALOG_CHANNEL_ID:
        if message.author.id != bot.user.id:
            schedule_refresh()
        if message.content.startswith(PREFIX) and not message.author.bot:
            await bot.process_commands(message)
        return

    if message.author.bot:
        return

    if message.content.startswith(PREFIX):
        await bot.process_commands(message)
        return

    # ← BLACKLIST: bot stays silent in these channels
    if message.channel.id in BLACKLIST_CHANNEL_IDS:
        return

    words = words_of(message.content)
    if not words:
        return

    products = list(catalog["products"].values())
    categories = sorted({p["category"] for p in products if p["category"]})

    cat_hits = [c for c in categories if category_mentioned(words, c)]
    if cat_hits:
        if not on_cooldown(message.channel.id, "category"):
            matched = [p for p in products if p["category"] in cat_hits]
            if matched:
                await reply_entries(message, matched, add_note=True)
    else:
        matched = [p for p in products if product_mentioned(words, p)]
        if not matched:
            matched = [p for p in products if partial_mentioned(words, p)]
        typed_cats = {c for c in categories if c in words}
        if typed_cats:
            matched = [p for p in matched if p["category"] in typed_cats] or matched
        if matched and not on_cooldown(message.channel.id, "price"):
            await reply_entries(message, matched, add_note=True)

    if catalog["payment"] and payment_mentioned(words) and not on_cooldown(message.channel.id, "pay"):
        await reply_entries(message, [catalog["payment"]])


# ------------------------- admin commands -------------------------
@bot.command(name="refresh")
@commands.has_permissions(administrator=True)
async def refresh_cmd(ctx):
    await refresh_catalog()
    if catalog["error"]:
        await ctx.send(f"❌ Cannot read the catalog channel.\n`{catalog['error'][:300]}`")
        return
    await ctx.send(f"✅ [{BOT_VERSION}] Read {catalog['seen']} messages: "
                   f"{len(catalog['products'])} products, "
                   f"payment {'found' if catalog['payment'] else 'NOT found'}.")


@bot.command(name="catalog")
@commands.has_permissions(administrator=True)
async def catalog_cmd(ctx):
    if catalog["error"]:
        await ctx.send(f"❌ Cannot read the catalog channel.\n`{catalog['error'][:300]}`")
        return
    lines = []
    for p in catalog["products"].values():
        extra = f", aliases: {', '.join(p['aliases'])}" if p["aliases"] else ""
        lines.append(f"• **{p['name']}** (cat: {p['category'] or '—'}, msg id: {p['msg_id']}{extra})")
    text = f"**[{BOT_VERSION}] Messages read:** {catalog['seen']}\n\n"
    text += "**Products parsed**\n" + ("\n".join(lines) or "❌ NONE — parsing failed")
    text += f"\n\n**Payment:** {'✅ found' if catalog['payment'] else '❌ not found'}"
    text += f"\n**Footer note:** {catalog['note'] or '—'}"
    for part in chunk_text(text):
        await ctx.send(part)


@bot.command(name="debug")
@commands.has_permissions(administrator=True)
async def debug_cmd(ctx):
    try:
        ch = bot.get_channel(CATALOG_CHANNEL_ID) or await bot.fetch_channel(CATALOG_CHANNEL_ID)
    except Exception as e:
        await ctx.send(f"❌ Cannot reach channel: {e}")
        return
    out = [f"**{BOT_VERSION} debug — last 6 catalog messages**\n"]
    count = 0
    async for msg in ch.history(limit=100, oldest_first=False):
        if msg.author.id == bot.user.id:
            continue
        text = full_text(msg)
        r = parse_message(text, msg.id, bool(msg.attachments))
        head = text.strip().splitlines()[0][:70] if text.strip() else "(empty)"
        out.append(
            f"`msg {msg.id}`\n"
            f"first line: `{head}`\n"
            f"duration={bool(DURATION_RE.search(text))} price={bool(PRICE_HINT_RE.search(text))} "
            f"payment_text={is_payment_text(text)}\n"
            f"→ products parsed: {[p['name'] for p in r['products']] or 'none'}\n"
        )
        count += 1
        if count >= 6:
            break
    for part in chunk_text("\n".join(out)):
        await ctx.send(part)


@bot.command(name="ping")
async def ping_cmd(ctx):
    """Reply test — works in any non-blacklisted channel."""
    blocked = ctx.channel.id in BLACKLIST_CHANNEL_IDS
    await ctx.send(f"🏓 pong — channel `<{ctx.channel.id}>` "
                   f"{'**BLOCKED** ❌' if blocked else 'active ✅'}")


@bot.event
async def on_command_error(ctx, error):
    if isinstance(error, (commands.MissingPermissions, commands.CommandNotFound)):
        return
    print(f"Error: {error}")


if not TOKEN:
    raise SystemExit("DISCORD_TOKEN is not set.")
bot.run(TOKEN)
