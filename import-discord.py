import discord
from discord.ext import commands
from discord.ui import Button, View
from datetime import datetime, timedelta
import json
import os
from dotenv import load_dotenv
import re
from functools import lru_cache

# Load environment variables from .env file
load_dotenv()

intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)

# Optimization: Compiled date patterns for faster parsing
DATE_PATTERNS = [
    (re.compile(r'^(\d{1,2})/(\d{1,2})/(\d{4})$'), "%d/%m/%Y"),
    (re.compile(r'^(\d{1,2})-(\d{1,2})-(\d{4})$'), "%d-%m-%Y"),
]

# Stats cache: server_key -> (cached_stats, records_hash)
stats_cache = {}

def _hash_records(records):
    """Quick hash of records for cache invalidation."""
    return hash(tuple(sorted((str(k), tuple(sorted(v["active"])), tuple(sorted(v["crossed"]))) for k, v in records.items())))


# --- NEW PAGINATION CLASS ---
class PaginatorView(View):
    def __init__(self, lines, title, footer_base, per_page=20):
        super().__init__(timeout=120)
        self.lines = lines
        self.title = title
        self.footer_base = footer_base
        self.per_page = per_page
        self.current_page = 0

    @property
    def total_pages(self):
        return max(1, (len(self.lines) + self.per_page - 1) // self.per_page)

    def get_page_text(self, page_index):
        start = page_index * self.per_page
        end = start + self.per_page
        return "\n".join(self.lines[start:end])

    async def update_embed(self, interaction):
        embed = discord.Embed(
            title=f"{self.title} (Page {self.current_page + 1}/{self.total_pages})",
            description=self.get_page_text(self.current_page),
            color=0x5865F2
        )
        embed.set_footer(text=self.footer_base)
        await interaction.response.edit_message(embed=embed, view=self)

    @discord.ui.button(label="◀", style=discord.ButtonStyle.gray)
    async def prev_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.current_page > 0:
            self.current_page -= 1
            await self.update_embed(interaction)

    @discord.ui.button(label="▶", style=discord.ButtonStyle.gray)
    async def next_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.current_page < self.total_pages - 1:
            self.current_page += 1
            await self.update_embed(interaction)


async def send_paginated_embed(ctx, title, lines, color, footer_base, per_page=20):
    """Send lines as a paginated embed when needed."""
    if not lines:
        await ctx.send("No entries to display.")
        return

    total_pages = max(1, (len(lines) + per_page - 1) // per_page)
    first_page = "\n".join(lines[:per_page])
    embed = discord.Embed(
        title=f"{title} (Page 1/{total_pages})",
        description=first_page,
        color=color,
    )
    embed.set_footer(text=footer_base)

    if total_pages == 1:
        await ctx.send(embed=embed)
        return

    view = PaginatorView(lines, title, footer_base, per_page=per_page)
    await ctx.send(embed=embed, view=view)

# Storage shape per server: {date_obj: {"active": set[str], "crossed": set[str]}}
server_settings = {}

# File persistence
RECORDS_DIR = "signup_records"
SETTINGS_FILE = os.path.join(RECORDS_DIR, "server_settings.json")


def get_server_key(guild_id, author_id=None):
    """Return a stable key for server-specific storage."""
    if guild_id is not None:
        return str(guild_id)
    return f"dm_{author_id}"


def get_records_file(server_key):
    """Return JSON storage path for a specific server key."""
    os.makedirs(RECORDS_DIR, exist_ok=True)
    return os.path.join(RECORDS_DIR, f"signup_records_{server_key}.json")


def load_settings():
    """Load per-server settings from disk."""
    global server_settings
    if server_settings:
        return server_settings

    os.makedirs(RECORDS_DIR, exist_ok=True)
    if os.path.exists(SETTINGS_FILE):
        try:
            with open(SETTINGS_FILE, "r") as f:
                data = json.load(f)
                if isinstance(data, dict):
                    server_settings = data
        except Exception as e:
            print(f"Error loading settings: {e}")

    return server_settings


def save_settings():
    """Save per-server settings to disk."""
    try:
        os.makedirs(RECORDS_DIR, exist_ok=True)
        with open(SETTINGS_FILE, "w") as f:
            json.dump(server_settings, f, indent=2)
    except Exception as e:
        print(f"Error saving settings: {e}")


def get_management_role_name(server_key):
    """Return configured management role name for a server key."""
    settings = load_settings()
    value = settings.get(server_key, {})
    if isinstance(value, dict):
        return value.get("management_role")
    return None


def set_management_role_name(server_key, role_name):
    """Set configured management role name for a server key."""
    load_settings()
    server_settings.setdefault(server_key, {})
    server_settings[server_key]["management_role"] = role_name
    save_settings()


def has_management_access():
    """Command check for configured management role with admin fallback."""
    async def predicate(ctx):
        if ctx.guild is None:
            raise commands.NoPrivateMessage()

        # Always allow server administrators.
        if ctx.author.guild_permissions.administrator:
            return True

        server_key = get_server_key(ctx.guild.id, ctx.author.id)
        role_name = get_management_role_name(server_key)
        if not role_name:
            return False

        return any(role.name.casefold() == role_name.casefold() for role in ctx.author.roles)

    return commands.check(predicate)


def load_records(server_key):
    """Load records for one server key from its JSON file."""
    records = {}
    records_file = get_records_file(server_key)
    if os.path.exists(records_file):
        try:
            with open(records_file, "r") as f:
                data = json.load(f)
                # Convert date strings back to date objects and sets
                for date_str, record in data.items():
                    date_obj = datetime.strptime(date_str, "%Y-%m-%d").date()
                    records[date_obj] = {
                        "active": set(record.get("active", [])),
                        "crossed": set(record.get("crossed", []))
                    }
        except Exception as e:
            print(f"Error loading records for {server_key}: {e}")

    return records


def save_records(server_key, records):
    """Save records for one server key to its JSON file."""
    try:
        data = {}
        for date_obj, record in records.items():
            date_str = date_obj.isoformat()
            data[date_str] = {
                "active": sorted(record["active"]),
                "crossed": sorted(record["crossed"])
            }
        records_file = get_records_file(server_key)
        # Use compact JSON (no indent) to reduce disk I/O and local storage
        with open(records_file, "w") as f:
            json.dump(data, f)
        # Invalidate stats cache for this server
        if server_key in stats_cache:
            del stats_cache[server_key]
    except Exception as e:
        print(f"Error saving records for {server_key}: {e}")


def format_json_error(exc):
    """Return user-friendly JSON parsing error messages."""
    return f"Error processing JSON: {str(exc)}"

@bot.event
async def on_ready():
    print(f"{bot.user} has connected to Discord!")
    print("JSON reader initialized and ready.")

@bot.event
async def on_message(message):
    if message.author == bot.user:
        return

    await bot.process_commands(message)

def parse_date(date_str):
    """Parse supported date formats using day-month-year ordering."""
    # Try regex patterns first (faster than strptime for simple formats)
    for pattern, fmt in DATE_PATTERNS:
        if pattern.match(date_str):
            try:
                return datetime.strptime(date_str, fmt).date()
            except ValueError:
                pass
    
    # Fallback to remaining formats
    formats = [
        "%d %m %Y",
        "%d %B %Y",
        "%d %b %Y",
        "%Y-%m-%d",
    ]
    for fmt in formats:
        try:
            return datetime.strptime(date_str, fmt).date()
        except ValueError:
            continue
    raise ValueError("Invalid date format")


def _find_first_date(value, depth=0, max_depth=20):
    """Recursively find the first `date` field in a JSON object/list.
    Limit recursion depth to prevent stack overflow on malformed JSON.
    """
    if depth > max_depth:
        return None
    
    if isinstance(value, dict):
        for key, val in value.items():
            if str(key).lower() == "date" and isinstance(val, str):
                return val
        for val in value.values():
            found = _find_first_date(val, depth + 1, max_depth)
            if found:
                return found
    elif isinstance(value, list):
        for item in value:
            found = _find_first_date(item, depth + 1, max_depth)
            if found:
                return found
    return None


def _collect_signup_entries(value, entries, depth=0, max_depth=20):
    """Collect objects that look like signup rows with name/status/classname keys.
    Limit recursion depth to prevent stack overflow.
    """
    if depth > max_depth:
        return
    
    if isinstance(value, dict):
        # Fast-path: check raw keys first before lowering
        keys_lower = {k.lower() if isinstance(k, str) else str(k).lower() for k in value.keys()}
        if "name" in keys_lower and "status" in keys_lower:
            entries.append({str(k).lower(): v for k, v in value.items()})
        for val in value.values():
            _collect_signup_entries(val, entries, depth + 1, max_depth)
    elif isinstance(value, list):
        for item in value:
            _collect_signup_entries(item, entries, depth + 1, max_depth)


def parse_signups_from_json_payload(payload):
    """Parse date, active names, and crossed names from JSON payload."""
    raw_date = _find_first_date(payload)
    if not raw_date:
        raise ValueError("Could not find a 'date' field in the JSON file.")

    screenshot_date = parse_date(raw_date)

    entries = []
    _collect_signup_entries(payload, entries)

    active, crossed = set(), set()
    for entry in entries:
        name = str(entry.get("name", "")).strip()
        status = str(entry.get("status", "")).strip().lower()
        classname = str(entry.get("classname", "")).strip().lower()

        if not name:
            continue
        if classname in {"absence", "bench"}:
            continue

        if status == "primary":
            active.add(name)
        elif status in {"queded", "queued"}:
            crossed.add(name)

    active -= crossed
    return screenshot_date, active, crossed


def _is_json_attachment(attachment):
    filename = (attachment.filename or "").lower()
    ctype = (attachment.content_type or "").lower() if attachment.content_type else ""
    return filename.endswith(".json") or "json" in ctype


def compute_streak(dates):
    """Compute current and longest consecutive-day streaks from date set."""
    if not dates:
        return 0, 0

    sorted_dates = sorted(dates)
    longest = 1
    current_run = 1

    for i in range(1, len(sorted_dates)):
        if sorted_dates[i] - sorted_dates[i - 1] == timedelta(days=1):
            current_run += 1
        else:
            current_run = 1
        longest = max(longest, current_run)

    # Current streak = streak ending at the latest recorded day for that person.
    current = 1
    for i in range(len(sorted_dates) - 1, 0, -1):
        if sorted_dates[i] - sorted_dates[i - 1] == timedelta(days=1):
            current += 1
        else:
            break

    return current, longest


def compute_active_streak_by_appearances(active_dates, benched_dates):
    """Compute active streaks by consecutive active appearances.

    Missing calendar dates do not break the streak. The streak resets only when
    the member appears as benched on a recorded date.
    """
    if not active_dates:
        return 0, 0

    all_dates = sorted(active_dates | benched_dates)
    current = 0
    longest = 0

    for day in all_dates:
        if day in active_dates:
            current += 1
            if current > longest:
                longest = current
        elif day in benched_dates:
            current = 0

    current_final = 0
    for day in reversed(all_dates):
        if day in active_dates:
            current_final += 1
        elif day in benched_dates:
            break

    return current_final, longest


def compute_crossed_streak_by_appearances(active_dates, crossed_dates):
    """Compute crossed streaks by consecutive queued appearances.

    Missing calendar dates do not break the streak. The streak resets only when
    the member appears as active on a recorded date.
    """
    if not crossed_dates:
        return 0, 0

    all_dates = sorted(active_dates | crossed_dates)
    current = 0
    longest = 0

    for day in all_dates:
        if day in crossed_dates:
            current += 1
            if current > longest:
                longest = current
        elif day in active_dates:
            current = 0

    current_final = 0
    for day in reversed(all_dates):
        if day in crossed_dates:
            current_final += 1
        elif day in active_dates:
            break

    return current_final, longest


def build_stats(daily_records):
    """Build per-name stats from daily records with caching."""
    # Create a server-independent cache key based on records content
    records_hash = _hash_records(daily_records)
    
    # Check if we have cached stats for this exact set of records
    cache_key = "_stats"
    if cache_key in stats_cache and stats_cache[cache_key][1] == records_hash:
        return stats_cache[cache_key][0]
    
    active_dates_by_name = {}
    crossed_dates_by_name = {}

    for day, record in daily_records.items():
        for name in record["active"]:
            active_dates_by_name.setdefault(name, set()).add(day)
        for name in record["crossed"]:
            crossed_dates_by_name.setdefault(name, set()).add(day)

    all_names = set(active_dates_by_name.keys()) | set(crossed_dates_by_name.keys())
    stats = {}
    for name in all_names:
        active_set = active_dates_by_name.get(name, set())
        crossed_set = crossed_dates_by_name.get(name, set())
        active_current, active_longest = compute_active_streak_by_appearances(active_set, crossed_set)
        crossed_current, crossed_longest = compute_crossed_streak_by_appearances(active_set, crossed_set)
        stats[name] = {
            "active_current_streak": active_current,
            "active_longest_streak": active_longest,
            "crossed_current_streak": crossed_current,
            "crossed_longest_streak": crossed_longest,
        }
    
    # Cache the computed stats
    stats_cache[cache_key] = (stats, records_hash)
    return stats


async def process_json_attachment(attachment, server_key):
    """Read JSON attachment, parse date/names, and store parsed names."""
    raw_data = await attachment.read()
    payload = json.loads(raw_data.decode("utf-8-sig"))
    screenshot_date, active, crossed = parse_signups_from_json_payload(payload)

    daily_records = load_records(server_key)

    # Latest upload wins for the same date (overwrite, do not merge).
    daily_records[screenshot_date] = {"active": set(active), "crossed": set(crossed)}
    save_records(server_key, daily_records)
    return screenshot_date, active, crossed


@bot.command(name="record")
@commands.guild_only()
@has_management_access()
async def record(ctx):
    """Record one JSON upload.
    Usage: !record (attach .json)
    """
    json_attachment = next((a for a in ctx.message.attachments if _is_json_attachment(a)), None)
    if not json_attachment:
        await ctx.send("Please attach a .json file.")
        return

    try:
        server_key = get_server_key(ctx.guild.id if ctx.guild else None, ctx.author.id)
        screenshot_date, active, crossed = await process_json_attachment(json_attachment, server_key)
        daily_records = load_records(server_key)
        stats = build_stats(daily_records)

        embed = discord.Embed(
            title=f"Recorded Signups - {screenshot_date.isoformat()}",
            color=discord.Color.blue(),
        )
        embed.add_field(
            name="Active Names",
            value=", ".join(sorted(active)) if active else "None",
            inline=False,
        )
        embed.add_field(
            name="Benched Names",
            value=", ".join(sorted(crossed)) if crossed else "None",
            inline=False,
        )
        embed.add_field(
            name="Tracked People",
            value=str(len(stats)),
            inline=False,
        )
        await ctx.send(embed=embed)
    except Exception as e:
        await ctx.send(format_json_error(e))


@bot.command(name="auto")
@commands.guild_only()
@has_management_access()
async def auto_record(ctx):
    """Auto-read date and signups from JSON.
    Usage: !auto (attach .json)
    """
    if not ctx.message.attachments:
        await ctx.send("Please attach a .json file.")
        return

    json_attachment = next((a for a in ctx.message.attachments if _is_json_attachment(a)), None)
    if not json_attachment:
        await ctx.send("Attachment must be a .json file.")
        return

    try:
        server_key = get_server_key(ctx.guild.id if ctx.guild else None, ctx.author.id)
        screenshot_date, active, crossed = await process_json_attachment(json_attachment, server_key)

        daily_records = load_records(server_key)
        stats = build_stats(daily_records)

        embed = discord.Embed(
            title=f"Recorded Signups - {screenshot_date.isoformat()}",
            color=discord.Color.blue(),
        )
        embed.add_field(
            name="Active Names",
            value=", ".join(sorted(active)) if active else "None",
            inline=False,
        )
        embed.add_field(
            name="Benched Names",
            value=", ".join(sorted(crossed)) if crossed else "None",
            inline=False,
        )
        embed.add_field(
            name="Tracked People",
            value=str(len(stats)),
            inline=False,
        )
        await ctx.send(embed=embed)
    except Exception as e:
        await ctx.send(format_json_error(e))

@bot.command(name="otterreset")
@commands.guild_only()
@has_management_access()
async def otterreset(ctx):
    """Reset all tallies"""
    server_key = get_server_key(ctx.guild.id if ctx.guild else None, ctx.author.id)
    save_records(server_key, {})
    await ctx.send("Tallies reset!")

@bot.command()
async def tally(ctx):
    """Show current tallies"""
    server_key = get_server_key(ctx.guild.id if ctx.guild else None, ctx.author.id)
    daily_records = load_records(server_key)
    stats = build_stats(daily_records)
    if not stats:
        await ctx.send("No records yet. Use !record with a .json attachment.")
        return

    latest_date = max(daily_records.keys())
    latest_active_names = daily_records[latest_date]["active"]
    latest_crossed_names = daily_records[latest_date]["crossed"]

    # Sort names case-insensitively
    sorted_names = sorted(stats.keys(), key=lambda n: n.casefold())

    # Pre-convert sets to lowercase for O(1) lookups
    active_set = {n.lower() for n in latest_active_names}
    crossed_set = {n.lower() for n in latest_crossed_names}
    
    lines = []
    for name in sorted_names:
        s = stats[name]
        name_lower = name.lower()
        if name_lower in crossed_set:
            status = "benched"
        elif name_lower in active_set:
            status = "active"
        else:
            status = "no signup"
        lines.append(
            f"{name} [{status}]: active streak {s['active_current_streak']} (best {s['active_longest_streak']}), "
            f"benched streak {s['crossed_current_streak']} (best {s['crossed_longest_streak']})"
        )
    await send_paginated_embed(
        ctx=ctx,
        title=f"Current Tallies - {latest_date.isoformat()}",
        lines=lines,
        color=discord.Color.green(),
        footer_base=f"Total Tracked: {len(lines)} | Latest Date: {latest_date.isoformat()}",
        per_page=20,
    )


@bot.command(name="agris")
async def agris(ctx):
    server_key = get_server_key(ctx.guild.id if ctx.guild else None, ctx.author.id)
    daily_records = load_records(server_key)
    stats = build_stats(daily_records)
    if not stats:
        await ctx.send("No records found yet.")
        return

    # Sort by most benches first.
    sorted_names = sorted(
        stats.items(),
        key=lambda x: (-x[1]["crossed_longest_streak"], -x[1]["crossed_current_streak"], -x[1]["active_current_streak"])
    )

    lines = []
    for name, s in sorted_names:
        lines.append(
            f"**{name}**: Active: {s['active_current_streak']} | "
            f"Benched: {s['crossed_current_streak']} (best {s['crossed_longest_streak']})"
        )

    latest_date = max(daily_records.keys()).isoformat() if daily_records else "N/A"
    footer = f"Total Tracked: {len(stats)} | Last Sync: {latest_date}"
    await send_paginated_embed(
        ctx=ctx,
        title="Agris Member Tally",
        lines=lines,
        color=0x5865F2,
        footer_base=footer,
        per_page=20,
    )


@bot.command(name="highbench")
async def highbench(ctx):
    """Show players with bench streak over 4.
    Usage: !highbench
    """
    server_key = get_server_key(ctx.guild.id if ctx.guild else None, ctx.author.id)
    daily_records = load_records(server_key)
    stats = build_stats(daily_records)
    if not stats:
        await ctx.send("No records found yet.")
        return

    # Filter players with bench streak > 4
    high_bench = [(name, s) for name, s in stats.items() if s["crossed_current_streak"] >= 4]
    
    if not high_bench:
        await ctx.send("No players with bench streak over 4.")
        return
    
    # Sort by current bench streak descending
    high_bench.sort(key=lambda x: -x[1]["crossed_current_streak"])
    
    lines = []
    for name, s in high_bench:
        lines.append(
            f"**{name}**: Benched Streak {s['crossed_current_streak']} (best {s['crossed_longest_streak']})"
        )
    
    await send_paginated_embed(
        ctx=ctx,
        title="Players with Bench Streak > 4",
        lines=lines,
        color=0xFF6B6B,
        footer_base=f"Total: {len(lines)} players",
        per_page=20,
    )


@bot.command(name="activestreak")
async def activestreak(ctx):
    """Show players sorted by active streak from highest to lowest.
    Usage: !activestreak
    """
    server_key = get_server_key(ctx.guild.id if ctx.guild else None, ctx.author.id)
    daily_records = load_records(server_key)
    stats = build_stats(daily_records)
    if not stats:
        await ctx.send("No records found yet.")
        return
    
    # Sort by active current streak descending, then by active longest streak
    sorted_names = sorted(
        stats.items(),
        key=lambda x: (-x[1]["active_current_streak"], -x[1]["active_longest_streak"])
    )
    
    lines = []
    for name, s in sorted_names:
        lines.append(
            f"**{name}**: Active Streak {s['active_current_streak']} (best {s['active_longest_streak']})"
        )
    
    latest_date = max(daily_records.keys()).isoformat() if daily_records else "N/A"
    await send_paginated_embed(
        ctx=ctx,
        title="Active Streak Rankings (Highest to Lowest)",
        lines=lines,
        color=0x51CF66,
        footer_base=f"Total Tracked: {len(stats)} | Last Sync: {latest_date}",
        per_page=20,
    )


@bot.command(name="removeplayer")
@commands.guild_only()
@has_management_access()
async def removeplayer(ctx, *, name: str):
    """Remove a tracked player from all records for this server.
    Usage: !removeplayer <name>
    """
    server_key = get_server_key(ctx.guild.id if ctx.guild else None, ctx.author.id)
    daily_records = load_records(server_key)

    target = name.strip()
    if not target:
        await ctx.send("Please provide a player name. Example: !removeplayer EBOY3")
        return

    target_lower = target.casefold()
    removed_active = 0
    removed_benched = 0

    for _, record in daily_records.items():
        active_match = next((n for n in record["active"] if n.casefold() == target_lower), None)
        benched_match = next((n for n in record["crossed"] if n.casefold() == target_lower), None)

        if active_match is not None:
            record["active"].remove(active_match)
            removed_active += 1
        if benched_match is not None:
            record["crossed"].remove(benched_match)
            removed_benched += 1

    if removed_active == 0 and removed_benched == 0:
        await ctx.send(f"No tracked entries found for '{target}'.")
        return

    save_records(server_key, daily_records)
    await ctx.send(
        f"Removed '{target}' from tracking. "
        f"Active removals: {removed_active}, Benched removals: {removed_benched}."
    )


@bot.command(name="resetbenchstreak")
@commands.guild_only()
@has_management_access()
async def resetbenchstreak(ctx, *, name: str):
    """Reset bench streak for a specific player.
    Usage: !resetbenchstreak <name>
    """
    server_key = get_server_key(ctx.guild.id if ctx.guild else None, ctx.author.id)
    daily_records = load_records(server_key)

    target = name.strip()
    if not target:
        await ctx.send("Please provide a player name. Example: !resetbenchstreak EBOY3")
        return

    target_lower = target.casefold()
    removed_count = 0

    for _, record in daily_records.items():
        benched_match = next((n for n in record["crossed"] if n.casefold() == target_lower), None)
        if benched_match is not None:
            record["crossed"].remove(benched_match)
            removed_count += 1

    if removed_count == 0:
        await ctx.send(f"No bench streak entries found for '{target}'.")
        return

    save_records(server_key, daily_records)
    await ctx.send(f"Reset bench streak for '{target}'. Removed {removed_count} benched entries.")


@bot.command(name="roleadd")
@commands.guild_only()
@commands.has_permissions(administrator=True)
async def roleadd(ctx, *, role_name: str):
    """Set the role allowed to use management commands.
    Usage: !roleadd <role name>
    """
    target_name = role_name.strip()
    if not target_name:
        await ctx.send("Please provide a role name. Example: !roleadd Leadership")
        return

    matched_role = next((r for r in ctx.guild.roles if r.name.casefold() == target_name.casefold()), None)
    if matched_role is None:
        await ctx.send(f"Role '{target_name}' was not found in this server.")
        return

    server_key = get_server_key(ctx.guild.id, ctx.author.id)
    set_management_role_name(server_key, matched_role.name)
    await ctx.send(
        f"Management role set to '{matched_role.name}'. "
        "Users with this role can use !record, !auto, !removeplayer, !resetbenchstreak, and !otterreset."
    )


@bot.command(name="helpagris")
async def helpagris(ctx):
    """Show Agris bot command help."""
    embed = discord.Embed(
        title="Agris Bot Commands",
        color=discord.Color.gold(),
        description="Use these commands to record JSON signups and view member tallies.",
    )
    embed.add_field(
        name="!auto <attach .json>",
        value="Reads date/name/status/classname from JSON and records the day.",
        inline=False,
    )
    embed.add_field(
        name="!record <attach .json>",
        value="Records one JSON file. Date is read from the JSON 'date' field.",
        inline=False,
    )
    embed.add_field(
        name="!tally",
        value="Shows latest-day active names with active streak and benched tally.",
        inline=False,
    )
    embed.add_field(
        name="!agris",
        value="Shows full member ranking sorted by current benched streak, including active streak info.",
        inline=False,
    )
    embed.add_field(
        name="!highbench",
        value="Shows players with bench streak over 4.",
        inline=False,
    )
    embed.add_field(
        name="!activestreak",
        value="Shows all players sorted by active streak from highest to lowest.",
        inline=False,
    )
    embed.add_field(
        name="!removeplayer <name>",
        value="Removes a player from all tracked records for this server.",
        inline=False,
    )
    embed.add_field(
        name="!resetbenchstreak <name>",
        value="Resets bench streak for a specific player (removes queued entries).",
        inline=False,
    )
    embed.add_field(
        name="!export",
        value="Export all stats and records as a copyable text file.",
        inline=False,
    )
    embed.add_field(
        name="!roleadd <role name>",
        value="Sets the server role that can run management commands.",
        inline=False,
    )
    embed.add_field(
        name="!otterreset",
        value="Clears all saved records.",
        inline=False,
    )
    embed.add_field(
        name="Server Isolation",
        value="Each Discord server stores records in its own file. One server cannot mix with another.",
        inline=False,
    )
    embed.add_field(
        name="Role Protected",
        value="Set a management role with !roleadd. Server Administrators are always allowed.",
        inline=False,
    )
    embed.set_footer(text="Benched streak counts consecutive queued appearances and resets when active.")
    await ctx.send(embed=embed)


@bot.command(name="export")
async def export_data(ctx):
    """Export all stats and records as a copyable text file.
    Usage: !export
    """
    server_key = get_server_key(ctx.guild.id if ctx.guild else None, ctx.author.id)
    daily_records = load_records(server_key)
    stats = build_stats(daily_records)
    
    if not stats:
        await ctx.send("No records to export yet.")
        return
    
    # Generate text content
    lines = []
    lines.append(f"=== Agris Bot Export ===")
    lines.append(f"Server: {ctx.guild.name if ctx.guild else 'DM'}")
    lines.append(f"Exported: {datetime.now().isoformat()}")
    lines.append("")
    
    # Stats summary
    lines.append(f"Total Tracked Players: {len(stats)}")
    lines.append("")
    lines.append("=== Player Stats ===")
    lines.append("")
    
    sorted_names = sorted(stats.items(), key=lambda x: (-x[1]["crossed_longest_streak"], -x[1]["crossed_current_streak"]))
    for name, s in sorted_names:
        lines.append(f"{name}:")
        lines.append(f"  Active Current Streak: {s['active_current_streak']}")
        lines.append(f"  Active Longest Streak: {s['active_longest_streak']}")
        lines.append(f"  Bench Current Streak: {s['crossed_current_streak']}")
        lines.append(f"  Bench Longest Streak: {s['crossed_longest_streak']}")
        lines.append("")
    
    # Daily records
    lines.append("=== Daily Records ===")
    lines.append("")
    for date_obj in sorted(daily_records.keys()):
        record = daily_records[date_obj]
        lines.append(f"Date: {date_obj.isoformat()}")
        lines.append(f"  Active: {', '.join(sorted(record['active'])) if record['active'] else 'None'}")
        lines.append(f"  Benched: {', '.join(sorted(record['crossed'])) if record['crossed'] else 'None'}")
        lines.append("")
    
    text_content = "\n".join(lines)
    
    # Save to local file
    export_dir = "exports"
    os.makedirs(export_dir, exist_ok=True)
    filename = f"{export_dir}/export_{server_key}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.txt"
    
    try:
        with open(filename, "w", encoding="utf-8", newline="\n") as f:
            f.write(text_content)
        
        # Send as Discord attachment
        with open(filename, "rb") as f:
            await ctx.send(f"Export completed. Records saved to {os.path.basename(filename)}", file=discord.File(f, os.path.basename(filename)))
    except Exception as e:
        await ctx.send(f"Error exporting data: {e}")


@bot.event
async def on_command_error(ctx, error):
    """Friendly permission and usage errors for commands."""
    if isinstance(error, (commands.MissingPermissions, commands.CheckFailure)):
        await ctx.send("You do not have the required role to use that command.")
        return
    if isinstance(error, commands.NoPrivateMessage):
        await ctx.send("That command can only be used in a server.")
        return
    if isinstance(error, commands.MissingRequiredArgument):
        await ctx.send("Missing command argument. Try !helpagris for usage.")
        return
    raise error

token = os.getenv("DISCORD_TOKEN")
if not token:
    raise RuntimeError("DISCORD_TOKEN is missing. Add it to your .env file.")

bot.run(token)