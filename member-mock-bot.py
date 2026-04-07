import os
import discord
from discord.ext import commands, tasks
from discord import app_commands
import json
import gspread
from gspread.cell import Cell
from google.oauth2.service_account import Credentials
from datetime import datetime, timedelta
import pytz
from dotenv import load_dotenv
from typing import Optional, List, Dict

# --- INITIALIZATION ---
load_dotenv()
intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)

# Configuration
GOOGLE_SHEETS_CREDENTIALS = "credentials.json"
SHEET_ID = os.getenv("GOOGLE_SHEET_ID", "")
ADMIN_IDS = json.loads(os.getenv("ADMIN_USERS", "[]"))
ALLOWED_CHANNELS = json.loads(os.getenv("ALLOWED_CHANNELS", "[]"))
REMINDER_CHANNEL_ID = int(os.getenv("REMINDER_CHANNEL_ID", 0))
PICK_CHANNEL_ID = int(os.getenv("PICK_CHANNEL_ID", 0))
CENTRAL_TZ = pytz.timezone('America/Chicago')
PICK_TIME_HOURS = 2

# --- DATA MANAGEMENT ---

class GoogleSheetsManager:
    def __init__(self, credentials_file: str, sheet_id: str):
        scope = ["https://www.googleapis.com/auth/spreadsheets",
                 "https://www.googleapis.com/auth/drive.file",
                 "https://www.googleapis.com/auth/drive"]
        creds = Credentials.from_service_account_file(credentials_file, scopes=scope)
        self.client = gspread.authorize(creds)
        self.sheet = self.client.open_by_key(sheet_id)
    
    def get_worksheet(self, title: str):
        return self.sheet.worksheet(title)
    
    def load_teams(self) -> Dict:
        ws = self.get_worksheet("teams")
        teams = {}
        for row in ws.get_all_records():
            teams[row['id']] = {
                'team_short': row.get('team_short', 'ERR'),
                'division': row['division'],
                'conference': row['conference'],
                'gm_id': row['gm_id'],
                'name': row['name']
            }
        return teams
    
    def load_users(self) -> Dict:
        ws = self.get_worksheet("users")
        users = {}
        for row in ws.get_all_records():
            users[row['id']] = {
                'username': row['username'],
                'screen_name': row['screen_name'],
                'timezone': row['timezone'],
                'team_pick_order': row['team_pick_order']
            }
        return users
    
    def load_prospects(self) -> Dict:
        ws = self.get_worksheet("prospects")
        prospects = {}
        for row in ws.get_all_records():
            prospects[row['id']] = {
                'f_name': row['f_name'],
                'l_name': row['l_name'],
                'college': row['college'],
                'position_id': row['position_id'],
                'ranking': row['ranking'],
                'drafted': row['drafted'] == 'TRUE'
            }
        return prospects
    
    def load_picks(self) -> List:
        ws = self.get_worksheet("picks")
        picks = []
        for row in ws.get_all_records():
            picks.append({
                'id': row['id'],
                'team_id': row['team_id'],
                'otc_at': row.get('otc_at'),
                'player_id': row.get('player_id'),
                'picked_at': row.get('picked_at'),
                'clock_expire': row.get('clock_expire') == 'TRUE'
            })
        return sorted(picks, key=lambda x: x['id'])
    
    def load_positions(self) -> Dict:
        ws = self.get_worksheet("positions")
        positions = {}
        for row in ws.get_all_records():
            positions[row['id']] = row['position']
        return positions
    
    def update_prospect_drafted(self, prospect_id: int, status: bool = True):
        ws = self.get_worksheet("prospects")
        cell = ws.find(str(prospect_id), in_column=1)
        ws.update_cell(cell.row, 7, "TRUE" if status else "FALSE")
    
    def update_pick(self, pick_id: int, player_id: int, picked_at: str):
        ws = self.get_worksheet("picks")
        cell = ws.find(str(pick_id), in_column=1)
        ws.update_cell(cell.row, 4, player_id)
        ws.update_cell(cell.row, 5, picked_at)

# Global Instance
gs_manager = GoogleSheetsManager(GOOGLE_SHEETS_CREDENTIALS, SHEET_ID)

# Global state
draft_state = {
    "running": False,
    "timer_paused": False,
    "current_pick_index": 0,
    "trade_in_progress": False,
    "picks": [],
    "teams": {},
    "users": {},
    "prospects": {},
    "positions": {},
    "warning_sent": None,
    "last_sync": "Never"
}

