import discord
import gspread 
from discord import app_commands
from discord.ext import commands
from datetime import datetime
from gspread import Cell 
from typing import Optional

# Internal Imports
from config import ADMIN_IDS, CENTRAL_TZ
from draft_engine import get_on_deck_and_in_hole
from services.state_manager import draft_state, save_status, load_data, gs_manager
from helpers.draft_logic import (
    get_current_pick, 
    get_time_remaining, 
    find_prospect_by_name, 
    process_pick_logic,
    is_empty
)

@app_commands.command(name="review", description="Review team picks")
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

@app_commands.command(name="best", description="Show top available")
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
    await interaction.response.send_message(embed=embed, ephemeral=private)

@app_commands.command(name="order", description="Show draft order")
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

@app_commands.command(name="great", description="who is great?")
async def great(interaction: discord.Interaction):
    await interaction.response.send_message("Craig is Great!")