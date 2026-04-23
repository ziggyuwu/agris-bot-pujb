import discord
from discord.ext import commands
from discord.ui import View
from datetime import datetime, timedelta
import json
import os
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

intents = discord.Intents.default()
intents.message_content = True
intents.members = True 
bot = commands.Bot(command_prefix="!", intents=intents)

RECORDS_DIR = "signup_records"
SETTINGS_FILE = "settings.json"

# --- HELPER FUNCTIONS ---

# ---Settings---
def load_settings():
    if os.path.exists(SETTINGS_FILE):
        with open(SETTINGS_FILE, "r") as f:
            try:
                return json.load(f)
            except json.JSONDecodeError:
                return {}
    return {}

def save_settings(settings):
    with open(SETTINGS_FILE, "w") as f:
        json.dump(settings, f, indent=4)

def has_management_access():
    async def predicate(ctx):
        if ctx.author.guild_permissions.administrator:
            return True
        
        server_key = str(ctx.guild.id)
        settings = load_settings()
        allowed_role_id = settings.get(server_key, {}).get("management_role_id")
        
        if allowed_role_id and any(role.id == int(allowed_role_id) for role in ctx.author.roles):
            return True
            
        await ctx.send("❌ You do not have permission (Leadership role required).")
        return False
    return commands.check(predicate)

def get_server_key(guild_id):
    return str(guild_id)

def get_records_file(server_key):
    os.makedirs(RECORDS_DIR, exist_ok=True)
    return os.path.join(RECORDS_DIR, f"signup_records_{server_key}.json")

def load_records(server_key):
    records = {}
    records_file = get_records_file(server_key)
    if os.path.exists(records_file):
        try:
            with open(records_file, "r") as f:
                data = json.load(f)
                for date_str, record in data.items():
                    records[date_str] = {
                        "active_ids": record.get("active_ids", []),
                        "crossed_ids": record.get("crossed_ids", [])
                    }
        except Exception as e:
            print(f"Error loading records: {e}")
    return records

def save_records(server_key, records):
    try:
        with open(get_records_file(server_key), "w") as f:
            json.dump(records, f, indent=4)
    except Exception as e:
        print(f"Error saving records: {e}")

def build_stats(daily_records):
    stats = {}
    
    # CRITICAL: Sort dates so Day 1 is processed before Day 2
    # This assumes your date format is DD-MM-YYYY. 
    # If using DD-MM-YYYY, standard string sort might fail for different months.
    # We convert to actual date objects for sorting.
    sorted_dates = sorted(daily_records.keys(), key=lambda x: datetime.strptime(x, "%d-%m-%Y"))

    for date_str in sorted_dates:
        day_data = daily_records[date_str]
        active = day_data.get("active_ids", [])
        crossed = day_data.get("crossed_ids", [])
        
        for u_id in set(active + crossed):
            u_id_str = str(u_id)
            if u_id_str not in stats:
                stats[u_id_str] = {
                    "active_current_streak": 0, "active_longest_streak": 0,
                    "crossed_current_streak": 0, "crossed_longest_streak": 0
                }
            
            s = stats[u_id_str]
            
            if u_id_str in active:
                # User was Primary
                s["active_current_streak"] += 1
                s["crossed_current_streak"] = 0  # Bench streak resets
            elif u_id_str in crossed:
                # User was Queued
                s["crossed_current_streak"] += 1
                s["active_current_streak"] = 0   # Active streak resets
                
            # Update best-ever records
            s["active_longest_streak"] = max(s["active_longest_streak"], s["active_current_streak"])
            s["crossed_longest_streak"] = max(s["crossed_longest_streak"], s["crossed_current_streak"])
            
    return stats

# --- Pagination Helper ---
class PaginationView(discord.ui.View):
    def __init__(self, title, chunks, color, footer_text):
        super().__init__(timeout=60)
        self.title = title
        self.chunks = chunks
        self.color = color
        self.footer_text = footer_text
        self.current_page = 0

    def create_embed(self):
        description = "\n".join(self.chunks[self.current_page])
        embed = discord.Embed(
            title=f"{self.title} (Page {self.current_page + 1}/{len(self.chunks)})",
            description=description,
            color=self.color
        )
        embed.set_footer(text=self.footer_text)
        return embed

    @discord.ui.button(label="Previous", style=discord.ButtonStyle.gray)
    async def previous_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.current_page > 0:
            self.current_page -= 1
            await interaction.response.edit_message(embed=self.create_embed(), view=self)

    @discord.ui.button(label="Next", style=discord.ButtonStyle.gray)
    async def next_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.current_page < len(self.chunks) - 1:
            self.current_page += 1
            await interaction.response.edit_message(embed=self.create_embed(), view=self)

