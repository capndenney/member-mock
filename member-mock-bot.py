import os
import discord
from discord.ext import commands, tasks
from discord import app_commands
import gspread
from google.oauth2.service_account import Credentials
from datetime import datetime, timedelta
import pytz
import asyncio
from typing import Optional, List, Dict

# --- INITIALIZATION ---
intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)

# Configuration
GOOGLE_SHEETS_CREDENTIALS = "credentials.json"
SHEET_ID = "YOUR_SHEET_ID_HERE"
ADMIN_USERS = [123456789] 
CENTRAL_TZ = pytz.timezone('America/Chicago')
PICK_TIME_HOURS = 2

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
    "warning_sent": None
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
    channel_id = int(os.getenv("CHANNEL_ID", 0))

    # If 30 minutes or less (but more than 0)
    if timedelta(minutes=0) < time_remaining <= timedelta(minutes=30):
        current_team = draft_state["teams"].get(current_pick['team_id'])
        gm_user_info = draft_state["users"].get(current_team['gm_id'])
        
        if gm_user_info:
            # We assume your 'username' in the sheet is the Discord User ID (int) 
            # or a string we can fetch. Adjust fetch_user as needed.
            try:
                # Find the main draft channel (Replace with your Channel ID)
                channel = bot.get_channel(channel_id) 
                
                mention = f"<@{gm_user_info['username']}>"
                await channel.send(
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

# --- DATA MANAGEMENT ---

class GoogleSheetsManager:
    def __init__(self, credentials_file: str, sheet_id: str):
        scope = ["https://spreadsheets.google.com/auth/spreadsheets"]
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
                'timezone': row['timezone']
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
                'player_id': row.get('player_id'),
                'otc_at': row.get('otc_at'),
                'clock_expire': row.get('clock_expire') == 'TRUE',
                'picked_at': row.get('picked_at')
            })
        return sorted(picks, key=lambda x: x['id'])
    
    def load_positions(self) -> Dict:
        ws = self.get_worksheet("positions")
        positions = {}
        for row in ws.get_all_records():
            positions[row['id']] = row['position']
        return positions
    
    def update_prospect_drafted(self, prospect_id: int):
        ws = self.get_worksheet("prospects")
        cell = ws.find(str(prospect_id), in_column=1)
        ws.update_cell(cell.row, 6, "TRUE")
    
    def update_pick(self, pick_id: int, player_id: int, picked_at: str):
        ws = self.get_worksheet("picks")
        cell = ws.find(str(pick_id), in_column=1)
        ws.update_cell(cell.row, 3, player_id)
        ws.update_cell(cell.row, 5, picked_at)

# --- LOGIC HELPERS ---

async def load_data():
    try:
        gs = GoogleSheetsManager(GOOGLE_SHEETS_CREDENTIALS, SHEET_ID)
        draft_state["teams"] = gs.load_teams()
        draft_state["users"] = gs.load_users()
        draft_state["prospects"] = gs.load_prospects()
        draft_state["picks"] = gs.load_picks()
        draft_state["positions"] = gs.load_positions()
    except Exception as e:
        print(f"Error loading data: {e}")

def find_prospect_by_name(first_name: str, last_name: str) -> Optional[int]:
    for pid, prospect in draft_state["prospects"].items():
        if (prospect['f_name'].lower() == first_name.lower() and 
            prospect['l_name'].lower() == last_name.lower()):
            return pid
    return None

def get_current_pick() -> Optional[Dict]:
    undrafted_picks = [p for p in draft_state["picks"] if p['player_id'] is None]
    return undrafted_picks[0] if undrafted_picks else None

def get_on_deck_and_in_hole() -> tuple:
    undrafted_picks = [p for p in draft_state["picks"] if p['player_id'] is None]
    on_deck = undrafted_picks[1] if len(undrafted_picks) > 1 else None
    in_hole = undrafted_picks[2] if len(undrafted_picks) > 2 else None
    return on_deck, in_hole

