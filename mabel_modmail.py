import discord
from discord.ext import commands
from pymongo import MongoClient
import os
import asyncio
from typing import Optional
from flask import Flask
from threading import Thread

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

# Global set to track users currently in the ticket creation process
ACTIVE_TICKET_CREATION = set() 

# --- Bot Initialization ---

intents = discord.Intents.default()
intents.dm_messages = True
intents.message_content = True
intents.guilds = True

client = commands.Bot(command_prefix=PREFIX, intents=intents)

# --- MongoDB Utility Functions ---

async def get_channel_id(user_id: int) -> Optional[int]:
    """Retrieves the active channel ID for a user from the database (searches using string ID)."""
    result = TICKETS_COLLECTION.find_one({"_id": str(user_id)})
    
    if result and result.get("channel_id"):
        try:
            return int(result.get("channel_id"))
        except ValueError:
            return None
    return None

async def create_ticket_mapping(user_id: int, channel_id: int):
    """Creates a new ticket mapping, FORCING all IDs to be stored as strings."""
    TICKETS_COLLECTION.insert_one({"_id": str(user_id), "channel_id": str(channel_id)})

async def delete_ticket_mapping(user_id: int):
    """Deletes a ticket mapping from the database (searches using string ID)."""
    TICKETS_COLLECTION.delete_one({"_id": str(user_id)})

async def get_user_id_from_channel(channel_id: int) -> Optional[int]:
    """Retrieves the user ID associated with a channel ID, searching with string ID."""
    
    # We must search using the string representation of the channel ID
    result = TICKETS_COLLECTION.find_one({"channel_id": str(channel_id)}) 

    if result and result.get("_id"):
        # The result's _id is the User ID, stored as a string. We convert it back to an integer.
        try:
            return int(result.get("_id"))
        except ValueError:
            return None
    return None


# --- Flask Server for Render Uptime ---

app = Flask(__name__)

@app.route('/')
def home():
    # This endpoint is hit by an external service (like UptimeRobot) to keep the bot alive.
    return "Professor Mabel ModMail Worker is Running!"

def run_flask_server():
    # Render requires binding to 0.0.0.0 and the PORT environment variable
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port)


# --- Events and Handlers (Observation/Logging Mode) ---

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
    
    # 1. Concurrency Lock Check (Prevents duplicate tickets)
    if user_id in ACTIVE_TICKET_CREATION:
        await asyncio.sleep(0.5) 
        return 

    # 2. Check database for existing ticket
    channel_id = await get_channel_id(user_id) 

    if channel_id is None:
        # Add user to the lock set immediately before creating the ticket
        ACTIVE_TICKET_CREATION.add(user_id) 
        await create_new_ticket(message)
    else:
        await forward_user_message(message, channel_id)

async def create_new_ticket(message: discord.Message):
    """Creates a new ticket channel and forwards the first message (Logging Mode)."""
    guild = client.get_guild(GUILD_ID)
    category = discord.utils.get(guild.categories, id=MODMAIL_CATEGORY_ID)
    user_id = message.author.id
    
    if not guild or not category:
        print("ERROR: Guild or Category ID is invalid.")
        
        # Release lock on failure
        if user_id in ACTIVE_TICKET_CREATION:
            ACTIVE_TICKET_CREATION.remove(user_id)
        return

    channel_name = f"consultation-{message.author.id}"
    
    try:
        new_channel = await guild.create_text_channel(channel_name, category=category)
        
        await create_ticket_mapping(message.author.id, new_channel.id) 
        
        # --- Notification to Staff (Log) ---
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
        
    finally:
        # RELEASE THE LOCK: Remove the user from the active set regardless of success/failure
        if user_id in ACTIVE_TICKET_CREATION:
            ACTIVE_TICKET_CREATION.remove(user_id)

async def forward_user_message(message: discord.Message, channel_id: int):
    """Forwards a user's reply to the corresponding ticket channel."""
    channel = client.get_channel(channel_id)
    
    if channel:
        embed = discord.Embed(
            description=message.content,
            color=discord.Color.lighter_grey()
        )
        embed.set_author(name=f"Trainer: {message.author.display_name}", icon_url=message.author.avatar.url if message.author.avatar else None)
        await channel.send(embed=embed)


# --- Staff Commands (Professor Mabel RP) ---

@client.command(name='reply', aliases=['r'])
@commands.has_role(MOD_ROLE_ID)
async def reply_to_ticket(ctx: commands.Context, *, response: str):
    """Staff command to reply to the user from the ticket channel (RP Mode)."""
    
    # ⚠️ DIAGNOSTIC: Print the channel ID being used for lookup (Check your Render logs!)
    print(f"DEBUG: Attempting lookup for Channel ID: {ctx.channel.id}")
    
    # The category check has been temporarily disabled to isolate the lookup issue.
    # if ctx.channel.category_id != MODMAIL_CATEGORY_ID:
    #     return await ctx.send(f"❌ This command can only be used in a consultation channel.")
        
    user_id = await get_user_id_from_channel(ctx.channel.id)
    
    if user_id:
        user = client.get_user(user_id)
        if user:
            # PROFESSOR MABEL RP STYLING: Hides staff identity
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
    """Staff command to close and delete the ticket channel."""
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
