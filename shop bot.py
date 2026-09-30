import discord
from discord.ext import commands, tasks
import difflib
import json
import os
import re
import time

# ======================= SETTINGS =======================
# On Render the token comes from the DISCORD_TOKEN environment variable.
# On your own PC you can paste it between the quotes instead.
TOKEN = os.environ.get("DISCORD_TOKEN", "PASTE_YOUR_BOT_TOKEN_HERE")

PREFIX = "!"

# The voice channel the bot stays in 24/7
VOICE_CHANNEL_ID = 1517623589799596354

# Folder where data is saved. On Render this is the disk mounted at /data.
DATA_DIR = os.environ.get("DATA_DIR", ".")
DATA_FILE = os.path.join(DATA_DIR, "shop_data.json")
# ========================================================

DEFAULT_PAYMENT = (
    "💳 𝙋𝘼𝙔𝙈𝙀𝙉𝙏 𝙈𝙀𝙏𝙃𝙊𝘿\n\n"
    "💖 𝘽𝙆𝘼𝙎𝙃: +880 1629-835648\n"
    "💖 𝙉𝘼𝙂𝘼𝘿: +880 1629-835648\n"
    "💎 𝘽𝙄𝙉𝘼𝙉𝘾𝙀: 1220374728"
)

# Created the first time the bot starts (only if no saved data exists).
DEFAULT_PRODUCTS = {
    "fulu ios": {
        "name": "🔥 FLUORITE iOS — PRICE LIST 🔥",
        "category": "ios",
        "prices": [
            ["1 Day", "$3 / ৳400"],
            ["15 Days", "$7 / ৳1,000"],
            ["1 Month", "$15 / ৳2000"],
        ],
    },
}

# Words that trigger the payment reply (spelling mistakes are also caught automatically)
PAYMENT_WORDS = [
    "bkash", "bikash", "bkas", "nagad", "nogod", "nagod",
    "binance", "baince", "balance", "payment",
]
EXACT_PAYMENT_WORDS = ["pay"]  # too short for fuzzy matching, must be typed exactly


# ------------------------ smart matching ------------------------
def words_of(text):
    return re.findall(r"\w+", text.lower())[:80]


def norm(text):
    return "".join(re.findall(r"\w+", text.lower()))


def threshold(length):
    if length <= 3:
        return 1.0
    if length <= 5:
        return 0.85
    return 0.8


def fuzzy_hit(words, target, max_size):
    """True if any group of up to max_size words (joined with no spaces)
    is the same as, or very close to, the target text."""
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


def product_mentioned(words, product_key):
    target = norm(product_key)
    if not target:
        return False
    max_size = min(len(product_key.split()) + 1, 4)
    return fuzzy_hit(words, target, max_size)


def category_mentioned(words, category):
    """Matches 'all ios', 'allios', 'all andriod', 'all pc' ..."""
    cat = norm(category)
    if not cat:
        return False
    return fuzzy_hit(words, "all" + cat, 2)


def payment_mentioned(words):
    for w in words:
        if w in EXACT_PAYMENT_WORDS:
            return True
    for pw in PAYMENT_WORDS:
        if fuzzy_hit(words, pw, 2):
            return True
    return False


def infer_category(name):
    w = words_of(name)
    for c in ("ios", "android", "pc"):
        if c in w:
            return c
    if "windows" in w:
        return "pc"
    return ""


# ---------------------------- data ----------------------------
def load_data():
    if os.path.exists(DATA_FILE):
        try:
            with open(DATA_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            print("Could not read data file, starting fresh:", e)
    return {"products": json.loads(json.dumps(DEFAULT_PRODUCTS)), "payment": DEFAULT_PAYMENT}


def save_data():
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


data = load_data()
# Older saved data may not have categories yet
for _p in data["products"].values():
    if "category" not in _p:
        _p["category"] = infer_category(_p["name"])
save_data()


def all_categories():
    return sorted({p.get("category", "") for p in data["products"].values() if p.get("category")})


# ---------------------------- bot ----------------------------
intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix=PREFIX, intents=intents, help_command=None)

