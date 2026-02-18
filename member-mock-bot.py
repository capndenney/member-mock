import discord
from discord.ext import commands, tasks
from discord import app_commands
import gspread
from google.oauth2.service_account import Credentials
from datetime import datetime, timedelta
import pytz
import asyncio
from typing import Optional, List, Dict

# Initialize bot
intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)

# Configuration
GOOGLE_SHEETS_CREDENTIALS = "credentials.json"  # Path to Google service account JSON
SHEET_ID = "YOUR_SHEET_ID_HERE"  # Replace with your Google Sheet ID
ADMIN_USERS = [123456789]  # Replace with actual Discord user IDs
CENTRAL_TZ = pytz.timezone('America/Chicago')
PICK_TIME_HOURS = 8

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
    "positions": {}
}

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
                'division': row['division'],
                'conference': row['conference'],
                'gm_id': row['gm_id']
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
        ws.update_cell(cell.row, 6, "TRUE")  # drafted column
    
    def update_pick(self, pick_id: int, player_id: int, picked_at: str):
        ws = self.get_worksheet("picks")
        cell = ws.find(str(pick_id), in_column=1)
        ws.update_cell(cell.row, 3, player_id)  # player_id column
        ws.update_cell(cell.row, 5, picked_at)  # picked_at column

async def load_data():
    """Load all data from Google Sheets"""
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
    """Find prospect ID by name"""
    for pid, prospect in draft_state["prospects"].items():
        if (prospect['f_name'].lower() == first_name.lower() and 
            prospect['l_name'].lower() == last_name.lower()):
            return pid
    return None

def get_current_pick() -> Optional[Dict]:
    """Get the current pick on the clock"""
    undrafted_picks = [p for p in draft_state["picks"] if p['player_id'] is None]
    if undrafted_picks:
        return undrafted_picks[0]
    return None

def get_on_deck_and_in_hole() -> tuple:
    """Get the next two picks (on deck and in hole)"""
    undrafted_picks = [p for p in draft_state["picks"] if p['player_id'] is None]
    on_deck = undrafted_picks[1] if len(undrafted_picks) > 1 else None
    in_hole = undrafted_picks[2] if len(undrafted_picks) > 2 else None
    return on_deck, in_hole

def get_time_remaining() -> timedelta:
    """Calculate time remaining on clock (considering 7 AM - 11 PM Central constraint)"""
    now = datetime.now(CENTRAL_TZ)
    current_pick = get_current_pick()
    
    if not current_pick or not current_pick['otc_at']:
        return timedelta(hours=PICK_TIME_HOURS)
    
    otc_time = datetime.fromisoformat(current_pick['otc_at']).astimezone(CENTRAL_TZ)
    deadline = otc_time + timedelta(hours=PICK_TIME_HOURS)
    
    # Adjust for business hours constraint
    if now.hour < 7:  # Before 7 AM
        adjusted_deadline = now.replace(hour=7, minute=0, second=0) + timedelta(hours=PICK_TIME_HOURS)
        return adjusted_deadline - now
    elif now.hour >= 23:  # After 11 PM
        next_day_start = (now + timedelta(days=1)).replace(hour=7, minute=0, second=0)
        adjusted_deadline = next_day_start + timedelta(hours=PICK_TIME_HOURS)
        return adjusted_deadline - now
    
    return max(deadline - now, timedelta(0))

async def notify_admins(interaction: discord.Interaction, message: str):
    """Notify all admin users"""
    for admin_id in ADMIN_USERS:
        try:
            user = await bot.fetch_user(admin_id)
            await user.send(message)
        except Exception as e:
            print(f"Error notifying admin {admin_id}: {e}")

@bot.event
async def on_ready():
    await load_data()
    await bot.tree.sync()
    print(f"{bot.user} has connected to Discord!")