async def send_paginated_embed(ctx, title, lines, color, footer_text):
    chunk_size = 10
    chunks = [lines[i:i + chunk_size] for i in range(0, len(lines), chunk_size)]
    if not chunks:
        return await ctx.send(embed=discord.Embed(title=title, description="No data.", color=color))
    view = PaginationView(title, chunks, color, footer_text)
    await ctx.send(embed=view.create_embed(), view=view)

# --- COMMANDS ---

@bot.command(name="setadminrole")
@commands.has_permissions(administrator=True)
async def set_management_role(ctx, role: discord.Role):
    server_key = str(ctx.guild.id)
    settings = load_settings()
    settings.setdefault(server_key, {})["management_role_id"] = role.id
    save_settings(settings)
    await ctx.send(f"✅ Management role set to **{role.name}**.")

@bot.command(name="auto")
@has_management_access()
async def auto_record(ctx):
    if not ctx.message.attachments:
        return await ctx.send("Please attach a .json file.")
    
    attachment = ctx.message.attachments[0]
    try:
        server_key = get_server_key(ctx.guild.id)
        raw_data = await attachment.read()
        # Using utf-8-sig to handle potential BOM characters from Windows exports
        payload = json.loads(raw_data.decode("utf-8-sig"))
        
        signups = payload.get("signUps", [])
        active_ids = []
        crossed_ids = []

        # The list of terms that DISQUALIFY an entry from being tracked
        naughty_list = ["absence", "tentative", "late", "bench", "benched"]

        for entry in signups:
            u_id = str(entry.get("userId", "")).strip()
            if not u_id or u_id == "None": 
                continue
            
            # Extract fields and force to clean, lowercase strings
            # We use .get(key, "") to prevent errors if a field is missing
            c_name = str(entry.get("className", "")).strip().lower()
            u_status = str(entry.get("status", "")).strip().lower()

            # 1. EXCLUSION GATEKEEPER
            # This checks if ANY word in our naughty list is found within the class name
            is_excluded = any(word in c_name for word in naughty_list)
            
            if is_excluded:
                # If they match any of the forbidden terms, we skip them entirely
                continue

            # 2. STATUS ASSIGNMENT
            # Only those who passed the gatekeeper get sorted here
            if u_status == "primary":
                active_ids.append(u_id)
            elif u_status == "queued":
                crossed_ids.append(u_id)

        # Update the records
        date_str = payload.get("date", datetime.now().strftime("%d-%m-%Y"))
        recs = load_records(server_key)
        recs[date_str] = {"active_ids": active_ids, "crossed_ids": crossed_ids}
        save_records(server_key, recs)
        
        await ctx.send(f"✅ **Processing Complete**\n**Active (Primary):** {len(active_ids)}\n**Benched (Queued):** {len(crossed_ids)}\n*Ignored {len(signups) - (len(active_ids) + len(crossed_ids))} entries based on class names.*")

    except Exception as e:
        await ctx.send(f"❌ **Error:** {e}")

@bot.command(name="syncagris")
@commands.guild_only()
@has_management_access() # <--- Use this instead of administrator=True
async def sync_agris_roles(ctx):
    server_key = str(ctx.guild.id)
    daily_records = load_records(server_key)
    stats = build_stats(daily_records)
    
    agris_role = discord.utils.get(ctx.guild.roles, name="Agris")
    if not agris_role:
        return await ctx.send("❌ Role 'Agris' not found.")

    updates = {"added": 0, "removed": 0, "unlinked": 0, "failed": 0}

    for user_id_str, user_stat in stats.items():
        member = ctx.guild.get_member(int(user_id_str))
        if member:
            try:
                should_have = user_stat["crossed_current_streak"] >= 4
                has_role = agris_role in member.roles
                if should_have and not has_role:
                    await member.add_roles(agris_role); updates["added"] += 1
                elif not should_have and has_role:
                    await member.remove_roles(agris_role); updates["removed"] += 1
            except:
                updates["failed"] += 1
        else:
            updates["unlinked"] += 1

    embed = discord.Embed(title="Agris Sync Result", color=0x2ECC71)
    embed.add_field(name="Added", value=str(updates["added"]))
    embed.add_field(name="Removed", value=str(updates["removed"]))
    embed.add_field(name="Not in Server", value=str(updates["unlinked"]))
    await ctx.send(embed=embed)