def get_time_remaining() -> timedelta:
    now = datetime.now(CENTRAL_TZ)
    current_pick = get_current_pick()
    if not current_pick or not current_pick['otc_at']:
        return timedelta(hours=PICK_TIME_HOURS)

    otc_time = datetime.fromisoformat(current_pick['otc_at']).astimezone(CENTRAL_TZ)
    
    # 1. Handle Overnight Freeze: If it's currently between 10PM and 9AM
    # We treat "now" as exactly 9:00 AM so the timer doesn't move.
    effective_now = now
    if now.hour >= 22:
        effective_now = (now + timedelta(days=1)).replace(hour=9, minute=0, second=0, microsecond=0)
    elif now.hour < 9:
        effective_now = now.replace(hour=9, minute=0, second=0, microsecond=0)

    # 2. Calculate Deadline
    # If the pick happened AFTER 10PM or BEFORE 9AM, its clock starts at 9AM.
    effective_otc = otc_time
    if otc_time.hour >= 22:
        effective_otc = (otc_time + timedelta(days=1)).replace(hour=9, minute=0, second=0, microsecond=0)
    elif otc_time.hour < 9:
        effective_otc = otc_time.replace(hour=9, minute=0, second=0, microsecond=0)

    deadline = effective_otc + timedelta(hours=PICK_TIME_HOURS)
    
    remaining = deadline - effective_now
    return max(remaining, timedelta(0))

async def notify_admins(interaction: discord.Interaction, message: str):
    for admin_id in ADMIN_USERS:
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
    gs = GoogleSheetsManager(GOOGLE_SHEETS_CREDENTIALS, SHEET_ID)
    now_iso = datetime.now(CENTRAL_TZ).isoformat()
    gs.update_pick(current_pick['id'], prospect_id, now_iso)
    gs.update_prospect_drafted(prospect_id)
    
    # Update State
    current_pick['player_id'] = prospect_id
    prospect['drafted'] = True
    
    # Create Response
    embed = discord.Embed(
        title="🏈 Pick Made!",
        description=f"**{team_short_name}** | Pick {current_pick['id']}: **{prospect['f_name']} {prospect['l_name']}**",
        color=discord.Color.green()
    )
    embed.add_field(name="College", value=prospect['college'], inline=True)
    embed.add_field(name="Position", value=draft_state["positions"].get(prospect['position_id'], "N/A"), inline=True)
    embed.add_field(name="Ranking", value=prospect['ranking'], inline=True)

    draft_state["warning_sent"] = None  # Reset any warnings on successful pick
    
    on_deck, in_hole = get_on_deck_and_in_hole()
    if on_deck:
        next_team = draft_state["teams"][on_deck['team_id']]
        next_gm = draft_state["users"].get(next_team['gm_id'])
        if next_gm:
            message = f"🎙️ **On the Clock**: <@{next_gm.get('username', 'Unknown')}>\n"
            if in_hole:
                hole_team = draft_state["teams"][in_hole['team_id']]
                hole_gm = draft_state["users"].get(hole_team['gm_id'])
                if hole_gm:
                    message += f"📍 **On Deck**: {draft_state['teams'][on_deck['team_id']].get('team_short')}\n"
                    message += f"🕳️ **In the Hole**: {hole_gm.get('screen_name', 'Unknown')}"
            embed.add_field(name="Next", value=message, inline=False)
    return embed

# --- COMMANDS ---

@bot.event
async def on_ready():
    await load_data()
    await bot.tree.sync()
    print(f"{bot.user} has connected to Discord!")