@tasks.loop(minutes=5)
async def draft_timer_check():
    if not draft_state.get("running") or draft_state.get("timer_paused"):
        return

    current_pick = get_current_pick()
    if not current_pick:
        return

    # Check if we've already warned for THIS specific pick ID
    if draft_state["warning_sent"] == current_pick['id']:
        return

    time_remaining = get_time_remaining()
    channel_id = bot.get_channel(REMINDER_CHANNEL_ID)

    # If 30 minutes or less (but more than 0)
    if timedelta(minutes=0) < time_remaining <= timedelta(minutes=30):
        current_team = draft_state["teams"].get(current_pick['team_id'])
        gm_user_info = draft_state["users"].get(current_team['gm_id'])
        
        if channel_id and gm_user_info:
            # We assume your 'username' in the sheet is the Discord User ID (int) 
            # or a string we can fetch. Adjust fetch_user as needed.
            try:
                # Find the main draft channel (Replace with your Channel ID)
                
                mention = f"<@{gm_user_info['username']}>"
                await channel_id.send(
                    f"⚠️ {mention} — **{current_team['team_short']}** has less than **30 minutes** remaining on the clock!"
                )
                
                # Mark as sent for this pick ID
                draft_state["warning_sent"] = current_pick['id']
            except Exception as e:
                print(f"Error in timer loop: {e}")

# IMPORTANT: You must start the loop in on_ready
@bot.event
async def on_ready():
    await load_data()
    await bot.tree.sync()
    if not draft_timer_check.is_running():
        draft_timer_check.start()
    print(f"{bot.user} is online and the timer loop is active!")

# --- LOGIC HELPERS ---

async def load_data():
    try:
        draft_state["teams"] = gs_manager.load_teams()
        draft_state["users"] = gs_manager.load_users()
        draft_state["prospects"] = gs_manager.load_prospects()
        draft_state["picks"] = gs_manager.load_picks()
        draft_state["positions"] = gs_manager.load_positions()
        draft_state["last_sync"] = datetime.now(CENTRAL_TZ).strftime("%H:%M:%S")
    except Exception as e:
        print(f"Error loading data: {e}")

def save_status():
    # Saves the current running/paused state to a local file.
    with open("draft_status.json", "w") as f:
        data = {
            "running": draft_state["running"],
            "timer_paused": draft_state["timer_paused"]
        }
        json.dump(data, f)

def load_status():
    """Loads the state back into the draft_state dictionary on startup."""
    try:
        with open("draft_status.json", "r") as f:
            data = json.load(f)
            draft_state["running"] = data.get("running", False)
            draft_state["timer_paused"] = data.get("timer_paused", False)
    except FileNotFoundError:
        # If the file doesn't exist yet, just keep the defaults
        pass

def find_prospect_by_name(first_name: str, last_name: str) -> Optional[int]:
    for pid, prospect in draft_state["prospects"].items():
        if (prospect['f_name'].strip().lower() == first_name.strip().lower() and 
            prospect['l_name'].strip().lower() == last_name.strip().lower()):
            return pid
    return None

def is_empty(value):
    if value is None: return True
    s_val = str(value).strip().lower()
    return s_val in ["", "none", "0", "false", "null", 0, None]

def get_current_pick() -> Optional[Dict]:
    undrafted_picks = [p for p in draft_state["picks"] if is_empty(p['player_id'])]
    return undrafted_picks[0] if undrafted_picks else None

def get_on_deck_and_in_hole() -> tuple:
    undrafted_picks = [p for p in draft_state["picks"] if is_empty(p['player_id'])]
    otc = undrafted_picks[0] if len(undrafted_picks) > 0 else None
    on_deck = undrafted_picks[1] if len(undrafted_picks) > 1 else None
    in_hole = undrafted_picks[2] if len(undrafted_picks) > 2 else None
    return otc, on_deck, in_hole

def get_time_remaining() -> timedelta:
    now = datetime.now(CENTRAL_TZ)
    current_pick = get_current_pick()
    if not current_pick or is_empty(current_pick.get('otc_at')):
        return timedelta(hours=PICK_TIME_HOURS)

    otc_time = datetime.fromisoformat(current_pick['otc_at']).astimezone(CENTRAL_TZ)
    
# 1. Determine "Active" Start Time
    # If pick was made during freeze (10PM-9AM), it effectively starts at 9AM
    start_time = otc_time
    if otc_time.hour >= 22:
        start_time = (otc_time + timedelta(days=1)).replace(hour=9, minute=0, second=0)
    elif otc_time.hour < 9:
        start_time = otc_time.replace(hour=9, minute=0, second=0)

    # 2. Calculate Raw Deadline
    deadline = start_time + timedelta(hours=PICK_TIME_HOURS)

    # 3. The "Overnight Jump"
    # If the 2-hour window crosses the9PM barrier, push the deadline by 11 hours
    # Example: Start 9:30 PM -> 2 hours later is 11:30 PM (crosses 10PM)
    # We add 11 hours to jump from 10PM to 9AM.
    if start_time.hour < 22 and deadline.hour >= 22 or (deadline.date() > start_time.date()):
        deadline += timedelta(hours=11)

