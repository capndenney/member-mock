import discord
from discord.ext import commands, tasks
from datetime import timedelta
from services.state_manager import draft_state
from helpers.draft_logic import get_current_pick, get_time_remaining
from config import REMINDER_CHANNEL_ID

class DraftEngine(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        # Start the loop when the Cog is initialized
        self.draft_timer_check.start()

    def cog_unload(self):
        # Stop the loop if the Cog is unloaded (prevents ghost tasks)
        self.draft_timer_check.cancel()

    @tasks.loop(minutes=5)
    async def draft_timer_check(self):
        if not draft_state.get("running") or draft_state.get("timer_paused"):
            return

        current_pick = get_current_pick()
        if not current_pick:
            return

        if draft_state["warning_sent"] == current_pick['id']:
            return

        time_remaining = get_time_remaining()
        channel = self.bot.get_channel(REMINDER_CHANNEL_ID)

        if timedelta(minutes=0) < time_remaining <= timedelta(minutes=30):
            current_team = draft_state["teams"].get(current_pick['team_id'])
            gm_user_info = draft_state["users"].get(current_team['gm_id'])
            
            if channel and gm_user_info:
                try:
                    mention = f"<@{gm_user_info['username']}>"
                    await channel.send(
                        f"⚠️ {mention} — **{current_team['team_short']}** has less than **30 minutes** remaining!"
                    )
                    draft_state["warning_sent"] = current_pick['id']
                except Exception as e:
                    print(f"Error in timer loop: {e}")

async def setup(bot):
    await bot.add_cog(DraftEngine(bot))