@bot.tree.command(name="pick", description="Make a pick in the draft")
@app_commands.describe(player_name="Full name of the player (First Last)")
async def pick_command(interaction: discord.Interaction, player_name: str):
    await interaction.response.defer()
    
    current_pick = get_current_pick()
    
    if not current_pick:
        await interaction.followup.send("❌ No picks remaining!")
        return
    
    # Verify user owns the current pick
    current_team = draft_state["teams"][current_pick['team_id']]
    gm_user = draft_state["users"].get(current_team['gm_id'])
    
    if gm_user and gm_user['username'] != str(interaction.user):
        await interaction.followup.send("❌ This is not your pick!")
        return
    
    # Parse player name
    name_parts = player_name.strip().split()
    if len(name_parts) < 2:
        await interaction.followup.send("❌ Please provide full name (First Last)")
        return
    
    first_name, last_name = name_parts[0], " ".join(name_parts[1:])
    prospect_id = find_prospect_by_name(first_name, last_name)
    
    if not prospect_id:
        await notify_admins(interaction, 
            f"⚠️ Player '{player_name}' not found in database! Pick paused.")
        await interaction.followup.send("❌ Player not found! Admins have been notified.")
        draft_state["timer_paused"] = True
        return
    
    prospect = draft_state["prospects"][prospect_id]
    
    if prospect['drafted']:
        await interaction.followup.send(f"❌ {player_name} has already been drafted!")
        return
    
    # Update pick and prospect
    gs = GoogleSheetsManager(GOOGLE_SHEETS_CREDENTIALS, SHEET_ID)
    now = datetime.now(CENTRAL_TZ).isoformat()
    gs.update_pick(current_pick['id'], prospect_id, now)
    gs.update_prospect_drafted(prospect_id)
    
    # Update draft state
    current_pick['player_id'] = prospect_id
    prospect['drafted'] = True
    
    # Build response message
    on_deck, in_hole = get_on_deck_and_in_hole()
    embed = discord.Embed(
        title="🏈 Pick Made!",
        description=f"{current_pick['id']}. **{prospect['f_name']} {prospect['l_name']}**",
        color=discord.Color.green()
    )
    embed.add_field(name="College", value=prospect['college'], inline=True)
    embed.add_field(name="Position", value=draft_state["positions"].get(prospect['position_id'], "N/A"), inline=True)
    embed.add_field(name="Ranking", value=prospect['ranking'], inline=True)
    
    # Notify next team
    if on_deck:
        next_team = draft_state["teams"][on_deck['team_id']]
        next_gm = draft_state["users"].get(next_team['gm_id'])
        
        if next_gm:
            message = f"🎙️ **On the Clock**: <@{next_gm.get('username', 'Unknown')}>\n"
            
            if in_hole:
                hole_team = draft_state["teams"][in_hole['team_id']]
                hole_gm = draft_state["users"].get(hole_team['gm_id'])
                if hole_gm:
                    message += f"📍 **On Deck**: {on_deck.get('username', 'Unknown')}\n"
                    message += f"🕳️ **In the Hole**: {hole_gm.get('username', 'Unknown')}"
            
            embed.add_field(name="Next", value=message, inline=False)
    
    await interaction.followup.send(embed=embed)

@bot.tree.command(name="timer", description="Check time remaining on current pick")
async def timer_command(interaction: discord.Interaction):
    current_pick = get_current_pick()
    
    if not current_pick:
        await interaction.response.send_message("❌ No active pick!")
        return
    
    time_remaining = get_time_remaining()
    current_team = draft_state["teams"][current_pick['team_id']]
    gm_user = draft_state["users"].get(current_team['gm_id'])
    
    hours = time_remaining.seconds // 3600
    minutes = (time_remaining.seconds % 3600) // 60
    
    embed = discord.Embed(
        title="⏱️ Draft Timer",
        color=discord.Color.blue()
    )
    embed.add_field(name="On the Clock", value=gm_user.get('screen_name', 'Unknown') if gm_user else "Unknown", inline=False)
    embed.add_field(name="Time Remaining", value=f"{hours}h {minutes}m", inline=False)
    
    if draft_state["timer_paused"]:
        embed.add_field(name="Status", value="⏸️ PAUSED", inline=False)
    
    await interaction.response.send_message(embed=embed)

@bot.tree.command(name="trade", description="Initiate a trade (admins only)")
async def trade_command(interaction: discord.Interaction):
    current_pick = get_current_pick()
    if not current_pick:
        await interaction.response.send_message("❌ No Active Pick!", ephemeral=True)
        return
    
    draft_state["timer_paused"] = True
    draft_state["trade_in_progress"] = True
    
    await interaction.response.send_message("⏸️ Draft paused for trade. Notifying admins...")
    await notify_admins(interaction, f"🔄 Trade in progress initiated by {interaction.user}")