# 4. Calculate Remaining Time
    # If currently in the freeze (10PM-9AM), we compare the deadline 
    # against the 9AM start time so the timer stays "paused."
    is_in_freeze = now.hour >= 22 or now.hour < 9
    
    if is_in_freeze:
        # The clock isn't running, so time remaining is fixed at its 9AM value
        remaining = deadline - start_time
    else:
        # The clock is running, so use the actual current time
        remaining = deadline - now
    return max(remaining, timedelta(0))

async def notify_admins(interaction: discord.Interaction, message: str):
    for admin_id in ADMIN_IDS:
        try:
            user = await bot.fetch_user(admin_id)
            await user.send(message)
        except Exception as e:
            print(f"Error notifying admin {admin_id}: {e}")

# --- SHARED PICK PROCESSING ENGINE ---

async def process_pick_logic(current_pick: Dict, prospect_id: int):
    """Refactored core engine used by both user /pick and admin /force."""
    prospect = draft_state["prospects"][prospect_id]
    team_info = draft_state["teams"].get(current_pick['team_id'])
    team_short_name = team_info.get('team_short', 'UNK')
    
    # Update Database
    
    now_iso = datetime.now(CENTRAL_TZ).isoformat()
    current_pick['player_id'] = prospect_id
    prospect['drafted'] = True
    current_pick['picked_at'] = now_iso
    draft_state["warning_sent"] = None

    gs_manager.update_pick(current_pick['id'], prospect_id, now_iso)
    gs_manager.update_prospect_drafted(prospect_id)

    otc, on_deck, in_hole = get_on_deck_and_in_hole()

    if otc:
        otc['otc_at'] = now_iso
        ws = gs_manager.get_worksheet("picks")
        next_pick_cell = ws.find(str(otc['id']), in_column=1)
        ws.update_cell(next_pick_cell.row, 3, now_iso) # Update otc_at for the new pick
    
    # Create Response
    embed = discord.Embed(
        title="🏈 Pick Made!",
        description=f"**{team_short_name}** | Pick {current_pick['id']}: **{prospect['f_name']} {prospect['l_name']}**",
        color=discord.Color.green()
    )
    embed.add_field(name="College", value=prospect['college'], inline=True)
    embed.add_field(name="Position", value=draft_state["positions"].get(prospect['position_id'], "N/A"), inline=True)
    embed.add_field(name="Ranking", value=prospect['ranking'], inline=True)
    
    if otc:
        next_team = draft_state["teams"][otc['team_id']]
        next_gm = draft_state["users"].get(next_team['gm_id']) if next_team else None
        if next_gm:
            message = f"🎙️ **On the Clock**: <@{next_gm.get('username', 'Unknown')}>\n"
            if on_deck:
                deck_team = draft_state["teams"][on_deck['team_id']]
                deck_gm = draft_state["users"].get(deck_team['gm_id'])
                if deck_gm:
                    message += f"📍 **On Deck**: <@{deck_gm.get('username', 'Unknown')}> ({deck_team.get('team_short', 'UNK')})\n"
                    if in_hole:
                        hole_team = draft_state["teams"][in_hole['team_id']]
                        hole_gm = draft_state["users"].get(hole_team['gm_id'])
                        message += f"🕳️ **In the Hole**: <@{hole_gm.get('username', 'Unknown')}> ({hole_team.get('team_short', 'UNK')})\n"
            embed.add_field(name="Next", value=message, inline=False)
    try: 
        draft_state["picks"] = gs_manager.load_picks()  # Refresh picks to reflect changes
        draft_state["prospects"] = gs_manager.load_prospects()  # Refresh prospects to reflect changes
        print("Data refreshed after pick." )
    except Exception as e:
        print(f"Error refreshing data after pick: {e}") 

    return embed

# --- COMMANDS ---