@bot.command(name="agrischeck")
@commands.guild_only()
async def agrischeck(ctx):
    server_key = get_server_key(ctx.guild.id)
    daily_records = load_records(server_key)
    stats = build_stats(daily_records)
    u_id = str(ctx.author.id)

    if u_id not in stats:
        return await ctx.send("No records found for your ID.")

    s = stats[u_id]
    embed = discord.Embed(title=f"Stats for {ctx.author.display_name}", color=0x2ECC71)
    embed.add_field(name="Bench Streak", value=f"Current: {s['crossed_current_streak']} | Best: {s['crossed_longest_streak']}")
    await ctx.send(embed=embed)

@bot.command(name="agris")
@commands.guild_only()
async def agris_ranking(ctx):
    server_key = get_server_key(ctx.guild.id)
    daily_records = load_records(server_key)
    stats = build_stats(daily_records)
    if not stats: return await ctx.send("No records found.")

    # Sort the stats
    sorted_items = sorted(stats.items(), key=lambda x: (
        -x[1]["crossed_current_streak"], 
        -x[1]["crossed_longest_streak"], 
        x[1]["active_current_streak"]
    ))
    
    lines = []
    for u_id_str, s in sorted_items:
        # Try to find the member in the server to get their current nickname
        member = ctx.guild.get_member(int(u_id_str))
        name = member.display_name if member else f"User({u_id_str})"
        
        emoji = "🔥 " if s['crossed_current_streak'] >= 4 else ""
        lines.append(
            f"{emoji}**{name}**: Bench: **{s['crossed_current_streak']}** | "
            f"Best: {s['crossed_longest_streak']} | Active: {s['active_current_streak']}"
        )
    
    await send_paginated_embed(ctx, "Agris Rankings", lines, 0x5865F2, "Use buttons to scroll")

@bot.command(name="untrack")
@commands.guild_only()
@has_management_access()
async def untrack_user(ctx, discord_id: str):
    """Removes a user from all historical records using their Discord ID."""
    server_key = get_server_key(ctx.guild.id)
    daily_records = load_records(server_key)
    
    if not daily_records:
        return await ctx.send("No records found to edit.")

    found = False
    # Iterate through every date entry in your storage
    for date_str in daily_records:
        active = daily_records[date_str].get("active_ids", [])
        crossed = daily_records[date_str].get("crossed_ids", [])

        # Remove the ID if it exists in either list
        if discord_id in active:
            active.remove(discord_id)
            found = True
        if discord_id in crossed:
            crossed.remove(discord_id)
            found = True

    if found:
        # Save the scrubbed data back to the file
        save_records(server_key, daily_records)
        await ctx.send(f"✅ Successfully removed ID `{discord_id}` from all historical tracking.")
        print(f"LOG: Admin {ctx.author} untracked ID {discord_id}")
    else:
        await ctx.send(f"❌ ID `{discord_id}` was not found in any tracking records.")

@bot.command(name="resetbench")
@commands.guild_only()
@has_management_access()
async def reset_bench_streak(ctx, member: discord.Member):
    """Resets a user's current bench streak by modifying the most recent record."""
    server_key = get_server_key(ctx.guild.id)
    records = load_records(server_key)

    if not records:
        return await ctx.send("❌ No records found.")

    # Sort dates properly
    sorted_dates = sorted(records.keys(), key=lambda x: datetime.strptime(x, "%d-%m-%Y"))
    latest_date = sorted_dates[-1]

    user_id = str(member.id)
    day_data = records[latest_date]

    crossed = day_data.get("crossed_ids", [])
    active = day_data.get("active_ids", [])

    if user_id not in crossed:
        return await ctx.send(f"❌ {member.display_name} is not currently benched on the latest record.")

    # Remove from bench
    crossed.remove(user_id)

    # OPTIONAL: add to active to fully break streak logic
    if user_id not in active:
        active.append(user_id)

    # Save changes
    records[latest_date]["crossed_ids"] = crossed
    records[latest_date]["active_ids"] = active
    save_records(server_key, records)

    await ctx.send(f"✅ Reset bench streak for **{member.display_name}** (updated latest record: {latest_date})")

token = os.getenv("DISCORD_TOKEN")
if not token:
    raise RuntimeError("DISCORD_TOKEN is missing. Add it to your .env file.")

bot.run(token)