last_reply = {}
COOLDOWN = 5  # seconds between auto-replies of the same kind in one channel


def on_cooldown(channel_id, kind):
    now = time.time()
    key = (channel_id, kind)
    if now - last_reply.get(key, 0) < COOLDOWN:
        return True
    last_reply[key] = now
    return False


def build_price_text(product):
    lines = [f"🔥{product['name']} — PRICE LIST 🔥", ""]
    if not product["prices"]:
        lines.append("No prices added yet.")
    for duration, price in product["prices"]:
        lines.append(f"📅 {duration} — 💵 {price}")
    return "\n".join(lines)


def find_product(name):
    return data["products"].get(name.strip().lower())


def split_parts(text):
    return [p.strip() for p in text.split("|")]


def chunk_text(text, limit=1900):
    """Splits long text into pieces under Discord's 2000 character limit."""
    chunks, current = [], ""
    for block in text.split("\n\n"):
        piece = block if not current else current + "\n\n" + block
        if len(piece) <= limit:
            current = piece
        else:
            if current:
                chunks.append(current)
            current = block[:limit]
    if current:
        chunks.append(current)
    return chunks


async def reply_long(message, text):
    first = True
    for part in chunk_text(text):
        if first:
            await message.reply(part, mention_author=False)
            first = False
        else:
            await message.channel.send(part)


async def send_long(ctx, text):
    for part in chunk_text(text):
        await ctx.send(part)


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
    if not keep_in_voice.is_running():
        keep_in_voice.start()


@bot.event
async def on_message(message):
    if message.author.bot or message.guild is None:
        return

    # Commands start with the prefix, let the command system handle them
    if message.content.startswith(PREFIX):
        await bot.process_commands(message)
        return

    words = words_of(message.content)
    if not words:
        return

    # "all ios" / "all android" / "all pc" -> show every product in that category
    cat_hits = [c for c in all_categories() if category_mentioned(words, c)]
    if cat_hits:
        if not on_cooldown(message.channel.id, "category"):
            blocks = []
            for p in data["products"].values():
                if p.get("category") in cat_hits:
                    blocks.append(build_price_text(p))
            if blocks:
                await reply_long(message, "\n\n".join(blocks))
    else:
        # Single product name (any case, any spacing, small spelling mistakes)
        matched = [p for key, p in data["products"].items() if product_mentioned(words, key)]
        if matched and not on_cooldown(message.channel.id, "price"):
            await reply_long(message, "\n\n".join(build_price_text(p) for p in matched))

    # Payment words
    if payment_mentioned(words) and not on_cooldown(message.channel.id, "pay"):
        await message.reply(data["payment"][:1990], mention_author=False)


# ------------------------- admin-only commands -------------------------
@bot.command(name="addproduct")
@commands.has_permissions(administrator=True)
async def addproduct(ctx, *, args: str):
    """!addproduct angry mod | android   (category is optional)"""
    parts = split_parts(args)
    name = parts[0]
    if not name:
        await ctx.send(f"❌ Use it like this:\n`{PREFIX}addproduct angry mod | android`")
        return
    category = norm(parts[1]) if len(parts) > 1 else infer_category(name)
    key = name.lower()
    if key in data["products"]:
        await ctx.send("❌ That product already exists.")
        return
    data["products"][key] = {"name": name, "category": category, "prices": []}
    save_data()
    await ctx.send(
        f"✅ Product **{name}** added (category: `{category or 'none'}`).\n"
        f"Now add prices like:\n`{PREFIX}addprice {name} | 1 Day | $3 / ৳400`"
    )


@bot.command(name="setcategory")
@commands.has_permissions(administrator=True)
async def setcategory(ctx, *, args: str):
    """!setcategory angry mod | android"""
    parts = split_parts(args)
    if len(parts) != 2 or not all(parts):
        await ctx.send(f"❌ Use it like this:\n`{PREFIX}setcategory angry mod | android`")
        return
    product = find_product(parts[0])
    if not product:
        await ctx.send("❌ Product not found.")
        return
    product["category"] = norm(parts[1])
    save_data()
    await ctx.send(f"✅ **{product['name']}** is now in category `{product['category']}`.\n"
                   f"Anyone typing `all {product['category']}` will see it.")