@bot.tree.command(name="pick", description="Make a pick in the draft")
async def pick_command(interaction: discord.Interaction, player_name: str):
    await interaction.response.defer()
    current_pick = get_current_pick()
    if not current_pick:
        await interaction.followup.send("❌ No picks remaining!")
        return
    
    current_team = draft_state["teams"][current_pick['team_id']]
    gm_user = draft_state["users"].get(current_team['gm_id'])
    if gm_user and gm_user['username'] != str(interaction.user):
        await interaction.followup.send("❌ This is not your pick!")
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
    if interaction.user.id not in ADMIN_USERS:
        await interaction.response.send_message("❌ Admin only!", ephemeral=True)
        return

    await interaction.response.defer()

    if draft_state["running"]:
        await interaction.followup.send("⚠️ The draft is already running!")
        return

    # 1. Get Pick #1
    first_pick = next((p for p in draft_state["picks"] if p['id'] == 1), None)
    if not first_pick:
        await interaction.followup.send("❌ Error: Could not find Pick #1 in the data.")
        return

    # 2. Update Timestamps
    now_iso = datetime.now(CENTRAL_TZ).isoformat()
    
    # Update Google Sheets
    try:
        gs = GoogleSheetsManager(GOOGLE_SHEETS_CREDENTIALS, SHEET_ID)
        ws = gs.get_worksheet("picks")
        cell = ws.find("1", in_column=1) # Find Pick ID 1
        ws.update_cell(cell.row, 4, now_iso) # Column 4 is otc_at
        
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
    if interaction.user.id not in ADMIN_USERS:
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
    if interaction.user.id not in ADMIN_USERS:
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
    current_pick = get_current_pick()
    on_deck, in_hole = get_on_deck_and_in_hole()
    embed = discord.Embed(title="📋 Draft Order", color=discord.Color.gold())
    if current_pick:
        team = draft_state["teams"][current_pick['team_id']]
        gm = draft_state["users"].get(team['gm_id'])
        time_rem = get_time_remaining()
        h, m = time_rem.seconds // 3600, (time_rem.seconds % 3600) // 60
        embed.add_field(name=f"Pick {current_pick['id']} - OTC", value=f"**{gm.get('screen_name', 'Unknown')}**\nTime: {h}h {m}m", inline=False)
    if on_deck:
        team = draft_state["teams"][on_deck['team_id']]
        gm = draft_state["users"].get(team['gm_id'])
        embed.add_field(name=f"Pick {on_deck['id']} - On Deck", value=f"**{gm.get('screen_name', 'Unknown')}**", inline=False)
    if in_hole:
        team = draft_state["teams"][in_hole['team_id']]
        gm = draft_state["users"].get(team['gm_id'])
        embed.add_field(name=f"Pick {in_hole['id']} - In Hole", value=f"**{gm.get('screen_name', 'Unknown')}**", inline=False)
    await interaction.response.send_message(embed=embed)

