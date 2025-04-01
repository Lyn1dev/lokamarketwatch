import os
import discord
from discord.ext import commands
from dotenv import load_dotenv

load_dotenv()

bot = commands.Bot(command_prefix='!', intents=discord.Intents.default())

@bot.command()
async def ping(ctx):
    latency = round(bot.latency * 1000)
    await ctx.send(f'Pong! {latency}ms')

try:
    bot.run(os.getenv('BOT_TOKEN'))
except discord.LoginFailure:
    print('Invalid bot token. Please check your .env file')