@bot.tree.command(name="pick", description="Make a pick in the draft")
async def pick_command(interaction: discord.Interaction, player_name: str):
    await interaction.response.defer()
    current_pick = get_current_pick()
    if not current_pick:
        await interaction.followup.send("❌ No picks remaining!")
        return
    
    if interaction.channel_id != PICK_CHANNEL_ID:
        await interaction.response.send_message(
            f"❌ Picks can only be made in <#{PICK_CHANNEL_ID}>.", 
            ephemeral=True
        )
        return
    
    current_team = draft_state["teams"][current_pick['team_id']]
    gm_user = draft_state["users"].get(current_team['gm_id'])
    # Compare against the unique Snowflake ID for accuracy
    if gm_user and str(gm_user['username']) != str(interaction.user.id):
        await interaction.followup.send(f"❌ This is not your pick! It is currently {gm_user['screen_name']}'s turn.")
        return
    
    name_parts = player_name.strip().split()
    if len(name_parts) < 2:
        await interaction.followup.send("❌ Please provide full name (First Last)")
        return
    
    f_name, l_name = name_parts[0], " ".join(name_parts[1:])
    prospect_id = find_prospect_by_name(f_name, l_name)
    
    if not prospect_id:
        await notify_admins(interaction, f"⚠️ '{player_name}' not found. Timer paused.")
        draft_state["timer_paused"] = True
        await interaction.followup.send("❌ Player not found! Admins notified.")
        return

    if draft_state["prospects"][prospect_id]['drafted']:
        await interaction.followup.send("❌ Player already drafted!")
        return

    result_embed = await process_pick_logic(current_pick, prospect_id)
    await interaction.followup.send(embed=result_embed)

@bot.tree.command(name="start_draft", description="Admin Only: Officially start the draft and the Pick 1 clock")
async def start_draft(interaction: discord.Interaction):
    if interaction.user.id not in ADMIN_IDS:
        await interaction.response.send_message("❌ Admin only!", ephemeral=True)
        return

    await interaction.response.defer()

    if draft_state["running"]:
        await interaction.followup.send("⚠️ The draft is already running!")
        return

    # 1. Get Pick #1
    first_pick = next((p for p in draft_state["picks"] if int(p['id']) == 1), None)
    if not first_pick:
        await interaction.followup.send("❌ Error: Could not find Pick #1 in the data.")
        return

    # 2. Update Timestamps
    now_iso = datetime.now(CENTRAL_TZ).isoformat()
    
    # Update Google Sheets
    try:
        
        ws = gs_manager.get_worksheet("picks")
        cell = ws.find("1", in_column=1) # Find Pick ID 1
        ws.update_cell(cell.row, 3, now_iso) # Column 4 is otc_at
        
        # Update Local State
        draft_state["running"] = True
        first_pick['otc_at'] = now_iso
        
        # 3. Identify the GM to ping
        team = draft_state["teams"].get(first_pick['team_id'])
        gm_info = draft_state["users"].get(team['gm_id']) if team else None
        
        embed = discord.Embed(
            title="🚀 The Draft has Officially Started!",
            description=f"Pick #1 is now **ON THE CLOCK**.",
            color=discord.Color.gold()
        )
        embed.add_field(name="Team", value=team['name'] if team else "Unknown", inline=True)
        
        msg = "🎉 **Let's get started!**"
        if gm_info:
            msg = f"🎉 **The Draft has begun!** <@{gm_info['username']}> you are OTC!"

        await interaction.followup.send(content=msg, embed=embed)
        
    except Exception as e:
        await interaction.followup.send(f"❌ Failed to start draft: {e}")

@bot.tree.command(name="force", description="Force a pick (admins only)")
async def force_command(interaction: discord.Interaction, player_name: str):
    if interaction.user.id not in ADMIN_IDS:
        await interaction.response.send_message("❌ Admin only!", ephemeral=True)
        return
    await interaction.response.defer()
    current_pick = get_current_pick()
    if not current_pick:
        await interaction.followup.send("❌ No picks remaining!")
        return
    name_parts = player_name.strip().split()
    f_name, l_name = name_parts[0], " ".join(name_parts[1:]) if len(name_parts) > 1 else ("", "")
    prospect_id = find_prospect_by_name(f_name, l_name)
    if not prospect_id:
        await interaction.followup.send(f"❌ Admin Error: Could not find '{player_name}'")
        return
    result_embed = await process_pick_logic(current_pick, prospect_id)
    result_embed.set_footer(text=f"Forced by Admin: {interaction.user.display_name}")
    await interaction.followup.send("⚠️ **ADMIN OVERRIDE**", embed=result_embed)

@bot.tree.command(name="timer", description="Check time remaining")
async def timer_command(interaction: discord.Interaction):
    current_pick = get_current_pick()
    if not current_pick:
        await interaction.response.send_message("❌ No active pick!")
        return
    time_remaining = get_time_remaining()
    current_team = draft_state["teams"][current_pick['team_id']]
    gm_user = draft_state["users"].get(current_team['gm_id'])
    hours, minutes = time_remaining.seconds // 3600, (time_remaining.seconds % 3600) // 60
    embed = discord.Embed(title="⏱️ Draft Timer", color=discord.Color.blue())
    embed.add_field(name="On the Clock", value=gm_user.get('screen_name', 'Unknown') if gm_user else "Unknown", inline=False)
    embed.add_field(name="Time Remaining", value=f"{hours}h {minutes}m", inline=False)
    if draft_state["timer_paused"]:
        embed.add_field(name="Status", value="⏸️ PAUSED", inline=False)
    await interaction.response.send_message(embed=embed)

