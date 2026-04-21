import discord
from datetime import datetime, timedelta
from typing import Optional, Dict

# Internal Imports
from config import ADMIN_IDS, CENTRAL_TZ, PICK_TIME_HOURS
from services.state_manager import draft_state, save_status, load_data, gs_manager
from helpers.draft_logic import (
    get_current_pick, 
    get_time_remaining, 
    find_prospect_by_name, 
    process_pick_logic,
    is_empty
)

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
    # If the 2-hour window crosses the 10PM barrier, push the deadline by 11 hours
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

async def notify_admins(bot, message: str):
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
    ping_content = ""
    
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
            user_id_val = next_gm.get('username', 'Unknown')
            mention = f"<@{user_id_val}>"
            ping_content = f"🔔 {mention}, you're up next for Pick {otc['id']}!"
            message = f"🎙️ **On the Clock**: <@{next_gm.get('username', 'Unknown')}> ({next_gm.get('team_short', 'UNK')})\n"
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

    return embed, ping_content