@bot.tree.command(name="best", description="Show top available")
@app_commands.choices(position=[app_commands.Choice(name="QB", value="1"), app_commands.Choice(name="RB", value="2"), app_commands.Choice(name="WR", value="3"), app_commands.Choice(name="TE", value="4"), app_commands.Choice(name="OL", value="5"), app_commands.Choice(name="DL", value="9"), app_commands.Choice(name="LB", value="13"), app_commands.Choice(name="CB", value="14"), app_commands.Choice(name="S", value="15")])
async def best_command(interaction: discord.Interaction, position: Optional[app_commands.Choice[int]] = None):
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
async def review_command(interaction: discord.Interaction, team_name: str):
    team_id = next((tid for tid, t in draft_state["teams"].items() if team_name.lower() in str(tid).lower()), None)
    if team_id is None:
        await interaction.response.send_message("❌ Invalid team!")
        return
    team = draft_state["teams"][team_id]
    picks = [p for p in draft_state["picks"] if p['team_id'] == team_id and p['player_id']]
    embed = discord.Embed(title=f"📊 {team['division']} Review", color=discord.Color.blue())
    if picks:
        for p in picks:
            prospect = draft_state["prospects"].get(p['player_id'])
            pos = draft_state["positions"].get(prospect['position_id'], "N/A")
            embed.add_field(name=f"Pick {p['id']}: {prospect['f_name']} {prospect['l_name']}", value=f"**{pos}** | {prospect['college']}", inline=False)
    else:
        embed.add_field(name="Players", value="None drafted yet", inline=False)
    await interaction.response.send_message(embed=embed)

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
    if interaction.user.id not in ADMIN_USERS:
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
            pick = next((p for p in draft_state["picks"] if p['id'] == pid), None)
            if not pick:
                return f"Pick {pid} does not exist."
            if str(pick['team_id']) != str(expected_team_id):
                actual_team = draft_state["teams"].get(pick['team_id'], {}).get('team_short', 'UNK')
                return f"Pick {pid} belongs to {actual_team}, not {expected_team_id}."
            if pick['player_id'] is not None:
                return f"Pick {pid} has already been used!"
        return None

    error_a = validate_ownership(list_a, team_a_id)
    error_b = validate_ownership(list_b, team_b_id)

    if error_a or error_b:
        await interaction.followup.send(f"❌ Trade Failed: {error_a or error_b}")
        return

    # 4. Execution
    gs = GoogleSheetsManager(GOOGLE_SHEETS_CREDENTIALS, SHEET_ID)
    ws = gs.get_worksheet("picks")
    
    def swap_ownership(pick_ids, new_team_id):
        for pid in pick_ids:
            # Update Local State
            pick = next(p for p in draft_state["picks"] if p['id'] == pid)
            pick['team_id'] = new_team_id
            # Update Google Sheet
            cell = ws.find(str(pid), in_column=1)
            ws.update_cell(cell.row, 2, new_team_id)

    swap_ownership(list_a, team_b_id)
    swap_ownership(list_b, team_a_id)

    # 5. Success Message
    embed = discord.Embed(title="🤝 Trade Executed!", color=discord.Color.gold())
    embed.add_field(name=f"Sent to {team_b_short.upper()}", value=f"Picks: {team_a_picks}", inline=True)
    embed.add_field(name=f"Sent to {team_a_short.upper()}", value=f"Picks: {team_b_picks}", inline=True)
    
    await interaction.followup.send(embed=embed)

@bot.tree.command(name="reverse_pick", description="Admin Only: Undo a specific pick and restart its clock")
@app_commands.describe(pick_id="The number of the pick to undo")
async def reverse_pick(interaction: discord.Interaction, pick_id: int):
    if interaction.user.id not in ADMIN_USERS:
        await interaction.response.send_message("❌ Admin only!", ephemeral=True)
        return

    await interaction.response.defer()

    # 1. Find the pick in state
    pick = next((p for p in draft_state["picks"] if p['id'] == pick_id), None)
    
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
    gs = GoogleSheetsManager(GOOGLE_SHEETS_CREDENTIALS, SHEET_ID)
    
    # Reset the Pick Row
    now_iso = datetime.now(CENTRAL_TZ).isoformat()
    p_ws = gs.get_worksheet("picks")
    p_cell = p_ws.find(str(pick_id), in_column=1)
    p_ws.update_cell(p_cell.row, 3, "")       # Clear player_id
    p_ws.update_cell(p_cell.row, 4, now_iso)  # Reset otc_at to NOW
    p_ws.update_cell(p_cell.row, 5, "")       # Clear picked_at
    
    # Reset the Prospect Row
    pr_ws = gs.get_worksheet("prospects")
    pr_cell = pr_ws.find(str(player_id), in_column=1)
    pr_ws.update_cell(pr_cell.row, 6, "FALSE") # drafted = FALSE

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
    if interaction.user.id not in ADMIN_USERS:
        await interaction.response.send_message("❌ Admin only!", ephemeral=True)
        return

    await interaction.response.defer(ephemeral=True)
    try:
        await load_data()
        await interaction.followup.send("✅ Data successfully synced from Google Sheets!")
    except Exception as e:
        await interaction.followup.send(f"❌ Sync failed: {e}")

TOKEN = os.getenv("DISCORD_TOKEN")
bot.run(TOKEN)