@bot.tree.command(name="trade", description="Initiate a trade")
async def trade_command(interaction: discord.Interaction):
    current_pick = get_current_pick()
    if not current_pick:
        await interaction.response.send_message("❌ No Active Pick!", ephemeral=True)
        return
    draft_state["timer_paused"] = True
    draft_state["trade_in_progress"] = True
    await interaction.response.send_message("⏸️ Draft paused for trade. Notifying admins...")
    await notify_admins(interaction, f"🔄 Trade initiated by {interaction.user}")

@bot.tree.command(name="resume", description="Resume draft (admins only)")
async def resume_command(interaction: discord.Interaction):
    if interaction.user.id not in ADMIN_IDS:
        await interaction.response.send_message("❌ Admin only!", ephemeral=True)
        return
    draft_state["timer_paused"], draft_state["trade_in_progress"] = False, False
    current_pick = get_current_pick()
    if current_pick:
        gm = draft_state["users"].get(draft_state["teams"][current_pick['team_id']]['gm_id'])
        await interaction.response.send_message(f"▶️ Draft resumed! On clock: **{gm.get('screen_name') if gm else 'Unknown'}**")
    else:
        await interaction.response.send_message("▶️ Draft resumed!")

@bot.tree.command(name="order", description="Show draft order")
async def order_command(interaction: discord.Interaction):
    await interaction.response.defer()
    otc, on_deck, in_hole = get_on_deck_and_in_hole()
    embed = discord.Embed(title="📋 Draft Order", color=discord.Color.gold())
    if otc:
        team = draft_state["teams"][otc['team_id']]
        gm = draft_state["users"].get(team['gm_id'])
        time_rem = get_time_remaining()
        h, m = time_rem.seconds // 3600, (time_rem.seconds % 3600) // 60
        embed.add_field(name=f"Pick {otc['id']} - OTC", value=f"**{gm.get('screen_name', 'Unknown')}**\nTime: {h}h {m}m", inline=False)
    if on_deck:
        team = draft_state["teams"][on_deck['team_id']]
        gm = draft_state["users"].get(team['gm_id'])
        embed.add_field(name=f"Pick {on_deck['id']} - On Deck", value=f"**{gm.get('screen_name', 'Unknown')}**", inline=False)
    if in_hole:
        team = draft_state["teams"][in_hole['team_id']]
        gm = draft_state["users"].get(team['gm_id'])
        embed.add_field(name=f"Pick {in_hole['id']} - In The Hole", value=f"**{gm.get('screen_name', 'Unknown')}**", inline=False)
    await interaction.followup.send(embed=embed)

@bot.tree.command(name="best", description="Show top available")
@app_commands.choices(position=[
    app_commands.Choice(name="QB", value=1), 
    app_commands.Choice(name="RB", value=2), 
    app_commands.Choice(name="WR", value=3), 
    app_commands.Choice(name="TE", value=4), 
    app_commands.Choice(name="OL", value=5),
    app_commands.Choice(name="OT", value=6),
    app_commands.Choice(name="OG", value=7),
    app_commands.Choice(name="C", value=8), 
    app_commands.Choice(name="DL", value=9),
    app_commands.Choice(name="DE", value=10),
    app_commands.Choice(name="DT", value=11),
    app_commands.Choice(name="EDGE", value=12), 
    app_commands.Choice(name="LB", value=13), 
    app_commands.Choice(name="CB", value=14), 
    app_commands.Choice(name="S", value=15), 
    app_commands.Choice(name="K", value=16), 
    app_commands.Choice(name="P", value=17), 
    app_commands.Choice(name="LS", value=18), 
    app_commands.Choice(name="IOL", value=19)])
@app_commands.describe(private="If true, only you can see the response")
async def best_command(interaction: discord.Interaction, position: Optional[app_commands.Choice[int]] = None, private: bool = True):
    undrafted = [p for p in draft_state["prospects"].values() if not p['drafted']]
    if position:
        undrafted = [p for p in undrafted if int(p['position_id']) == position.value]
    top_10 = sorted(undrafted, key=lambda x: x['ranking'])[:10]
    if not top_10:
        await interaction.response.send_message("No players found!", ephemeral=True)
        return
    embed = discord.Embed(title="🌟 Top 10 Available", color=discord.Color.purple())
    for i, p in enumerate(top_10, 1):
        pos = draft_state["positions"].get(p['position_id'], "N/A")
        embed.add_field(name=f"{i}. {p['f_name']} {p['l_name']}", value=f"**{pos}** | {p['college']} | Rank: {p['ranking']}", inline=False)
    await interaction.response.send_message(embed=embed)

