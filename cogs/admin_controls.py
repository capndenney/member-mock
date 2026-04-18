import discord
import gspread 
from discord import app_commands
from discord.ext import commands
from datetime import datetime
from gspread import Cell 

# Internal Imports
from config import ADMIN_IDS, CENTRAL_TZ
from services.state_manager import draft_state, save_status, load_data, gs_manager
from helpers.draft_logic import (
    get_current_pick, 
    get_time_remaining, 
    find_prospect_by_name, 
    process_pick_logic,
    is_empty
)

class AdminControls(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        # This check runs before EVERY command in this Cog
        if interaction.user.id not in ADMIN_IDS:
            await interaction.response.send_message("❌ Admin only!", ephemeral=True)
            return False
        return True
    
    # 1. Map Short Names to IDs
    def _get_id_from_short(short_name):
        for tid, info in draft_state["teams"].items():
            if info['team_short'].upper() == short_name.strip().upper():
                return tid
        return None

    @app_commands.command(name="trade_picks", description="Admin Only: Swap multiple picks between two teams")
    @app_commands.describe(
        team_a_short="Acronym for first team (e.g. KC)",
        team_a_picks="Comma separated IDs (e.g. 1, 45)",
        team_b_short="Acronym for second team (e.g. SF)",
        team_b_picks="Comma separated IDs (e.g. 12, 80)"
        )
    async def trade_picks(
        self,
        interaction: discord.Interaction, 
        team_a_short: str, 
        team_a_picks: str, 
        team_b_short: str, 
        team_b_picks: str
        ):
        await interaction.response.defer()

        team_a_id = self._get_id_from_short(team_a_short)
        team_b_id = self._get_id_from_short(team_b_short)

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
                            description=f"Due to the trade, **{new_team['team_short']}** is now **On the Clock** for Pick {current_pick['id']}!",
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

    @app_commands.command(name="reverse_pick", description="Admin Only: Undo a specific pick and restart its clock")
    @app_commands.describe(pick_id="The number of the pick to undo")
    async def reverse_pick(self, interaction: discord.Interaction, pick_id: int):
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

    @app_commands.command(name="resume", description="Resume draft (admins only)")
    async def resume_command(self, interaction: discord.Interaction):
        draft_state["timer_paused"], draft_state["trade_in_progress"], draft_state["running"] = False, False, True
        save_status()
        current_pick = get_current_pick()
        if current_pick:
            gm = draft_state["users"].get(draft_state["teams"][current_pick['team_id']]['gm_id'])
            await interaction.response.send_message(f"▶️ Draft resumed! On clock: **{gm.get('screen_name') if gm else 'Unknown'}**")
        else:
            await interaction.response.send_message("▶️ Draft resumed!")

    @app_commands.command(name="force", description="Force a pick (admins only)")
    async def force_command(self, interaction: discord.Interaction, player_name: str):
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
        result_embed, ping_content = await process_pick_logic(current_pick, prospect_id)
        final_content = "⚠️ **ADMIN OVERRIDE**"
        if ping_content:
            final_content += f" | {ping_content}"
        result_embed.set_footer(text=f"Forced by Admin: {interaction.user.display_name}")
        await interaction.followup.send(content=final_content, embed=result_embed)

    @app_commands.command(name="start_draft", description="Admin Only: Officially start the draft and the Pick 1 clock")
    async def start_draft(self, interaction: discord.Interaction):
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
            draft_state["timer_paused"] = False
            save_status()
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

        @app_commands.command(name="sync", description="Admin Only: Refresh all data from Google Sheets")
        async def sync_command(self, interaction: discord.Interaction):


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

    @app_commands.command(name="draft-status", description="Admin Only: Check internal state for debugging")
    async def draft_status_command(self, interaction: discord.Interaction):
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