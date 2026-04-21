import os
import discord
from discord.ext import commands
from dotenv import load_dotenv
import asyncio

# Local Imports
from config import ADMIN_IDS, CENTRAL_TZ, PICK_TIME_HOURS
from services.state_manager import save_status, load_data, load_status
from helpers.draft_logic import get_current_pick, get_time_remaining
from cogs.draft_engine import draft_timer_check


# 1. Load Environment Variables
load_dotenv()

# 2. Define Bot/Intents
intents = discord.Intents.default()
intents.message_content = True  # Required for reading commands
bot = commands.Bot(command_prefix="!", intents=intents)

# 3. Load your Cogs (This is where your AdminControls file is registered)
async def load_extensions():
    # Example: loading the admin file you just cleaned up
    await bot.load_extension("cogs.admin_controls")

# IMPORTANT: You must start the loop in on_ready
@bot.event
async def on_ready():
    load_status()
    await load_data()
    await bot.tree.sync()
    if not draft_timer_check.is_running():
        draft_timer_check.start()
    print(f"{bot.user} is online and the timer loop is active!")


if __name__ == "__main__":
    TOKEN = os.getenv("DISCORD_TOKEN")
    if TOKEN:
        asyncio.run(load_extensions())
        bot.run(TOKEN)
    else:
        print("Critical Error: DISCORD_TOKEN not found in environment")