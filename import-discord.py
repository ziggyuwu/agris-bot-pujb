import discord
from discord.ext import commands
from discord.ui import Button, View
from datetime import datetime, timedelta
import json
import os
from dotenv import load_dotenv

# Load environment variables from .env file
load_dotenv()

intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)


# --- NEW PAGINATION CLASS ---
class PaginatorView(View):
    def __init__(self, chunks, title, footer_base):
        super().__init__(timeout=120)
        self.chunks = chunks
        self.title = title
        self.footer_base = footer_base
        self.current_page = 0

    async def update_embed(self, interaction):
        embed = discord.Embed(
            title=f"{self.title} (Page {self.current_page + 1}/{len(self.chunks)})",
            description=self.chunks[self.current_page],
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
        if self.current_page < len(self.chunks) - 1:
            self.current_page += 1
            await self.update_embed(interaction)


async def send_paginated_embed(ctx, title, lines, color, footer_base, per_page=20):
    """Send lines as a paginated embed when needed."""
    if not lines:
        await ctx.send("No entries to display.")
        return

    chunks = ["\n".join(lines[i:i + per_page]) for i in range(0, len(lines), per_page)]
    embed = discord.Embed(
        title=f"{title} (Page 1/{len(chunks)})",
        description=chunks[0],
        color=color,
    )
    embed.set_footer(text=footer_base)

    if len(chunks) == 1:
        await ctx.send(embed=embed)
        return

    view = PaginatorView(chunks, title, footer_base)
    await ctx.send(embed=embed, view=view)

# Storage per server: {server_key: {date_obj: {"active": set[str], "crossed": set[str]}}}
server_records = {}
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
    if server_key in server_records:
        return server_records[server_key]

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

    server_records[server_key] = records
    return records


def save_records(server_key):
    """Save records for one server key to its JSON file."""
    try:
        records = server_records.get(server_key, {})
        data = {}
        for date_obj, record in records.items():
            date_str = date_obj.isoformat()
            data[date_str] = {
                "active": sorted(record["active"]),
                "crossed": sorted(record["crossed"])
            }
        records_file = get_records_file(server_key)
        with open(records_file, "w") as f:
            json.dump(data, f, indent=2)
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
    formats = [
        "%d/%m/%Y",
        "%d-%m-%Y",
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


def _find_first_date(value):
    """Recursively find the first `date` field in a JSON object/list."""
    if isinstance(value, dict):
        for key, val in value.items():
            if str(key).lower() == "date" and isinstance(val, str):
                return val
        for val in value.values():
            found = _find_first_date(val)
            if found:
                return found
    elif isinstance(value, list):
        for item in value:
            found = _find_first_date(item)
            if found:
                return found
    return None


def _collect_signup_entries(value, entries):
    """Collect objects that look like signup rows with name/status/classname keys."""
    if isinstance(value, dict):
        lowered = {str(k).lower(): v for k, v in value.items()}
        if "name" in lowered and "status" in lowered:
            entries.append(lowered)
        for val in value.values():
            _collect_signup_entries(val, entries)
    elif isinstance(value, list):
        for item in value:
            _collect_signup_entries(item, entries)


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
    """Build per-name stats from daily records."""
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

    return stats


async def process_json_attachment(attachment, server_key):
    """Read JSON attachment, parse date/names, and store parsed names."""
    raw_data = await attachment.read()
    payload = json.loads(raw_data.decode("utf-8-sig"))
    screenshot_date, active, crossed = parse_signups_from_json_payload(payload)

    daily_records = load_records(server_key)

    # Latest upload wins for the same date (overwrite, do not merge).
    daily_records[screenshot_date] = {"active": set(active), "crossed": set(crossed)}
    save_records(server_key)
    return screenshot_date, active, crossed


@bot.command(name="record")
@commands.guild_only()
@has_management_access()
async def record(ctx):
    """Record one JSON upload.
    Usage: !record (attach .json)
    """
    json_attachment = None
    for attachment in ctx.message.attachments:
        if _is_json_attachment(attachment):
            json_attachment = attachment
            break

    if not json_attachment:
        await ctx.send("Please attach a .json file.")
        return

    try:
        server_key = get_server_key(ctx.guild.id if ctx.guild else None, ctx.author.id)
        screenshot_date, active, crossed = await process_json_attachment(json_attachment, server_key)
        stats = build_stats(load_records(server_key))

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

    json_attachment = None
    for attachment in ctx.message.attachments:
        if _is_json_attachment(attachment):
            json_attachment = attachment
            break

    if not json_attachment:
        await ctx.send("Attachment must be a .json file.")
        return

    try:
        server_key = get_server_key(ctx.guild.id if ctx.guild else None, ctx.author.id)
        screenshot_date, active, crossed = await process_json_attachment(json_attachment, server_key)

        stats = build_stats(load_records(server_key))

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
    server_records[server_key] = {}
    save_records(server_key)
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
    latest_active_names = set(daily_records[latest_date]["active"])
    latest_crossed_names = set(daily_records[latest_date]["crossed"])
    all_names = sorted(stats.keys())
    if not all_names:
        await ctx.send("No tracked names found.")
        return

    # Tally view is alphabetical.
    sorted_names = sorted(all_names, key=lambda n: n.casefold())

    lines = []
    for name in sorted_names:
        s = stats[name]
        if name in latest_crossed_names:
            status = "benched"
        elif name in latest_active_names:
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

    removed_active = 0
    removed_benched = 0

    for _, record in daily_records.items():
        active_match = next((n for n in record["active"] if n.casefold() == target.casefold()), None)
        benched_match = next((n for n in record["crossed"] if n.casefold() == target.casefold()), None)

        if active_match is not None:
            record["active"].remove(active_match)
            removed_active += 1
        if benched_match is not None:
            record["crossed"].remove(benched_match)
            removed_benched += 1

    if removed_active == 0 and removed_benched == 0:
        await ctx.send(f"No tracked entries found for '{target}'.")
        return

    save_records(server_key)
    await ctx.send(
        f"Removed '{target}' from tracking. "
        f"Active removals: {removed_active}, Benched removals: {removed_benched}."
    )


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
        "Users with this role can use !record, !auto, !removeplayer, and !otterreset."
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
        name="!removeplayer <name>",
        value="Removes a player from all tracked records for this server.",
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