@bot.tree.command(name="resume", description="Resume the draft (admins only)")
async def resume_command(interaction: discord.Interaction):
    if interaction.user.id not in ADMIN_USERS:
        await interaction.response.send_message("❌ Admin only!", ephemeral=True)
        return
    
    draft_state["timer_paused"] = False
    draft_state["trade_in_progress"] = False
    
    current_pick = get_current_pick()
    if current_pick:
        current_team = draft_state["teams"][current_pick['team_id']]
        gm_user = draft_state["users"].get(current_team['gm_id'])
        if gm_user:
            await interaction.response.send_message(
                f"▶️ Draft resumed! On the clock: **{gm_user.get('screen_name')}**"
            )
    else:
        await interaction.response.send_message("▶️ Draft resumed!")

@bot.tree.command(name="order", description="Show current draft order")
async def order_command(interaction: discord.Interaction):
    current_pick = get_current_pick()
    on_deck, in_hole = get_on_deck_and_in_hole()
    
    embed = discord.Embed(title="📋 Draft Order", color=discord.Color.gold())
    
    if current_pick:
        current_team = draft_state["teams"][current_pick['team_id']]
        gm_user = draft_state["users"].get(current_team['gm_id'])
        time_remaining = get_time_remaining()
        hours = time_remaining.seconds // 3600
        minutes = (time_remaining.seconds % 3600) // 60
        
        embed.add_field(
            name=f"Pick {current_pick['id']} - On the Clock",
            value=f"**{gm_user.get('screen_name', 'Unknown')}** ({current_team['division']})\nTime: {hours}h {minutes}m",
            inline=False
        )
    
    if on_deck:
        on_deck_team = draft_state["teams"][on_deck['team_id']]
        on_deck_gm = draft_state["users"].get(on_deck_team['gm_id'])
        embed.add_field(
            name=f"Pick {on_deck['id']} - On Deck",
            value=f"**{on_deck_gm.get('screen_name', 'Unknown')}** ({on_deck_team['division']})",
            inline=False
        )
    
    if in_hole:
        in_hole_team = draft_state["teams"][in_hole['team_id']]
        in_hole_gm = draft_state["users"].get(in_hole_team['gm_id'])
        embed.add_field(
            name=f"Pick {in_hole['id']} - In the Hole",
            value=f"**{in_hole_gm.get('screen_name', 'Unknown')}** ({in_hole_team['division']})",
            inline=False
        )
    
    await interaction.response.send_message(embed=embed)

@bot.tree.command(name="best", description="Show top 10 undrafted players")
async def best_command(interaction: discord.Interaction):
    undrafted = [p for p in draft_state["prospects"].values() if not p['drafted']]
    top_10 = sorted(undrafted, key=lambda x: x['ranking'])[:10]
    
    embed = discord.Embed(title="🌟 Top 10 Available", color=discord.Color.purple())
    
    for i, prospect in enumerate(top_10, 1):
        position = draft_state["positions"].get(prospect['position_id'], "N/A")
        embed.add_field(
            name=f"{i}. {prospect['f_name']} {prospect['l_name']}",
            value=f"**{position}** | {prospect['college']} | Rank: {prospect['ranking']}",
            inline=False
        )
    
    await interaction.response.send_message(embed=embed)

@bot.tree.command(name="review", description="Review drafted players by team")
@app_commands.describe(team_name="Name of the team to review")
async def review_command(interaction: discord.Interaction, team_name: str):
    # Find team by name (case-insensitive)
    team_id = None
    for tid, team in draft_state["teams"].items():
        if team_name.lower() in str(tid).lower():
            team_id = tid
            break
    
    if team_id is None:
        await interaction.response.send_message("❌ Invalid team!")
        return
    
    team = draft_state["teams"][team_id]
    gm = draft_state["users"].get(team['gm_id'])
    
    team_picks = [p for p in draft_state["picks"] if p['team_id'] == team_id and p['player_id']]
    
    embed = discord.Embed(
        title=f"📊 {team['division']} - {team['conference']}",
        description=f"GM: {gm.get('screen_name', 'Unknown') if gm else 'Unknown'}",
        color=discord.Color.blue()
    )
    
    if team_picks:
        for pick in team_picks:
            prospect = draft_state["prospects"].get(pick['player_id'])
            if prospect:
                position = draft_state["positions"].get(prospect['position_id'], "N/A")
                embed.add_field(
                    name=f"Pick {pick['id']}: {prospect['f_name']} {prospect['l_name']}",
                    value=f"**{position}** | {prospect['college']}",
                    inline=False
                )
    else:
        embed.add_field(name="Players", value="No players drafted yet", inline=False)
    
    await interaction.response.send_message(embed=embed)

# Run the bot
bot.run("YOUR_DISCORD_TOKEN_HERE")  # Replace with your Discord bot token
