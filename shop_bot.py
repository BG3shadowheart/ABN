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
# The title "🔥 ... — PRICE LIST 🔥" is added automatically, so "name" is just the product name.
# "aliases" are extra names people can type to get this product.
DEFAULT_PRODUCTS = {
    "fluorite ios": {
        "name": "FLUORITE iOS",
        "category": "ios",
        "aliases": ["fulu ios"],
        "prices": [
            ["1 Day", "$3 / ৳400"],
            ["15 Days", "$7 / ৳1,000"],
            ["1 Month", "$15 / ৳2,000"],
        ],
    },
}

# Shown once at the bottom of every price reply. Change it with:  !setnote your text   (or  !setnote off)
DEFAULT_NOTE = "🏷️ Use our server tag ABN and get up to 10% discount!"

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


def name_mentioned(words, name):
    target = norm(name)
    if not target:
        return False
    max_size = min(len(name.split()) + 1, 4)
    return fuzzy_hit(words, target, max_size)


def product_mentioned(words, key, product):
    names = [key] + product.get("aliases", [])
    return any(name_mentioned(words, n) for n in names)


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


# ------------------ one-line price parsing ------------------
# Understands text like:  1 day 3$ 400 3 days 5$ 500 30 days 15$ 1500
DURATION_RE = re.compile(
    r"(?<![\d,.])(\d+)\s*(days?|d|weeks?|w|months?|mo|years?|y)\b", re.IGNORECASE
)
NUMBER_RE = re.compile(r"\d[\d,]*(?:\.\d+)?")
UNIT_NAMES = {"d": "Day", "w": "Week", "m": "Month", "y": "Year"}


def fmt_number(text):
    value = float(text.replace(",", ""))
    if value == int(value):
        return f"{int(value):,}"
    return f"{value:,.2f}"


def fmt_duration(count, unit):
    name = UNIT_NAMES[unit.lower()[0]]
    n = int(count)
    return f"{n} {name}{'' if n == 1 else 's'}"


def parse_prices(body):
    """Returns (list_of_prices, error_text). Each price is [duration, price_text]."""
    matches = list(DURATION_RE.finditer(body))
    prices = []
    for i, m in enumerate(matches):
        end = matches[i + 1].start() if i + 1 < len(matches) else len(body)
        segment = body[m.end():end]
        duration = fmt_duration(m.group(1), m.group(2))
        nums = NUMBER_RE.findall(segment)[:2]
        if len(nums) == 2:
            usd, bdt = nums
            if float(usd.replace(",", "")) > float(bdt.replace(",", "")):
                usd, bdt = bdt, usd
            text = f"${fmt_number(usd)} / ৳{fmt_number(bdt)}"
        elif len(nums) == 1:
            if "$" in segment:
                text = f"${fmt_number(nums[0])}"
            elif "৳" in segment or "tk" in segment.lower():
                text = f"৳{fmt_number(nums[0])}"
            else:
                return None, f"I could not tell if the price for **{duration}** is $ or ৳."
        else:
            return None, f"I need two prices ($ and ৳) after **{duration}**."
        prices.append([duration, text])
    return prices, None


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
# Older saved data may be missing some fields
for _p in data["products"].values():
    if "category" not in _p:
        _p["category"] = infer_category(_p["name"])
    if "aliases" not in _p:
        _p["aliases"] = []
if "note" not in data:
    data["note"] = DEFAULT_NOTE
save_data()


def all_categories():
    return sorted({p.get("category", "") for p in data["products"].values() if p.get("category")})


def find_key(name):
    """Finds a product by its name or by one of its aliases."""
    n = name.strip().lower()
    if n in data["products"]:
        return n
    for key, p in data["products"].items():
        if n in [a.lower() for a in p.get("aliases", [])]:
            return key
    return None


def find_product(name):
    key = find_key(name)
    return data["products"][key] if key else None


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


def split_parts(text):
    return [p.strip() for p in text.split("|")]


def clean_name(text):
    """Turns '🔥 FLUORITE iOS — PRICE LIST 🔥' into 'FLUORITE iOS'."""
    text = re.sub(r"price\s*list", "", text, flags=re.IGNORECASE)
    for line in text.splitlines():
        line = re.sub(r"^[^\w]+|[^\w]+$", "", line.strip())
        if line:
            return line
    return ""


def add_note(text):
    note = data.get("note", "")
    return text + "\n\n" + note if note else text


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
            blocks = [build_price_text(p) for p in data["products"].values()
                      if p.get("category") in cat_hits]
            if blocks:
                await reply_long(message, add_note("\n\n".join(blocks)))
    else:
        # Single product name (any case, any spacing, small spelling mistakes)
        matched = [p for key, p in data["products"].items() if product_mentioned(words, key, p)]
        if matched and not on_cooldown(message.channel.id, "price"):
            await reply_long(message, add_note("\n\n".join(build_price_text(p) for p in matched)))

    # Payment words
    if payment_mentioned(words) and not on_cooldown(message.channel.id, "pay"):
        await message.reply(data["payment"][:1990], mention_author=False)


