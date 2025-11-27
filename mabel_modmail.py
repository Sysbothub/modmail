import discord
from discord.ext import commands
from pymongo import MongoClient
import os
import asyncio
from typing import Optional
from flask import Flask # <-- NEW
from threading import Thread # <-- NEW

# --- Configuration & MongoDB Setup ---

# Load configuration from environment variables
try:
    TOKEN = os.environ['DISCORD_TOKEN']
    GUILD_ID = int(os.environ['GUILD_ID'])
    MODMAIL_CATEGORY_ID = int(os.environ['MODMAIL_CATEGORY_ID'])
    MOD_ROLE_ID = int(os.environ['MOD_ROLE_ID'])
    MONGODB_URI = os.environ['MONGODB_URI']
    PREFIX = os.environ.get('PREFIX', '!')
except KeyError as e:
    print(f"FATAL: Missing environment variable: {e}")
    exit()

# Connect to MongoDB Atlas
try:
    cluster = MongoClient(MONGODB_URI, serverSelectionTimeoutMS=5000)
    cluster.admin.command('ismaster')
    
    db = cluster["MabelModMail"]
    TICKETS_COLLECTION = db["Tickets"] 
    print("✅ Successfully connected to MongoDB Atlas.")
except Exception as e:
    print(f"FATAL: Failed to connect to MongoDB. Check MONGODB_URI and IP access: {e}")
    exit()

# --- Bot Initialization ---

intents = discord.Intents.default()
intents.dm_messages = True
intents.message_content = True
intents.guilds = True

client = commands.Bot(command_prefix=PREFIX, intents=intents)

# --- MongoDB Utility Functions (Same as before) ---
async def get_channel_id(user_id: int) -> Optional[int]:
    result = TICKETS_COLLECTION.find_one({"_id": user_id})
    return result.get("channel_id") if result else None

async def create_ticket_mapping(user_id: int, channel_id: int):
    TICKETS_COLLECTION.insert_one({"_id": user_id, "channel_id": channel_id})

async def delete_ticket_mapping(user_id: int):
    TICKETS_COLLECTION.delete_one({"_id": user_id})

async def get_user_id_from_channel(channel_id: int) -> Optional[int]:
    result = TICKETS_COLLECTION.find_one({"channel_id": channel_id})
    return result.get("_id") if result else None


# --- Flask Server for Render Uptime (NEW) ---

app = Flask(__name__)

@app.route('/')
def home():
    # Render requires this endpoint to confirm the service is alive.
    return "Professor Mabel ModMail Worker is Running!"

def run_flask_server():
    # Render requires binding to 0.0.0.0 and the PORT environment variable
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port)


# --- Events and Handlers (Same as before) ---

@client.event
async def on_ready():
    print(f'Logged in as {client.user} (ID: {client.user.id})')
    print('----------------------------------')
    await client.change_presence(activity=discord.Activity(
        type=discord.ActivityType.listening, 
        name="your DMs for a consultation"
    ))

@client.event
async def on_message(message):
    if message.author.bot:
        return

    if isinstance(message.channel, discord.DMChannel):
        await handle_dm_message(message)
    
    await client.process_commands(message)

async def handle_dm_message(message: discord.Message):
    user_id = message.author.id
    channel_id = await get_channel_id(user_id) 

    if channel_id is None:
        await create_new_ticket(message)
    else:
        await forward_user_message(message, channel_id)

async def create_new_ticket(message: discord.Message):
    guild = client.get_guild(GUILD_ID)
    category = discord.utils.get(guild.categories, id=MODMAIL_CATEGORY_ID)
    
    if not guild or not category:
        print("ERROR: Guild or Category ID is invalid.")
        return

    channel_name = f"consultation-{message.author.id}"
    
    try:
        new_channel = await guild.create_text_channel(channel_name, category=category)
        await create_ticket_mapping(message.author.id, new_channel.id)
        
        mod_role = guild.get_role(MOD_ROLE_ID)
        
        embed = discord.Embed(
            title="📬 New Consultation Thread Opened",
            description=message.content,
            color=discord.Color.blue()
        )
        embed.set_author(name=f"Trainer: {message.author.display_name}", icon_url=message.author.avatar.url if message.author.avatar else None)
        embed.set_footer(text=f"User ID: {message.author.id} | Use {PREFIX}reply")
        
        await new_channel.send(f"{mod_role.mention if mod_role else 'Staff'}, new request:", embed=embed)

    except Exception as e:
        print(f"Error creating channel or saving to MongoDB: {e}")

async def forward_user_message(message: discord.Message, channel_id: int):
    channel = client.get_channel(channel_id)
    
    if channel:
        embed = discord.Embed(
            description=message.content,
            color=discord.Color.lighter_grey()
        )
        embed.set_author(name=f"Trainer: {message.author.display_name}", icon_url=message.author.avatar.url if message.author.avatar else None)
        await channel.send(embed=embed)


# --- Staff Commands (Same as before) ---

@client.command(name='reply', aliases=['r'])
@commands.has_role(MOD_ROLE_ID)
async def reply_to_ticket(ctx: commands.Context, *, response: str):
    if ctx.channel.category_id != MODMAIL_CATEGORY_ID:
        return await ctx.send(f"❌ This command can only be used in a consultation channel.")
        
    user_id = await get_user_id_from_channel(ctx.channel.id)
    
    if user_id:
        user = client.get_user(user_id)
        if user:
            mabel_response_embed = discord.Embed(
                description=response,
                color=discord.Color.blue()
            )
            mabel_response_embed.set_author(name="Professor Mabel", icon_url=client.user.avatar.url if client.user.avatar else None)
            
            await user.send(embed=mabel_response_embed)
            
            await ctx.send(f"✅ Response sent to {user.display_name} (Replied by {ctx.author.display_name})")
            
            try:
                await ctx.message.delete()
            except:
                pass 
            return

    await ctx.send("❌ Error: Could not find the associated trainer for this consultation.")

@client.command(name='close', aliases=['c'])
@commands.has_role(MOD_ROLE_ID)
async def close_ticket(ctx: commands.Context):
    if ctx.channel.category_id != MODMAIL_CATEGORY_ID:
        return await ctx.send("❌ This command can only be used in a consultation channel.")
        
    user_id = await get_user_id_from_channel(ctx.channel.id)
    
    if user_id:
        user = client.get_user(user_id)
        if user:
            try:
                await user.send("✅ Professor Mabel has closed your consultation thread. Please DM the bot again to open a new one.")
            except:
                print(f"Could not DM user {user.id} about closure.")
            
            await delete_ticket_mapping(user_id)

    await ctx.send("🗑️ Consultation thread closing in 5 seconds...")
    await asyncio.sleep(5)
    await ctx.channel.delete()

# --- Run the Bot ---
if __name__ == '__main__':
    # 1. Start Flask in a background thread to satisfy Render's Web Service requirement
    Thread(target=run_flask_server).start()
    
    # 2. Run the Discord bot in the main thread
    try:
        client.run(TOKEN)
    except Exception as e:
        print(f"FATAL: Discord Bot failed to run: {e}")