@bot.command(name="removeproduct")
@commands.has_permissions(administrator=True)
async def removeproduct(ctx, *, name: str):
    key = name.strip().lower()
    if key not in data["products"]:
        await ctx.send("❌ Product not found.")
        return
    del data["products"][key]
    save_data()
    await ctx.send(f"🗑️ Product **{name.strip()}** removed.")


@bot.command(name="addprice")
@commands.has_permissions(administrator=True)
async def addprice(ctx, *, args: str):
    parts = split_parts(args)
    if len(parts) != 3 or not all(parts):
        await ctx.send(f"❌ Use it like this:\n`{PREFIX}addprice fulu iOS | 1 Day | $3 / ৳400`")
        return
    product = find_product(parts[0])
    if not product:
        await ctx.send(f"❌ Product not found. Create it first with `{PREFIX}addproduct {parts[0]}`")
        return
    duration, price = parts[1], parts[2]
    for row in product["prices"]:
        if row[0].lower() == duration.lower():
            row[1] = price
            save_data()
            await ctx.send("✅ Price updated.\n\n" + build_price_text(product))
            return
    product["prices"].append([duration, price])
    save_data()
    await ctx.send("✅ Price added.\n\n" + build_price_text(product))


@bot.command(name="removeprice")
@commands.has_permissions(administrator=True)
async def removeprice(ctx, *, args: str):
    parts = split_parts(args)
    if len(parts) != 2 or not all(parts):
        await ctx.send(f"❌ Use it like this:\n`{PREFIX}removeprice fulu iOS | 1 Day`")
        return
    product = find_product(parts[0])
    if not product:
        await ctx.send("❌ Product not found.")
        return
    before = len(product["prices"])
    product["prices"] = [r for r in product["prices"] if r[0].lower() != parts[1].lower()]
    if len(product["prices"]) == before:
        await ctx.send("❌ That duration was not found.")
        return
    save_data()
    await ctx.send("🗑️ Price removed.\n\n" + build_price_text(product))


@bot.command(name="products")
@commands.has_permissions(administrator=True)
async def products(ctx):
    if not data["products"]:
        await ctx.send(f"No products yet. Add one with `{PREFIX}addproduct name | category`")
        return
    lines = [f"• {p['name']}  (category: {p.get('category') or 'none'})" for p in data["products"].values()]
    await send_long(ctx, "**Products**\n" + "\n".join(lines))


@bot.command(name="setpayment")
@commands.has_permissions(administrator=True)
async def setpayment(ctx, *, text: str):
    data["payment"] = text
    save_data()
    await ctx.send("✅ Payment message updated:\n\n" + text[:1900])


@bot.command(name="shophelp")
@commands.has_permissions(administrator=True)
async def shophelp(ctx):
    await ctx.send(
        "**Admin commands**\n"
        f"`{PREFIX}addproduct angry mod | android`\n"
        f"`{PREFIX}setcategory angry mod | android`\n"
        f"`{PREFIX}removeproduct angry mod`\n"
        f"`{PREFIX}addprice angry mod | 1 Day | $3 / ৳400`\n"
        f"`{PREFIX}removeprice angry mod | 1 Day`\n"
        f"`{PREFIX}products`\n"
        f"`{PREFIX}setpayment your payment text`"
    )


@bot.event
async def on_command_error(ctx, error):
    if isinstance(error, (commands.MissingPermissions, commands.CommandNotFound)):
        return  # non-admins get no reply
    if isinstance(error, commands.MissingRequiredArgument):
        await ctx.send(f"❌ Missing details. Type `{PREFIX}shophelp` to see how to use it.")
        return
    print(f"Error: {error}")


bot.run(TOKEN)
