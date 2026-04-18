import discord
import gspread 
from discord import app_commands
from discord.ext import commands
from datetime import datetime
from gspread import Cell 
from typing import Optional

# Internal Imports
from config import ADMIN_IDS, CENTRAL_TZ
from draft_engine import notify_admins
from services.state_manager import draft_state, save_status, load_data, gs_manager
from helpers.draft_logic import (
    get_current_pick, 
    get_time_remaining, 
    find_prospect_by_name, 
    process_pick_logic,
    is_empty
)

@app_commands.command(name="trade", description="Initiate a trade")
async def trade_command(interaction: discord.Interaction):
    current_pick = get_current_pick()
    if not current_pick:
        await interaction.response.send_message("❌ No Active Pick!", ephemeral=True)
        return
    draft_state["timer_paused"] = True
    save_status()
    draft_state["trade_in_progress"] = True
    await interaction.response.send_message("⏸️ Draft paused for trade. Notifying admins...")
    await notify_admins(interaction, f"🔄 Trade initiated by {interaction.user}")

@app_commands.command(name="timer", description="Check time remaining")
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

@app_commands.command(name="pick", description="Make a pick in the draft")
async def pick_command(interaction: discord.Interaction, player_name: str):
    await interaction.response.defer()
    current_pick = get_current_pick()
    if not current_pick:
        await interaction.followup.send("❌ No picks remaining!")
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
        save_status()
        await interaction.followup.send("❌ Player not found! Admins notified.")
        return

    if draft_state["prospects"][prospect_id]['drafted']:
        await interaction.followup.send("❌ Player already drafted!")
        return

    result_embed, ping_content = await process_pick_logic(current_pick, prospect_id)
    await interaction.followup.send(content=ping_content if ping_content else None, embed=result_embed)