@bot.tree.command(name="review", description="Review team picks")
@app_commands.describe(acronym="2-3 Letter acronym of team (e.g. KC)")
async def review_command(interaction: discord.Interaction, acronym: str):
    await interaction.response.defer()

    search_term = acronym.strip().upper()

    team_data = next(
        ((tid, t) for tid, t in draft_state["teams"].items() if t.get('team_short', '').upper() == search_term),(None, None))
    team_id, team = team_data
    if not team:
        await interaction.followup.send("❌ Team acronym '**{search_term}**' not found. Please check your input and try again.")
        return
        
    team_picks = [p for p in draft_state["picks"] if str(p['team_id']) == str(team_id) and p.get('player_id')]

    embed = discord.Embed(title=f"📊 {team['team_short']} Review", color=discord.Color.blue())
    embed.set_footer(text=f"{team['conference']}  |  {team['division']}")

    if team_picks:
        for p in team_picks:
            prospect = draft_state["prospects"].get(p['player_id'])
            if prospect:
                pos = draft_state["positions"].get(prospect['position_id'], "N/A")
                embed.add_field(name=f"Pick {p['id']}: {prospect['f_name']} {prospect['l_name']}", value=f"**{pos}** | {prospect['college']}", inline=False)
    else:
        embed.add_field(name="Players", value="None drafted yet", inline=False)
    await interaction.followup.send(embed=embed)

@bot.tree.command(name="trade_picks", description="Admin Only: Swap multiple picks between two teams")
@app_commands.describe(
    team_a_short="Acronym for first team (e.g. KC)",
    team_a_picks="Comma separated IDs (e.g. 1, 45)",
    team_b_short="Acronym for second team (e.g. SF)",
    team_b_picks="Comma separated IDs (e.g. 12, 80)"
)
async def trade_picks(
    interaction: discord.Interaction, 
    team_a_short: str, 
    team_a_picks: str, 
    team_b_short: str, 
    team_b_picks: str
):
    if interaction.user.id not in ADMIN_IDS:
        await interaction.response.send_message("❌ Admin only!", ephemeral=True)
        return

    await interaction.response.defer()

    # 1. Map Short Names to IDs
    def get_id_from_short(short_name):
        for tid, info in draft_state["teams"].items():
            if info['team_short'].upper() == short_name.strip().upper():
                return tid
        return None

    team_a_id = get_id_from_short(team_a_short)
    team_b_id = get_id_from_short(team_b_short)

    if not team_a_id or not team_b_id:
        missing = team_a_short if not team_a_id else team_b_short
        await interaction.followup.send(f"❌ Error: Team acronym '{missing}' not found.")
        return    

    # 2. Parse the strings into clean integer lists
    try:
        list_a = [int(p.strip()) for p in team_a_picks.split(",")]
        list_b = [int(p.strip()) for p in team_b_picks.split(",")]
    except ValueError:
        await interaction.followup.send("❌ Error: Pick lists must be numbers separated by commas.")
        return

    # 3. Validation Helper
    def validate_ownership(pick_ids, expected_team_id):
        for pid in pick_ids:
            pick = next((p for p in draft_state["picks"] if str(p['id']) == str(pid)), None)
            if not pick:
                return f"Pick {pid} does not exist."
            if str(pick['team_id']) != str(expected_team_id):
                actual_team = draft_state["teams"].get(pick['team_id'], {}).get('team_short', 'UNK')
                return f"Pick {pid} belongs to {actual_team}, not {expected_team_id}."
            if pick['player_id'] not in [None, "", "None", 0, "0"]:
                return f"Pick {pid} has already been used!"
        return None

    error_a = validate_ownership(list_a, team_a_id)
    error_b = validate_ownership(list_b, team_b_id)

    if error_a or error_b:
        await interaction.followup.send(f"❌ Trade Failed: {error_a or error_b}")
        return

    # 4. Execution
    ws = gs_manager.get_worksheet("picks")
    cells_to_update = []
    local_updates = []
    otc_pick = get_current_pick()
    now_iso = datetime.now(CENTRAL_TZ).isoformat()

    # Helper to prepare updates without sending them yet
    def prepare_trade_data(pick_ids, new_team_id):
        for pid in pick_ids:
            # 1. Find the row in the sheet
            try:
                cell = ws.find(str(pid), in_column=1)
                # We want to update Column 2 (team_id) for this row
                # We fetch the cell object for that specific coordinate
                cells_to_update.append(Cell(row=cell.row, col=2, value=new_team_id))

                # Reset Timer if this pick is currently OTC
                if otc_pick and str(otc_pick['id']) == str(pid):
                    cells_to_update.append(Cell(row=cell.row, col=3, value=now_iso))

                    otc_pick['otc_at'] = now_iso  # Update local state for timer reset
                    draft_state["warning_sent"] = None  # Reset warning flag since timer is effectively restarted
                
                # Store local state change for later
                local_updates.append((pid, new_team_id))
            except gspread.exceptions.CellNotFound:
                print(f"Error: Pick ID {pid} not found in sheet.")

    # Prepare both sides of the trade
    prepare_trade_data(list_a, team_b_id)
    prepare_trade_data(list_b, team_a_id)

    if cells_to_update:
        try:
            # This is the optimization: One network request for all cells
            ws.update_cells(cells_to_update)
            draft_state["picks"] = gs_manager.load_picks()
            print("Picks synced successfully after trade.")

            current_pick = next((p for p in draft_state["picks"] if p.get('player_id') in [None, "", "None"]), None)

            if current_pick:
                # 2. Check if the current OTC pick was part of the trade
                all_traded_picks = list_a + list_b
                if int(current_pick['id']) in [int(p) for p in all_traded_picks]:
                    # 3. If it was traded, trigger a "New Team is OTC" message
                    new_team = draft_state["teams"].get(current_pick['team_id'])
                    new_gm_id = new_team.get('gm_id') if new_team else None
                    
                    otc_embed = discord.Embed(
                        title="⏱️ Order of Play Updated",
                        description=f"Due to the trade, **<@{new_gm_id}>** is now **On the Clock** for Pick {current_pick['id']}!",
                        color=discord.Color.blue()
                    )
                    
                    if new_gm_id:
                        # Mention the new GM so they get a notification
                        await interaction.channel.send(content=f"🔔 <@{new_gm_id}>, you're up!", embed=otc_embed)
                    else:
                        await interaction.channel.send(embed=otc_embed)
            
        except Exception as e:
            await interaction.followup.send(f"❌ API Error: Could not sync to Sheets. {e}")
            return

    # 5. Success Message
    embed = discord.Embed(title="🤝 Trade Executed!", color=discord.Color.gold())
    embed.add_field(name=f"Sent to {team_b_short.upper()}", value=f"Picks: {team_a_picks}", inline=True)
    embed.add_field(name=f"Sent to {team_a_short.upper()}", value=f"Picks: {team_b_picks}", inline=True)
    
    await interaction.followup.send(embed=embed)