# ------------------------- admin-only commands -------------------------
@bot.command(name="addproduct")
@commands.has_permissions(administrator=True)
async def addproduct(ctx, *, args: str):
    """
    Any of these work (you can even paste a finished price list with emojis):
    !addproduct Angry Mod | android | 1 day 3$ 400 3 days 5$ 500 30 days 15$ 1500
    !addproduct Angry Mod 1 day 3$ 400 3 days 5$ 500 30 days 15$ 1500
    If the product already exists, its price list is replaced.
    """
    m = DURATION_RE.search(args)
    head = args[:m.start()] if m else args
    body = args[m.start():] if m else ""

    head_parts = [p for p in split_parts(head)]
    name = clean_name(head_parts[0]) if head_parts else ""
    if not name:
        await ctx.send(f"❌ Use it like this:\n`{PREFIX}addproduct Angry Mod | android | 1 day 3$ 400 3 days 5$ 500`")
        return
    category = norm(head_parts[1]) if len(head_parts) > 1 and head_parts[1] else infer_category(name)

    prices, error = parse_prices(body) if body else ([], None)
    if error:
        await ctx.send(f"❌ {error}\nExample: `1 day 3$ 400 3 days 5$ 500`")
        return

    key = find_key(name)
    if key:
        product = data["products"][key]
        if body:
            product["prices"] = prices
        if len(head_parts) > 1 and head_parts[1]:
            product["category"] = category
        action = "updated"
    else:
        product = {"name": name, "category": category, "aliases": [], "prices": prices}
        data["products"][name.lower()] = product
        action = "added"
    save_data()

    extra = ""
    if not product["prices"]:
        extra = f"\nNow add prices, for example:\n`{PREFIX}addproduct {name} 1 day 3$ 400 3 days 5$ 500`"
    await ctx.send(f"✅ Product {action} (category: `{product['category'] or 'none'}`)\n\n"
                   + build_price_text(product) + extra)


@bot.command(name="setcategory")
@commands.has_permissions(administrator=True)
async def setcategory(ctx, *, args: str):
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


@bot.command(name="addalias")
@commands.has_permissions(administrator=True)
async def addalias(ctx, *, args: str):
    """!addalias FLUORITE iOS | fulu ios"""
    parts = split_parts(args)
    if len(parts) != 2 or not all(parts):
        await ctx.send(f"❌ Use it like this:\n`{PREFIX}addalias FLUORITE iOS | fulu ios`")
        return
    product = find_product(parts[0])
    if not product:
        await ctx.send("❌ Product not found.")
        return
    if parts[1].lower() not in [a.lower() for a in product["aliases"]]:
        product["aliases"].append(parts[1])
        save_data()
    await ctx.send(f"✅ People can now also type **{parts[1]}** to get **{product['name']}**.")


@bot.command(name="removealias")
@commands.has_permissions(administrator=True)
async def removealias(ctx, *, args: str):
    """!removealias FLUORITE iOS | fulu ios"""
    parts = split_parts(args)
    if len(parts) != 2 or not all(parts):
        await ctx.send(f"❌ Use it like this:\n`{PREFIX}removealias FLUORITE iOS | fulu ios`")
        return
    product = find_product(parts[0])
    if not product:
        await ctx.send("❌ Product not found.")
        return
    before = len(product["aliases"])
    product["aliases"] = [a for a in product["aliases"] if a.lower() != parts[1].lower()]
    if len(product["aliases"]) == before:
        await ctx.send("❌ That alias was not found.")
        return
    save_data()
    await ctx.send("🗑️ Alias removed.")


@bot.command(name="removeproduct")
@commands.has_permissions(administrator=True)
async def removeproduct(ctx, *, name: str):
    key = find_key(name)
    if not key:
        await ctx.send("❌ Product not found.")
        return
    removed = data["products"].pop(key)
    save_data()
    await ctx.send(f"🗑️ Product **{removed['name']}** removed.")


@bot.command(name="addprice")
@commands.has_permissions(administrator=True)
async def addprice(ctx, *, args: str):
    """!addprice fulu iOS | 1 Day | $3 / ৳400   (adds or updates ONE price)"""
    parts = split_parts(args)
    if len(parts) != 3 or not all(parts):
        await ctx.send(f"❌ Use it like this:\n`{PREFIX}addprice FLUORITE iOS | 1 Day | $3 / ৳400`")
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
        await ctx.send(f"❌ Use it like this:\n`{PREFIX}removeprice FLUORITE iOS | 1 Day`")
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
        await ctx.send(f"No products yet. Add one with `{PREFIX}addproduct name | category | prices`")
        return
    lines = []
    for p in data["products"].values():
        alias_text = f", also: {', '.join(p['aliases'])}" if p.get("aliases") else ""
        lines.append(f"• {p['name']}  (category: {p.get('category') or 'none'}{alias_text})")
    await send_long(ctx, "**Products**\n" + "\n".join(lines))


@bot.command(name="setnote")
@commands.has_permissions(administrator=True)
async def setnote(ctx, *, text: str):
    """!setnote your text   or   !setnote off"""
    if text.strip().lower() == "off":
        data["note"] = ""
        save_data()
        await ctx.send("✅ Note removed from price replies.")
        return
    data["note"] = text.strip()
    save_data()
    await ctx.send("✅ Note updated:\n\n" + data["note"][:1900])


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
        f"`{PREFIX}addproduct Angry Mod | android | 1 day 3$ 400 3 days 5$ 500 30 days 15$ 1500`\n"
        f"`{PREFIX}addprice Angry Mod | 7 Days | $5 / ৳600`\n"
        f"`{PREFIX}removeprice Angry Mod | 7 Days`\n"
        f"`{PREFIX}removeproduct Angry Mod`\n"
        f"`{PREFIX}setcategory Angry Mod | android`\n"
        f"`{PREFIX}addalias FLUORITE iOS | fulu ios`\n"
        f"`{PREFIX}removealias FLUORITE iOS | fulu ios`\n"
        f"`{PREFIX}products`\n"
        f"`{PREFIX}setnote your discount text`  (or `{PREFIX}setnote off`)\n"
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
