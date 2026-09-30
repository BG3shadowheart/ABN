"""
Channel-powered shop bot (free Render friendly).

You post price lists / payment methods in ONE private channel.
The bot reads that channel, learns everything, and copy-pastes your exact
message when a customer types a product name (even misspelled) or a payment word.
No saved files needed: the channel IS the database, so Render's free
(ephemeral) disk is not a problem.
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
PORT = int(os.environ.get("PORT", "10000"))  # Render gives this automatically
PREFIX = "!"
HISTORY_LIMIT = 1000   # how many recent catalog-channel messages the bot reads
COOLDOWN = 5           # seconds between auto-replies of the same kind per channel
# ========================================================

# Words a customer can type to get the payment message (typos are caught too)
PAYMENT_TRIGGERS = [
    "bkash", "bikash", "bkas", "nagad", "nogod", "nagod", "rocket", "upay",
    "binance", "baince", "balance", "payment", "paypal",
]
EXACT_PAYMENT_TRIGGERS = ["pay"]  # too short for fuzzy matching

# Words that make a catalog-channel message count as "the payment message"
PAYMENT_MARKERS = {
    "bkash", "bikash", "nagad", "nogod", "rocket", "upay", "binance",
    "payment", "paypal", "usdt", "crypto",
}


# ------------------------ text helpers ------------------------
def fold(text):
    """Lowercase + turns fancy fonts (𝘽𝙆𝘼𝙎𝙃) into normal letters."""
    return unicodedata.normalize("NFKC", text).lower()


def words_of(text):
    return re.findall(r"\w+", fold(text))[:80]


def norm(text):
    return "".join(re.findall(r"\w+", fold(text)))


def threshold(length):
    if length <= 3:
        return 1.0
    if length <= 5:
        return 0.85
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


def product_mentioned(words, product):
    names = [product["key"]] + product["aliases"]
    return any(name_mentioned(words, n) for n in names)


def category_mentioned(words, category):
    cat = norm(category)
    return bool(cat) and fuzzy_hit(words, "all" + cat, 2)


def payment_mentioned(words):
    if any(w in EXACT_PAYMENT_TRIGGERS for w in words):
        return True
    return any(fuzzy_hit(words, pw, 2) for pw in PAYMENT_TRIGGERS)


def infer_category(name):
    w = words_of(name)
    for c in ("ios", "android", "pc"):
        if c in w:
            return c
    if "windows" in w:
        return "pc"
    return ""


# ------------------- reading the catalog channel -------------------
DURATION_RE = re.compile(
    r"(?<![\d,.])(\d+)\s*(days?|d|weeks?|w|months?|mo|years?|y|lifetime)\b", re.IGNORECASE
)
PRICE_HINT_RE = re.compile(r"[$৳]|\b(?:tk|taka|usd|bdt)\b", re.IGNORECASE)
META_RE = re.compile(r"^\s*(aliases?|category|cat)\s*[:=]\s*(.+?)\s*$", re.IGNORECASE)
NOTE_RE = re.compile(r"^\s*note\s*[:=]\s*(.*)$", re.IGNORECASE | re.DOTALL)


def clean_name(text):
    text = re.sub(r"price\s*list", "", text, flags=re.IGNORECASE)
    for line in text.splitlines():
        line = re.sub(r"^[^\w]+|[^\w]+$", "", line.strip())
        if line:
            return line
    return ""


def split_blocks(text):
    """If one message holds several 'PRICE LIST' headers, split it per product."""
    lines = text.splitlines()
    starts = [i for i, l in enumerate(lines) if "pricelist" in norm(l)]
    if len(starts) < 2:
        return [text]
    starts.append(len(lines))
    return ["\n".join(lines[starts[k]:starts[k + 1]]) for k in range(len(starts) - 1)]


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
    if "pricelist" in norm(block):
        return True
    return bool(DURATION_RE.search(block)) and bool(PRICE_HINT_RE.search(block))


def is_payment_text(text):
    return any(w in PAYMENT_MARKERS for w in words_of(text))


def parse_message(text, msg_id, has_files):
    """Returns ('note', str) | ('payment', entry) | ('products', [entries]) | None"""
    text = text.strip()
    if not text:
        return None

    m = NOTE_RE.match(text)
    if m:
        value = m.group(1).strip()
        return "note", "" if value.lower() == "off" else value

    entries = []
    for block in split_blocks(text):
        block, aliases, category = pull_meta(block)
        if not block or not is_product_block(block):
            continue
        d = DURATION_RE.search(block)
        name = clean_name(block[:d.start()] if d else block)
        if not name:
            continue
        entries.append({
            "name": name,
            "key": fold(name).strip(),
            "category": category or infer_category(name),
            "aliases": aliases,
            "text": block,
            "msg_id": msg_id,
            "has_files": has_files,
        })
    if entries:
        return "products", entries

    if is_payment_text(text):
        return "payment", {"text": text, "msg_id": msg_id, "has_files": has_files}
    return None


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


catalog = {"products": {}, "payment": None, "note": ""}
refresh_lock = asyncio.Lock()


async def refresh_catalog():
    """Re-reads the whole catalog channel. Newest message wins for the same product."""
    async with refresh_lock:
        try:
            ch = bot.get_channel(CATALOG_CHANNEL_ID) or await bot.fetch_channel(CATALOG_CHANNEL_ID)
            products, payment, note = {}, None, ""
            async for msg in ch.history(limit=HISTORY_LIMIT, oldest_first=True):
                if msg.author.id == bot.user.id:
                    continue
                result = parse_message(full_text(msg), msg.id, bool(msg.attachments))
                if not result:
                    continue
                kind, value = result
                if kind == "note":
                    note = value
                elif kind == "payment":
                    payment = value
                else:
                    for entry in value:
                        products[entry["key"]] = entry
            catalog["products"], catalog["payment"], catalog["note"] = products, payment, note
            print(f"Catalog loaded: {len(products)} products, payment={'yes' if payment else 'no'}")
        except Exception as e:
            print("Could not read catalog channel (check bot permissions):", e)


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
    return web.Response(text="ok")


class ShopBot(commands.Bot):
    async def setup_hook(self):
        # Tiny web server so Render sees an open port (and UptimeRobot can ping it)
        app = web.Application()
        app.router.add_get("/", health)
        app.router.add_get("/health", health)
        runner = web.AppRunner(app)
        await runner.setup()
        await web.TCPSite(runner, "0.0.0.0", PORT).start()
        print("Web server listening on port", PORT)


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
    """Downloads fresh copies of images the owner attached (Discord links expire)."""
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
        kwargs = {"files": files} if (last and files) else {}
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
        elif not vc.is_connected():
            await vc.disconnect(force=True)
            await channel.connect(reconnect=True, self_deaf=True)
            print("Re-joined voice channel:", channel.name)
        elif vc.channel.id != channel.id:
            await vc.move_to(channel)
    except Exception as e:
        print("Voice check problem:", e)


@bot.event
async def on_ready():
    print(f"Logged in as {bot.user}")
    await refresh_catalog()
    if not keep_in_voice.is_running():
        keep_in_voice.start()


# ---- catalog channel changes -> re-learn (new post, edit, delete) ----
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
        if matched and not on_cooldown(message.channel.id, "price"):
            await reply_entries(message, matched, add_note=True)

    if catalog["payment"] and payment_mentioned(words) and not on_cooldown(message.channel.id, "pay"):
        await reply_entries(message, [catalog["payment"]])


# ------------------------- admin commands -------------------------
@bot.command(name="refresh")
@commands.has_permissions(administrator=True)
async def refresh_cmd(ctx):
    await refresh_catalog()
    await ctx.send(f"✅ Reloaded: {len(catalog['products'])} products, "
                   f"payment {'found' if catalog['payment'] else 'NOT found'}.")


@bot.command(name="catalog")
@commands.has_permissions(administrator=True)
async def catalog_cmd(ctx):
    lines = []
    for p in catalog["products"].values():
        extra = f", also: {', '.join(p['aliases'])}" if p["aliases"] else ""
        lines.append(f"• {p['name']} (category: {p['category'] or 'none'}{extra})")
    text = "**Products**\n" + ("\n".join(lines) or "none found")
    text += f"\n\n**Payment message:** {'found' if catalog['payment'] else 'NOT found'}"
    text += f"\n**Footer note:** {catalog['note'] or 'none'}"
    for part in chunk_text(text):
        await ctx.send(part)


@bot.event
async def on_command_error(ctx, error):
    if isinstance(error, (commands.MissingPermissions, commands.CommandNotFound)):
        return
    print(f"Error: {error}")


if not TOKEN:
    raise SystemExit("DISCORD_TOKEN is not set.")
bot.run(TOKEN)