@bot.tree.command(name="reverse_pick", description="Admin Only: Undo a specific pick and restart its clock")
@app_commands.describe(pick_id="The number of the pick to undo")
async def reverse_pick(interaction: discord.Interaction, pick_id: int):
    if interaction.user.id not in ADMIN_IDS:
        await interaction.response.send_message("❌ Admin only!", ephemeral=True)
        return

    await interaction.response.defer()

    # 1. Find the pick in state
    pick = next((p for p in draft_state["picks"] if str(p['id']) == str(pick_id)), None)
    
    if not pick:
        await interaction.followup.send(f"❌ Pick {pick_id} not found.")
        return
    
    if not pick['player_id']:
        await interaction.followup.send(f"❌ Pick {pick_id} hasn't been made yet.")
        return

    # 2. Get Player Info before clearing
    player_id = pick['player_id']
    prospect = draft_state["prospects"].get(player_id)
    team_short = draft_state["teams"].get(pick['team_id'], {}).get('team_short', 'UNK')

    # 3. Update Google Sheets
    
    
    # Reset the Pick Row
    now_iso = datetime.now(CENTRAL_TZ).isoformat()
    p_ws = gs_manager.get_worksheet("picks")
    p_cell = p_ws.find(str(pick_id), in_column=1)
    p_ws.update_cell(p_cell.row, 4, "")       # Clear player_id
    restart_time = datetime.now(CENTRAL_TZ).isoformat()
    p_ws.update_cell(p_cell.row, 3, restart_time)
    pick['otc_at'] = restart_time
    # Also reset the warning flag so the 30-minute warning can trigger again
    draft_state["warning_sent"] = None
    p_ws.update_cell(p_cell.row, 5, "")       # Clear picked_at
    
    # Reset the Prospect Row
    pr_ws = gs_manager.get_worksheet("prospects")
    pr_cell = pr_ws.find(str(player_id), in_column=1)
    pr_ws.update_cell(pr_cell.row, 7, "FALSE") # drafted = FALSE

    # 4. Update Local State
    pick['player_id'] = None
    pick['picked_at'] = None
    pick['otc_at'] = now_iso
    pick['clock_expire'] = False  # Ensuring timer isn't dead
    
    if prospect:
        prospect['drafted'] = False

    # 5. Confirmation
    embed = discord.Embed(
        title="↩️ Pick Reversed",
        description=f"Pick {pick_id} for **{team_short}** has been reset.",
        color=discord.Color.orange()
    )
    if prospect:
        embed.add_field(name="Player Released", value=f"{prospect['f_name']} {prospect['l_name']}", inline=True)
    embed.add_field(name="Timer Status", value="Clock restarted (2 Hours)", inline=True)
    embed.set_footer(text=f"Action by {interaction.user.display_name}")

    await interaction.followup.send(embed=embed)

