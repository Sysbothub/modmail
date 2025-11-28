import discord
from discord.ext import commands
from pymongo import MongoClient
import os
import asyncio
from typing import Optional
from threading import Thread
from flask import Flask

# --- Configuration & MongoDB Setup ---

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

# Connect to MongoDB Atlas (Synchronous Client)
try:
    cluster = MongoClient(MONGODB_URI, serverSelectionTimeoutMS=5000)
    cluster.admin.command('ismaster')
    
    db = cluster["MabelModMail"]
    TICKETS_COLLECTION = db["Tickets"] 
    
    # ⚠️ CRITICAL: Ensure an index exists on 'user_id' for fast lookups during ticket creation
    TICKETS_COLLECTION.create_index("user_id", unique=True)
    
    print("✅ Successfully connected to MongoDB Atlas and ensured 'user_id' index exists.")
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

# --- MongoDB Utility Functions (Updated to use NEW MODEL: _id is Channel ID) ---

async def get_channel_id(user_id: int) -> Optional[int]:
    """Retrieves the active channel ID for a user by searching the secondary 'user_id' field."""
    def fetch_doc_sync():
        # Search using the secondary 'user_id' field
        return TICKETS_COLLECTION.find_one({"user_id": str(user_id)})
        
    result = await asyncio.to_thread(fetch_doc_sync)
    
    # The result's _id is the Channel ID in the new model
    if result and result.get("_id"): 
        try:
            return int(result.get("_id"))
        except ValueError:
            return None
    return None

async def create_ticket_mapping(user_id: int, channel_id: int):
    """Creates a new ticket mapping (Channel ID is the primary key _id)."""
    def insert_doc_sync():
        # _id is Channel ID, user_id is the secondary field
        TICKETS_COLLECTION.insert_one({"_id": str(channel_id), "user_id": str(user_id)})
    await asyncio.to_thread(insert_doc_sync)

async def delete_ticket_mapping(user_id: int):
    """Deletes a ticket mapping using the user_id field."""
    def delete_doc_sync():
        # Find the document using user_id field and delete it
        TICKETS_COLLECTION.delete_one({"user_id": str(user_id)})
    await asyncio.to_thread(delete_doc_sync)

async def get_user_id_from_channel(channel_id: int) -> Optional[int]:
    """Retrieves the user ID directly using the Channel ID as the primary key (_id)."""
    
    def fetch_doc_sync():
        # 🌟 FIX: Direct lookup by _id is the fastest and most reliable MongoDB operation
        return TICKETS_COLLECTION.find_one({"_id": str(channel_id)})
            
    doc = await asyncio.to_thread(fetch_doc_sync)

    if doc and doc.get("user_id"):
        try:
            # The result's user_id field is the User ID (stored as a string)
            return int(doc.get("user_id"))
        except ValueError:
            return None
    return None


# --- Flask Server for Render Uptime ---

app = Flask(__name__)

@app.route('/')
def home():
    return "Professor Mabel ModMail Worker is Running!"

def run_flask_server():
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port)


# --- Events and Handlers ---

@client.event
async def on_ready():
    print(f'Logged in as {client.user} (ID: {client.user.id})')
    print('----------------------------------')
    await client.change_presence(activity=discord.Game(name="Pokémon Legends Z-A"))

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
        
        if user_id in ACTIVE_TICKET_CREATION:
            return 
        
        ACTIVE_TICKET_CREATION.add(user_id) 
        
        await create_new_ticket(message)
    else:
        await forward_user_message(message, channel_id)

async def create_new_ticket(message: discord.Message):
    """Creates a new ticket channel and forwards the first message."""
    guild = client.get_guild(GUILD_ID)
    category = discord.utils.get(guild.categories, id=MODMAIL_CATEGORY_ID)
    user_id = message.author.id
    
    if not guild or not category:
        print("ERROR: Guild or Category ID is invalid. Check GUILD_ID and MODMAIL_CATEGORY_ID.")
        
        if user_id in ACTIVE_TICKET_CREATION:
            ACTIVE_TICKET_CREATION.remove(user_id)
        return

    channel_name = f"consultation-{message.author.id}"
    
    try:
        new_channel = await guild.create_text_channel(channel_name, category=category)
        
        # Map the new channel ID to the user ID (NEW MODEL: channel_id is _id)
        await create_ticket_mapping(message.author.id, new_channel.id) 
        
        # --- Notification to Staff ---
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
        print(f"FATAL ERROR IN TICKET CREATION: {e}")
        
    finally:
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
@commands.guild_only() 
async def reply_to_ticket(ctx: commands.Context, *, response: str):
    """Staff command to reply to the user from the ticket channel (RP Mode)."""
    
    channel_id = ctx.channel.id
    
    if ctx.channel.category_id != MODMAIL_CATEGORY_ID:
        return await ctx.send(f"❌ This command can only be used in a consultation channel.")
        
    # 🌟 FIX: Direct lookup by Channel ID (_id) is now fast and reliable.
    user_id = await get_user_id_from_channel(channel_id)
    
    if user_id:
        
        user = client.get_user(user_id)
        if user:
            # PROFESSOR MABEL RP STYLING: Hides staff identity
            mabel_response_embed = discord.Embed(
                description=response,
                color=discord.Color.blue()
            )
            mabel_response_embed.set_author(name="Professor Mabel", icon_url=client.user.avatar.url if client.user.avatar else None)
            
            try:
                await user.send(embed=mabel_response_embed)
            except discord.Forbidden:
                return await ctx.send("❌ Error: Cannot DM the user. They may have DMs disabled or have blocked the bot.")

            await asyncio.sleep(1)
            
            await ctx.send(f"✅ Response sent to {user.display_name} (Replied by {ctx.author.display_name})")
            
            try:
                await ctx.message.delete()
            except:
                pass 
            return

    await ctx.send("❌ Error: Could not find the associated trainer for this consultation. Database lookup failed.")

@client.command(name='close', aliases=['c'])
@commands.has_role(MOD_ROLE_ID)
@commands.guild_only() 
async def close_ticket(ctx: commands.Context):
    """Staff command to close and delete the ticket channel."""
    if ctx.channel.category_id != MODMAIL_CATEGORY_ID:
        return await ctx.send("❌ This command can only be used in a consultation channel.")
        
    # We must look up the user ID based on the channel to delete the correct record
    user_id = await get_user_id_from_channel(ctx.channel.id) 
    
    if user_id:
        user = client.get_user(user_id)
        
        # We delete the mapping using the user_id (as channel_id is deleted when channel is deleted)
        await delete_ticket_mapping(user_id)

        if user:
            try:
                await user.send("✅ Professor Mabel has closed your consultation thread. Please DM the bot again to open a new one.")
            except:
                print(f"Could not DM user {user.id} about closure.")
            
    await ctx.send("🗑️ Consultation thread closing in 5 seconds...")
    await asyncio.sleep(5)
    await ctx.channel.delete()

# --- Run the Bot ---
if __name__ == '__main__':
    Thread(target=run_flask_server).start()
    
    try:
        client.run(TOKEN)
    except Exception as e:
        print(f"FATAL: Discord Bot failed to run: {e}")