@bot.tree.command(name="great", description="who is great?")
async def great(interaction: discord.Interaction):
    await interaction.response.send_message("Craig is Great!")

@bot.tree.command(name="sync", description="Admin Only: Refresh all data from Google Sheets")
async def sync_command(interaction: discord.Interaction):
    if interaction.user.id not in ADMIN_IDS:
        await interaction.response.send_message("❌ Admin only!", ephemeral=True)
        return

    await interaction.response.defer(ephemeral=True)
    try:
        # Clear local cache and reload fresh from Sheets
        draft_state["picks"].clear()
        await load_data()
        # Reset the index to find the new current pick
        draft_state["current_pick_index"] = 0
        await interaction.followup.send("✅ Data successfully synced from Google Sheets!")
    except Exception as e:
        await interaction.followup.send(f"❌ Sync failed: {e}")

@bot.tree.command(name="draft-status", description="Admin Only: Check internal state for debugging")
async def draft_status_command(interaction: discord.Interaction):
    if interaction.user.id not in ADMIN_IDS:
        await interaction.response.send_message("❌ Admin only!", ephemeral=True)
        return

    # 1. Determine Operational Status
    now = datetime.now(CENTRAL_TZ)
    is_frozen = now.hour >= 22 or now.hour < 9
    
    if draft_state.get("timer_paused"):
        status_text = "⏸️ **PAUSED** (Manual or Trade)"
        status_color = discord.Color.orange()
    elif not draft_state.get("running"):
        status_text = "💤 **NOT STARTED**"
        status_color = discord.Color.light_grey()
    elif is_frozen:
        status_text = "❄️ **FROZEN** (Overnight Break)"
        status_color = discord.Color.blue()
    else:
        status_text = "🟢 **ACTIVE**"
        status_color = discord.Color.green()

    # 2. Get Current Pick Info
    current_pick = get_current_pick()
    time_rem = get_time_remaining()
    
    # Calculate hours and minutes for display
    # (Using // 3600 and % 3600 // 60 from your logic)
    h, m = time_rem.seconds // 3600, (time_rem.seconds % 3600) // 60
    
    # 3. Build the Embed
    embed = discord.Embed(title="⚙️ System Status Report", color=status_color)
    embed.add_field(name="Draft Status", value=status_text, inline=False)
    
    if current_pick:
        team = draft_state["teams"].get(current_pick['team_id'], {})
        gm = draft_state["users"].get(team.get('gm_id'), {})   
        # Count picks that have a player_id assigned (meaning they are completed)
        completed_picks = sum(1 for p in draft_state["picks"] if not is_empty(p.get('player_id')))
        
        # Count prospects where is_drafted is specifically True
        drafted_prospects = sum(1 for pr in draft_state["prospects"].values() if pr.get('is_drafted') is True)

        pick_val = (
            f"**Current Pick:** #{current_pick['id']}\n"
            f"**Team:** {team.get('team_short', 'UNK')} ({team.get('name', 'Unknown')})\n"
            f"**Clock:** {h}h {m}m remaining\n"
            f"**Picks Completed:** {completed_picks}\n"
            f"**Prospects Drafted:** {drafted_prospects}"
        )
        embed.add_field(name="Current Pick Details", value=pick_val, inline=False)
    else:
        embed.add_field(name="Current Pick", value="None (Draft likely complete)", inline=False)

    # 4. Data Integrity Info
    sync_time = draft_state.get("last_sync", "Never")
    data_counts = (
        f"Teams: {len(draft_state['teams'])}\n"
        f"Prospects: {len(draft_state['prospects'])}\n"
        f"Picks: {len(draft_state['picks'])}"
    )
    embed.add_field(name="Data Counts", value=data_counts, inline=True)
    embed.add_field(name="Last Sync", value=f"🕒 {sync_time} CT", inline=True)

    await interaction.response.send_message(embed=embed, ephemeral=True)

if __name__ == "__main__":
    TOKEN = os.getenv("DISCORD_TOKEN")
    if TOKEN:
        bot.run(TOKEN)
    else:
        print("Critical Error: DISCORD_TOKEN not found in environment")