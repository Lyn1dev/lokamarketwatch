import discord
from discord import app_commands
import os
from dotenv import load_dotenv
import asyncio
import aiohttp
import json
from datetime import datetime
import logging

load_dotenv()

# Set up logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger('bot')

class PlayerCache:
    def __init__(self):
        self.cache_file = "player_cache.json"
        self.players = {}
        self.last_update = None
        self.highest_page_checked = 0  # Track the highest page we've checked
        self.load_cache()

    def load_cache(self):
        try:
            if os.path.exists(self.cache_file):
                with open(self.cache_file, 'r') as f:
                    data = json.load(f)
                    self.players = data.get('players', {})
                    self.last_update = data.get('last_update')
                    self.highest_page_checked = data.get('highest_page_checked', 0)
                    logger.info(f"Loaded {len(self.players)} players from cache (last checked up to page {self.highest_page_checked})")
        except Exception as e:
            logger.error(f"Error loading cache: {e}")
            self.players = {}
            self.last_update = None
            self.highest_page_checked = 0

    def save_cache(self):
        try:
            with open(self.cache_file, 'w') as f:
                json.dump({
                    'players': self.players,
                    'last_update': self.last_update,
                    'highest_page_checked': self.highest_page_checked
                }, f, indent=2)
            logger.info(f"Saved {len(self.players)} players to cache (up to page {self.highest_page_checked})")
        except Exception as e:
            logger.error(f"Error saving cache: {e}")

    async def update_cache(self):
        """Update the player cache, focusing on getting new players without redownloading everything"""
        try:
            async with aiohttp.ClientSession() as session:
                # First, get the total number of pages to know how far we need to go
                start_url = "https://api.lokamc.com/players?page=0&size=20"
                async with session.get(start_url) as response:
                    if response.status != 200:
                        logger.error(f"API request failed with status: {response.status}")
                        return
                    
                    data = await response.json()
                    if not isinstance(data, dict) or "page" not in data:
                        logger.error("Could not determine total pages")
                        return
                    
                    total_pages = data["page"].get("totalPages", 0)
                    total_elements = data["page"].get("totalElements", 0)
                    logger.info(f"Found {total_elements} total players across {total_pages} pages")
                    
                    # Extract any players from the first page
                    if "_embedded" in data and "players" in data["_embedded"]:
                        players_list = data["_embedded"]["players"]
                        for player in players_list:
                            if player and "id" in player:
                                self.players[player["id"]] = player
                
                # If we've never checked before, or it's been a long time, start from our highest page
                # Otherwise, just check newer pages first
                if self.highest_page_checked >= total_pages:
                    start_page = 0  # If we've checked everything before, just check newest pages
                else:
                    start_page = self.highest_page_checked
                
                # First check newest pages (page 0) for any new players
                if start_page > 0:
                    logger.info(f"Checking newest players first (page 0)")
                    async with session.get("https://api.lokamc.com/players?page=0&size=20") as response:
                        if response.status == 200:
                            data = await response.json()
                            if "_embedded" in data and "players" in data["_embedded"]:
                                newest_players = data["_embedded"]["players"]
                                for player in newest_players:
                                    if player and "id" in player:
                                        self.players[player["id"]] = player
                                logger.info(f"Added/updated {len(newest_players)} newest players")
                                await asyncio.sleep(1)  # Small delay to avoid rate limiting
                
                # Now check from our highest checked page up to the newest pages
                new_players_count = 0
                pages_checked = 0
                max_pages_per_update = 50  # Limit how many pages we check per update to avoid overloading
                
                for page in range(start_page, min(total_pages, start_page + max_pages_per_update)):
                    try:
                        logger.info(f"Checking page {page+1}/{total_pages} for new players...")
                        url = f"https://api.lokamc.com/players?page={page}&size=20"
                        
                        async with session.get(url) as response:
                            if response.status == 200:
                                data = await response.json()
                                if "_embedded" in data and "players" in data["_embedded"]:
                                    players_list = data["_embedded"]["players"]
                                    new_in_page = 0
                                    
                                    for player in players_list:
                                        if player and "id" in player:
                                            if player["id"] not in self.players:
                                                new_in_page += 1
                                            self.players[player["id"]] = player
                                    
                                    pages_checked += 1
                                    new_players_count += new_in_page
                                    logger.info(f"Found {new_in_page} new players on page {page+1}")
                                    
                                    # Update our highest page seen
                                    self.highest_page_checked = max(self.highest_page_checked, page + 1)
                                    
                                    # Small delay to avoid rate limiting
                                    await asyncio.sleep(1)
                            else:
                                logger.error(f"Error fetching page {page}: Status {response.status}")
                                await asyncio.sleep(5)  # Longer delay on error
                    except Exception as e:
                        logger.error(f"Error processing page {page}: {e}")
                        await asyncio.sleep(5)
                
                # Save after update
                self.last_update = datetime.now().isoformat()
                self.save_cache()
                logger.info(f"Cache update complete: Checked {pages_checked} pages, found {new_players_count} new players, total in cache: {len(self.players)}")
                
                # If we've checked all pages, reset our tracker to start from the beginning next time
                if self.highest_page_checked >= total_pages:
                    logger.info("Reached final page, will start from newest players next update")
                    self.highest_page_checked = total_pages  # Mark that we've seen all pages

        except Exception as e:
            logger.error(f"Error updating cache: {e}")

class MyClient(discord.Client):
    def __init__(self, *, intents: discord.Intents):
        super().__init__(intents=intents)
        self.tree = app_commands.CommandTree(self)
        self.player_cache = PlayerCache()
        self.bg_task = None
        
        # User links storage
        self.user_links_file = "user_links.json"
        self.user_links = {}  # Maps Discord IDs to Loka player IDs
        self.load_user_links()
        
        # Cache configuration flags
        self.cache_enabled = True  # Re-enable the cache for seller lookups
        self.initial_update = False  # Don't update cache on startup
        self.background_updates = False  # Don't run hourly updates
        self.seller_lookup = True  # Enable seller name lookups

    def load_user_links(self):
        """Load user links from file"""
        try:
            if os.path.exists(self.user_links_file):
                with open(self.user_links_file, 'r') as f:
                    self.user_links = json.load(f)
                logger.info(f"Loaded {len(self.user_links)} user links")
            else:
                logger.info("No user links file found, creating a new one")
                self.user_links = {}
                self.save_user_links()
        except Exception as e:
            logger.error(f"Error loading user links: {e}")
            self.user_links = {}
    
    def save_user_links(self):
        """Save user links to file"""
        try:
            with open(self.user_links_file, 'w') as f:
                json.dump(self.user_links, f, indent=2)
            logger.info(f"Saved {len(self.user_links)} user links")
        except Exception as e:
            logger.error(f"Error saving user links: {e}")
            
    async def is_user_linked(self, discord_id):
        """Check if a Discord user is linked to a Loka player"""
        return str(discord_id) in self.user_links
        
    async def get_linked_player(self, discord_id):
        """Get the Loka player linked to a Discord user"""
        player_id = self.user_links.get(str(discord_id))
        if not player_id:
            return None
            
        # Check if player is in cache
        if player_id in self.player_cache.players:
            return self.player_cache.players[player_id]
            
        # If not in cache, fetch from API
        try:
            async with aiohttp.ClientSession() as session:
                player_url = f"https://api.lokamc.com/players/{player_id}"
                async with session.get(player_url) as response:
                    if response.status == 200:
                        player = await response.json()
                        # Update cache
                        if self.cache_enabled and player and "id" in player:
                            self.player_cache.players[player["id"]] = player
                            self.player_cache.save_cache()
                        return player
        except Exception as e:
            logger.error(f"Error fetching linked player {player_id}: {e}")
            
        return None

    async def setup_hook(self):
        # Load existing cache but skip updates based on configuration
        if self.cache_enabled:
            logger.info(f"Cache enabled. Loaded {len(self.player_cache.players)} cached players.")
            
            if self.initial_update:
                # Only run initial update if explicitly enabled
                logger.info("Running initial cache update...")
                await self.player_cache.update_cache()
            else:
                logger.info("Skipping initial cache update")
            
            # Start background task only if enabled
            if self.background_updates:
                logger.info("Starting hourly background cache updates")
                self.bg_task = self.loop.create_task(self.cache_update_task())
            else:
                logger.info("Background cache updates disabled")
        else:
            logger.info("Player cache functionality is completely disabled")
        
        commands = await self.tree.sync()
        print(f"Syncing commands: {commands}")

    async def cache_update_task(self):
        await self.wait_until_ready()
        while not self.is_closed():
            try:
                await self.player_cache.update_cache()
                await asyncio.sleep(3600)  # Wait for 1 hour
            except Exception as e:
                logger.error(f"Error in cache update task: {e}")
                await asyncio.sleep(60)  # Wait 1 minute before retrying if there's an error

    async def search_player_by_name(self, name):
        """Search for a player by name in our local cache or via API if needed"""
        if not name or not isinstance(name, str):
            logger.warning(f"Invalid player name provided: {name}")
            return None
        
        try:
            # First check our local cache
            name_lower = name.lower()  # This is safe now since we checked name is a string
            if self.cache_enabled and self.player_cache.players:
                for player in self.player_cache.players.values():
                    if player and isinstance(player, dict) and player.get("name") and isinstance(player.get("name"), str):
                        if player.get("name", "").lower() == name_lower:
                            return player
        except Exception as e:
            logger.error(f"Error checking cache for player '{name}': {e}")
            # Continue to API search even if cache check fails
        
        # If not in cache or cache disabled, try direct API search
        logger.info(f"Player '{name}' not found in cache, trying API...")
        try:
            async with aiohttp.ClientSession() as session:
                # Try to find the player by exact name match
                search_url = f"https://api.lokamc.com/players/search/findByName?name={name}"
                async with session.get(search_url) as response:
                    if response.status == 200:
                        try:
                            player = await response.json()
                            if player and isinstance(player, dict) and "id" in player:
                                # Add to our cache for future lookups
                                if self.cache_enabled:
                                    self.player_cache.players[player["id"]] = player
                                    self.player_cache.save_cache()
                                logger.info(f"Found player via API: {player.get('name')}")
                                return player
                            else:
                                logger.warning(f"API returned invalid player data for '{name}'")
                        except Exception as e:
                            logger.error(f"Error parsing player data for '{name}': {e}")
        except Exception as e:
            logger.error(f"Error searching for player '{name}' via API: {e}")
        
        logger.warning(f"Player '{name}' not found anywhere")
        return None

intents = discord.Intents.default()
client = MyClient(intents=intents)

@client.event
async def on_ready():
    num_commands = len(client.tree.get_commands())
    print(f'Logged in as {client.user} (ID: {client.user.id})')
    print(f'Registered {num_commands} commands.')
    print(f'Player cache contains {len(client.player_cache.players)} players.')

@client.tree.command(name="ping", description="Replies with Pong!")
async def ping(interaction: discord.Interaction):
    await interaction.response.send_message("Pong!")

@client.tree.command(name="link", description="Link your Discord account to your Loka Minecraft account")
@app_commands.describe(
    username="Your Loka Minecraft username"
)
async def link(interaction: discord.Interaction, username: str):
    await interaction.response.defer(ephemeral=True)  # Use ephemeral to keep personal info private
    
    logger.info(f"Link request from {interaction.user.name} (ID: {interaction.user.id}) for username: {username}")
    
    # Check if the user is already linked
    if await client.is_user_linked(interaction.user.id):
        linked_player = await client.get_linked_player(interaction.user.id)
        if linked_player and "name" in linked_player:
            await interaction.followup.send(
                f"Your Discord account is already linked to Loka player: **{linked_player['name']}**\n"
                f"If you want to link to a different account, please contact an admin.",
                ephemeral=True
            )
        else:
            await interaction.followup.send(
                "Your Discord account is already linked to a Loka player, but I couldn't retrieve their details.\n"
                "If you want to link to a different account, please contact an admin.",
                ephemeral=True
            )
        return
    
    # Search for the player
    try:
        player = await client.search_player_by_name(username)
        
        if not player:
            await interaction.followup.send(
                f"Could not find a Loka player with the username: **{username}**\n"
                f"Please check your spelling and try again.",
                ephemeral=True
            )
            return
        
        player_id = player.get("id")
        player_name = player.get("name", username)
        discord_id_from_api = player.get("discordId")
        
        if not player_id:
            await interaction.followup.send(
                f"Found player **{player_name}** but couldn't get their ID. Please try again later.",
                ephemeral=True
            )
            return
        
        # Check if the player has a discordId
        if not discord_id_from_api:
            await interaction.followup.send(
                f"Found player **{player_name}**, but they haven't linked their Discord account on Loka yet.\n"
                f"Please link your Discord account in-game first, then try again!",
                ephemeral=True
            )
            return
        
        # Verify that the Discord ID matches
        user_discord_id = str(interaction.user.id)
        if discord_id_from_api != user_discord_id:
            await interaction.followup.send(
                f"Found player **{player_name}**, but they're linked to a different Discord account.\n"
                f"Please make sure you've linked your Discord account in-game with the correct Discord account.",
                ephemeral=True
            )
            return
        
        # All checks passed, link the accounts
        client.user_links[user_discord_id] = player_id
        client.save_user_links()
        
        await interaction.followup.send(
            f"Successfully linked your Discord account to Loka player: **{player_name}**\n"
            f"You can now use personalized commands!",
            ephemeral=True
        )
        
    except Exception as e:
        logger.error(f"Error in link command: {e}")
        await interaction.followup.send(
            "An error occurred while trying to link your account. Please try again later.",
            ephemeral=True
        )

@client.tree.command(name="buyorders", description="Get market buy orders")
@app_commands.describe(
    item="Item name to filter by (e.g., EMERALD, DIAMOND)",
    buyer="Player name who is buying the items"
)
async def buyorders(interaction: discord.Interaction, item: str = None, buyer: str = None):
    await interaction.response.defer()
    
    logger.info(f"Buy order requested by {interaction.user.name} with item: {item}, buyer: {buyer}")
    
    # Convert item to uppercase for comparison
    item_upper = item.upper() if item else None
    
    all_items = []
    buyer_ids = set()
    filtered_by_item = False
    buyer_id = None
    buyer_name = None
    
    async with aiohttp.ClientSession() as session:
        # Build the URL for the API request
        if buyer:
            try:
                logger.info(f"Searching for buyer: '{buyer}'")
                player = await client.search_player_by_name(buyer)
                
                if player and isinstance(player, dict):
                    buyer_id = player.get("id")
                    buyer_name = player.get("name")
                    
                    if buyer_id:
                        logger.info(f"Found buyer: {buyer_name} with ID: {buyer_id}")
                        # Use the findByOwnerId endpoint
                        next_url = f"https://api.lokamc.com/market_buyorders/search/findByOwnerId?id={buyer_id}&size=100"
                        logger.info(f"Using buyer endpoint with ID {buyer_id}: {next_url}")
                        
                        # If item is also specified, we'll filter results after fetching
                        if item:
                            logger.info(f"Will filter {buyer_name}'s buy orders by item type: {item_upper}")
                            filtered_by_item = True
                    else:
                        logger.warning(f"Found player but ID is missing: {player}")
                        await interaction.followup.send(f"Found player '{buyer}' but couldn't get their ID. Please try again.")
                        return
                else:
                    logger.warning(f"Could not find player with name '{buyer}'")
                    await interaction.followup.send(f"Could not find player with name '{buyer}'")
                    return
            except Exception as e:
                logger.error(f"Error looking up buyer '{buyer}': {e}")
                await interaction.followup.send(f"Error looking up player '{buyer}'. Please try again.")
                return
        elif item:
            # Use the findByType endpoint
            next_url = f"https://api.lokamc.com/market_buyorders/search/findByType?type={item_upper}&size=100"
        else:
            # Use the default endpoint for all items - use larger size for unfiltered requests
            next_url = "https://api.lokamc.com/market_buyorders?size=200"
        
        logger.info(f"Fetching buy orders from URL: {next_url}")
        
        # Process API responses
        retry_count = 0
        max_retries = 3
        pages_retrieved = 0
        total_pages = None
        
        try:
            while next_url and retry_count < max_retries:
                try:
                    async with session.get(next_url) as response:
                        if response.status == 200:
                            # Reset retry counter on success
                            retry_count = 0
                            pages_retrieved += 1
                            
                            data = await response.json()
                            
                            # Log the data structure to debug
                            logger.info(f"Response structure: {list(data.keys()) if isinstance(data, dict) else 'Not a dict'}")
                            
                            # Track total pages if we don't know it yet
                            if total_pages is None and "page" in data:
                                total_pages = data["page"].get("totalPages", 0)
                                logger.info(f"Total pages reported by API: {total_pages}")
                            
                            # Check for both possible response structures
                            items_list = []
                            if isinstance(data, dict):
                                if "_embedded" in data:
                                    if "market_buyorders" in data["_embedded"]:
                                        items_list = data["_embedded"]["market_buyorders"]
                                        logger.info(f"Found {len(items_list)} buy orders in market_buyorders")
                                    elif "market_sales" in data["_embedded"]:  # API sometimes uses this key
                                        items_list = data["_embedded"]["market_sales"]
                                        logger.info(f"Found {len(items_list)} buy orders in market_sales")
                            
                            # Log detailed pagination information
                            page_info = data.get("page", {})
                            if page_info:
                                logger.info(f"Page info: size={page_info.get('size')}, totalElements={page_info.get('totalElements')}, " +
                                           f"totalPages={page_info.get('totalPages')}, number={page_info.get('number')} of {total_pages}")
                            
                            # Process items from the current page
                            for item_data in items_list:
                                if item_data:  # Make sure item_data is not None
                                    owner_id = item_data.get("ownerId")
                                    material_type = item_data.get("type", "Unknown")
                                    
                                    # If both filters are active, only add matching items
                                    if filtered_by_item and item_upper and material_type.upper() != item_upper:
                                        continue
                                    
                                    if owner_id and not buyer:  # Only track buyer IDs if we're not already filtering by buyer
                                        buyer_ids.add(owner_id)
                                    
                                    all_items.append({
                                        "material": material_type,
                                        "price": round(item_data.get("price", 0)),
                                        "quantity": item_data.get("quantity", 0),
                                        "ownerId": owner_id
                                    })
                            
                            # Detailed logging of link structure
                            links = data.get("_links", {})
                            logger.info(f"Available links: {list(links.keys())}")
                            if "next" in links:
                                logger.info(f"Next link details: {links['next']}")
                            else:
                                logger.info("No 'next' link found in response")
                                
                            # Force maximum fetched pages if not filtering
                            max_pages_to_fetch = 200  # Increased to handle hundreds of pages
                            if not item and not buyer and pages_retrieved >= max_pages_to_fetch:
                                logger.info(f"Reached maximum page fetch limit ({max_pages_to_fetch}) for unfiltered results")
                                next_url = None
                                break
                                
                            # Stop if we've collected enough items
                            max_items = 2000  # Increased to show more items
                            if len(all_items) >= max_items and (not item and not buyer):
                                logger.info(f"Reached maximum items limit ({max_items}) for unfiltered results")
                                next_url = None
                                break
                                
                            # Get next page URL if available - ensuring we don't miss any
                            next_link = None
                            if "_links" in data and "next" in data["_links"]:
                                next_info = data["_links"]["next"]
                                if isinstance(next_info, dict) and "href" in next_info:
                                    next_link = next_info["href"]
                                    
                            if next_link:
                                # Handle both relative and absolute URLs
                                if next_link.startswith("/"):
                                    next_url = f"https://api.lokamc.com{next_link}"
                                elif next_link.startswith("http"):
                                    next_url = next_link
                                else:
                                    next_url = f"https://api.lokamc.com/{next_link}"
                                
                                # Make sure we're requesting a large page size
                                if "size=" in next_url:
                                    # Replace the size parameter with our desired size
                                    parts = next_url.split("size=")
                                    if len(parts) > 1:
                                        size_part = parts[1].split("&")[0]
                                        next_url = next_url.replace(f"size={size_part}", "size=200")
                                    else:
                                        # Add size parameter if not present
                                        next_url = next_url + ("&" if "?" in next_url else "?") + "size=200"
                                        
                                logger.info(f"Next page URL (modified): {next_url}")
                                
                                # Add a small delay between page requests to avoid rate limiting
                                await asyncio.sleep(0.2)
                            else:
                                logger.info("No next page found or reached the end")
                                next_url = None
                        elif response.status == 429:  # Rate limited
                            retry_count += 1
                            logger.warning(f"Rate limited. Retry {retry_count}/{max_retries}. Waiting 2 seconds...")
                            await asyncio.sleep(2)  # Wait before retrying
                            continue  # Try again with the same URL
                        else:
                            logger.error(f"API request failed with status: {response.status}")
                            if retry_count < max_retries - 1:
                                retry_count += 1
                                logger.warning(f"Retrying {retry_count}/{max_retries}. Waiting 2 seconds...")
                                await asyncio.sleep(2)  # Wait before retrying
                                continue  # Try again with the same URL
                            else:
                                await interaction.followup.send(f"API request failed with status: {response.status}")
                                return
                except aiohttp.ClientError as e:
                    logger.error(f"API request error: {e}")
                    if retry_count < max_retries - 1:
                        retry_count += 1
                        logger.warning(f"Retrying {retry_count}/{max_retries}. Waiting 2 seconds...")
                        await asyncio.sleep(2)  # Wait before retrying
                        continue  # Try again with the same URL
                    else:
                        await interaction.followup.send("Failed to fetch market data. Please try again later.")
                        return
        except Exception as e:
            logger.error(f"Unexpected error in buyorders command: {e}")
            await interaction.followup.send("An unexpected error occurred. Please try again later.")
        
        # Log pagination summary
        logger.info(f"Pagination summary: Retrieved {pages_retrieved} pages of buyorders. Total items: {len(all_items)}")
        
        # After fetching all orders, no need to filter again as we already filtered during processing
        logger.info(f"Total buy orders found: {len(all_items)}")
        
        if not all_items:
            if buyer_name and item:
                await interaction.followup.send(f"No buy orders found for item '{item}' from player '{buyer_name}'")
            elif buyer_name:
                await interaction.followup.send(f"No buy orders found for player '{buyer_name}'")
            elif buyer:
                await interaction.followup.send(f"No buy orders found for player '{buyer}'")
            elif item:
                await interaction.followup.send(f"No buy orders found for item '{item}'")
            else:
                await interaction.followup.send("No buy orders found. This could be due to an API issue. Please try again later.")
            return
        
        # If we're not filtering by buyer, try to populate seller names
        seller_names = {}
        if not buyer and client.seller_lookup:
            logger.info(f"Need to look up {len(buyer_ids)} unique sellers")
            
            # First check cache for sellers we already know
            for seller_id in list(buyer_ids):
                if seller_id in client.player_cache.players:
                    seller = client.player_cache.players[seller_id]
                    if seller and "name" in seller:
                        seller_names[seller_id] = seller["name"]
                        buyer_ids.remove(seller_id)
            
            logger.info(f"Found {len(seller_names)} sellers in cache, {len(buyer_ids)} still need lookup")
            
            # If we still have sellers to look up, only fetch a limited number to avoid rate limits
            max_lookups = min(5, len(buyer_ids))  # Limit to 5 seller lookups per command
            if buyer_ids and max_lookups > 0:
                # Create a new session for seller lookups
                async with aiohttp.ClientSession() as seller_session:
                    for seller_id in list(buyer_ids)[:max_lookups]:
                        try:
                            player_url = f"https://api.lokamc.com/players/{seller_id}"
                            async with seller_session.get(player_url) as player_response:
                                if player_response.status == 200:
                                    seller = await player_response.json()
                                    if seller and "name" in seller:
                                        seller_names[seller_id] = seller["name"]
                                        # Add to cache for future use
                                        client.player_cache.players[seller_id] = seller
                                        # Save cache immediately to avoid losing this info
                                        client.player_cache.save_cache()
                        except Exception as e:
                            logger.error(f"Error fetching player {seller_id}: {e}")
                        # Add a small delay between requests
                        await asyncio.sleep(0.5)

        items_per_page = 10
        current_page = 0
        sort_mode = "default"  # Default sort mode
        
        # Create a copy of all_items for sorting
        sorted_items = list(all_items)
        
        def sort_items(mode):
            nonlocal sorted_items
            # Create a fresh copy of the original items
            sorted_items = list(all_items)
            
            if mode == "price_low_high":
                # Sort by price (low to high)
                sorted_items.sort(key=lambda x: x.get("price", 0))
            elif mode == "price_high_low":
                # Sort by price (high to low)
                sorted_items.sort(key=lambda x: x.get("price", 0), reverse=True)
            # Default mode uses the original order from the API
            
            return sorted_items

        # Initial sort with default mode
        sorted_items = sort_items(sort_mode)
        
        # Calculate number of pages based on sorted items
        num_pages = (len(sorted_items) + items_per_page - 1) // items_per_page

        async def update_embed(page: int):
            start_index = page * items_per_page
            end_index = min(start_index + items_per_page, len(sorted_items))
            items = sorted_items[start_index:end_index]

            title = "Buy Orders"
            if buyer_name and item:
                title = f"Buy Orders for {item_upper} by {buyer_name}"
            elif buyer_name:
                title = f"Buy Orders by {buyer_name}"
            elif buyer:
                title = f"Buy Orders by {buyer}"
            elif item:
                title = f"Buy Orders for {item_upper}"

            embed = discord.Embed(title=title, color=discord.Color.gold())
            
            # If we're filtering by a specific buyer, set their head as the thumbnail
            if buyer_name:
                embed.set_thumbnail(url=f"https://mc-heads.net/head/{buyer_name}")
                embed.set_author(name=f"{buyer_name}'s Buy Orders", icon_url=f"https://mc-heads.net/avatar/{buyer_name}/32")
            
            # Handle case where no items were found
            if not items:
                embed.add_field(
                    name="No Items Found",
                    value="No buy orders matching your criteria were found.",
                    inline=False
                )
                return embed
            
            for item_data in items:
                name = item_data.get("material", "Unknown Material")
                price = item_data.get("price", 0)
                quantity = item_data.get("quantity", 0)
                
                # If we're not already filtering by buyer, show the owner's name
                if not buyer and item_data.get("ownerId"):
                    owner_id = item_data["ownerId"]
                    # Use our lookup results
                    owner_name = seller_names.get(owner_id, "Unknown")
                    
                    # Create a field with seller information
                    value = f"Price: {price}<:PowerShard:1356559399409422336> | Quantity: {quantity}"
                    if owner_name and owner_name != "Unknown":
                        value += f"\nSeller: **{owner_name}**"
                        # Add a seller-specific icon to the embed if not already set
                        if not embed.thumbnail and page == 0 and item_data == items[0]:
                            embed.set_thumbnail(url=f"https://mc-heads.net/head/{owner_name}")
                    else:
                        value += "\nSeller: Unknown"
                    
                    embed.add_field(
                        name=name,
                        value=value,
                        inline=False
                    )
                else:
                    embed.add_field(
                        name=name,
                        value=f"Price: {price}<:PowerShard:1356559399409422336> | Quantity: {quantity}",
                        inline=False
                    )

            # Add sort mode to footer
            sort_text = "Default Order"
            if sort_mode == "price_low_high":
                sort_text = "Price: Low to High"
            elif sort_mode == "price_high_low":
                sort_text = "Price: High to Low"
                
            footer_text = f"Sort: {sort_text}"
            if len(sorted_items) > items_per_page:
                footer_text += f" | Page {current_page + 1} of {num_pages}"
                
            embed.set_footer(text=footer_text)
            return embed

        async def button_callback(interaction: discord.Interaction, page_num: int):
            nonlocal current_page
            current_page = page_num
            embed = await update_embed(current_page)
            
            # Update the buttons when page changes
            await update_view_and_send(interaction, "edit")

        async def sort_callback(interaction: discord.Interaction, new_sort_mode: str):
            nonlocal sort_mode, current_page, num_pages, sorted_items
            
            # Update sort mode
            sort_mode = new_sort_mode
            logger.info(f"Sorting changed to: {sort_mode}")
            
            # Reset to first page
            current_page = 0
            
            # Sort items again
            sorted_items = sort_items(sort_mode)
            
            # Recalculate pages
            num_pages = (len(sorted_items) + items_per_page - 1) // items_per_page
            
            # Update embed
            embed = await update_embed(current_page)
            
            # Update view and send
            await update_view_and_send(interaction, "edit")
            
        async def update_view_and_send(interaction: discord.Interaction, action="send"):
            # Create new view
            view = discord.ui.View(timeout=120)
            
            # Add sort dropdown
            sort_dropdown = discord.ui.Select(
                placeholder="Sort by...",
                options=[
                    discord.SelectOption(label="Default Order", value="default", default=(sort_mode == "default")),
                    discord.SelectOption(label="Price: Low to High", value="price_low_high", default=(sort_mode == "price_low_high")),
                    discord.SelectOption(label="Price: High to Low", value="price_high_low", default=(sort_mode == "price_high_low"))
                ],
                min_values=1,
                max_values=1
            )
            
            sort_dropdown.callback = lambda i: sort_callback(i, sort_dropdown.values[0])
            view.add_item(sort_dropdown)
            
            # Add pagination buttons
            prev_button_disabled = (current_page == 0)
            next_button_disabled = (current_page >= num_pages - 1)
            
            prev_page_button = discord.ui.Button(label="Previous", style=discord.ButtonStyle.secondary, disabled=prev_button_disabled, row=1)
            next_page_button = discord.ui.Button(label="Next", style=discord.ButtonStyle.secondary, disabled=next_button_disabled, row=1)

            prev_page_button.callback = lambda i: button_callback(i, max(0, current_page - 1))
            next_page_button.callback = lambda i: button_callback(i, min(num_pages - 1, current_page + 1))

            # Add buttons to view
            view.add_item(prev_page_button)
            view.add_item(next_page_button)
            view.timeout = 120  # Set timeout to 120 seconds
            
            # Create embed
            embed = await update_embed(current_page)
            
            # Send or edit message
            if action == "send":
                await interaction.followup.send(embed=embed, view=view)
            else:
                await interaction.response.edit_message(embed=embed, view=view)

        # Initial send
        await update_view_and_send(interaction, "send")
        
@client.tree.command(name="sales", description="Lists active sales from the Loka Market.")
@app_commands.describe(
    item="The specific type of item to search for (optional)",
    seller="The player name to check their sales (optional)"
)
async def sales(interaction: discord.Interaction, item: str = None, seller: str = None):
    try:
        await interaction.response.defer(thinking=True)
        
        async with aiohttp.ClientSession() as session:
            all_items = []
            filtered_by_item = False
            item_upper = item.upper() if item else None
            
            # If seller is specified, find their ID
            seller_id = None
            seller_name = None
            
            # Build the URL for the API request
            if seller:
                try:
                    logger.info(f"Searching for seller: '{seller}'")
                    player = await client.search_player_by_name(seller)
                    
                    if player and isinstance(player, dict):
                        seller_id = player.get("id")
                        seller_name = player.get("name")
                        
                        if seller_id:
                            logger.info(f"Found seller: {seller_name} with ID: {seller_id}")
                            # Use the findByOwnerId endpoint
                            next_url = f"https://api.lokamc.com/market_sales/search/findByOwnerId?id={seller_id}&size=100"
                            logger.info(f"Using seller endpoint with ID {seller_id}: {next_url}")
                            
                            # If item is also specified, we'll filter results after fetching
                            if item:
                                logger.info(f"Will filter {seller_name}'s sales by item type: {item_upper}")
                                filtered_by_item = True
                        else:
                            logger.warning(f"Found player but ID is missing: {player}")
                            await interaction.followup.send(f"Found player '{seller}' but couldn't get their ID. Please try again.")
                            return
                    else:
                        logger.warning(f"Could not find player with name '{seller}'")
                        await interaction.followup.send(f"Could not find player with name '{seller}'")
                        return
                except Exception as e:
                    logger.error(f"Error looking up seller '{seller}': {e}")
                    await interaction.followup.send(f"Error looking up player '{seller}'. Please try again.")
                    return
            elif item:
                # Use the findByType endpoint
                next_url = f"https://api.lokamc.com/market_sales/search/findByType?type={item_upper}&size=100"
            else:
                # Use the default endpoint for all items - use larger size for unfiltered requests
                next_url = "https://api.lokamc.com/market_sales?size=200"
            
            logger.info(f"Fetching sales from URL: {next_url}")
            
            # Keep track of unique seller IDs we need to look up
            seller_ids = set()
            
            # Process API responses
            retry_count = 0
            max_retries = 3
            pages_retrieved = 0
            total_pages = None
            
            try:
                while next_url and retry_count < max_retries:
                    try:
                        async with session.get(next_url) as response:
                            if response.status == 200:
                                # Reset retry counter on success
                                retry_count = 0
                                pages_retrieved += 1
                                
                                data = await response.json()
                                
                                # Log the data structure to debug
                                logger.info(f"Response structure: {list(data.keys()) if isinstance(data, dict) else 'Not a dict'}")
                                
                                # Track total pages if we don't know it yet
                                if total_pages is None and "page" in data:
                                    total_pages = data["page"].get("totalPages", 0)
                                    logger.info(f"Total pages reported by API: {total_pages}")
                                
                                # Check for both possible response structures
                                items_list = []
                                if isinstance(data, dict):
                                    if "_embedded" in data:
                                        if "market_sales" in data["_embedded"]:
                                            items_list = data["_embedded"]["market_sales"]
                                            logger.info(f"Found {len(items_list)} sales in market_sales")
                                        elif "market_buyorders" in data["_embedded"]:  # API sometimes uses this key for sales too
                                            items_list = data["_embedded"]["market_buyorders"]
                                            logger.info(f"Found {len(items_list)} sales in market_buyorders")
                                            
                                # Log detailed pagination information
                                page_info = data.get("page", {})
                                if page_info:
                                    logger.info(f"Page info: size={page_info.get('size')}, totalElements={page_info.get('totalElements')}, " +
                                               f"totalPages={page_info.get('totalPages')}, number={page_info.get('number')} of {total_pages}")
                                    
                                # Process items from the current page
                                for item_data in items_list:
                                    if item_data:  # Make sure item_data is not None
                                        owner_id = item_data.get("ownerId")
                                        material_type = item_data.get("type", "Unknown")
                                        
                                        # If both filters are active, only add matching items
                                        if filtered_by_item and item_upper and material_type.upper() != item_upper:
                                            continue
                                            
                                        if owner_id and not seller:  # Only track seller IDs if we're not already filtering by seller
                                            seller_ids.add(owner_id)
                                        
                                        all_items.append({
                                            "material": material_type,
                                            "price": round(item_data.get("price", 0)),
                                            "quantity": item_data.get("quantity", 0),
                                            "ownerId": owner_id
                                        })
                                
                                # Detailed logging of link structure
                                links = data.get("_links", {})
                                logger.info(f"Available links: {list(links.keys())}")
                                if "next" in links:
                                    logger.info(f"Next link details: {links['next']}")
                                else:
                                    logger.info("No 'next' link found in response")
                                    
                                # Force maximum fetched pages if not filtering
                                max_pages_to_fetch = 200  # Increased to handle hundreds of pages
                                if not item and not seller and pages_retrieved >= max_pages_to_fetch:
                                    logger.info(f"Reached maximum page fetch limit ({max_pages_to_fetch}) for unfiltered results")
                                    next_url = None
                                    break
                                    
                                # Stop if we've collected enough items
                                max_items = 2000  # Increased to show more items
                                if len(all_items) >= max_items and (not item and not seller):
                                    logger.info(f"Reached maximum items limit ({max_items}) for unfiltered results")
                                    next_url = None
                                    break
                                
                                # Get next page URL if available - ensuring we don't miss any
                                next_link = None
                                if "_links" in data and "next" in data["_links"]:
                                    next_info = data["_links"]["next"]
                                    if isinstance(next_info, dict) and "href" in next_info:
                                        next_link = next_info["href"]
                                        
                                if next_link:
                                    # Handle both relative and absolute URLs
                                    if next_link.startswith("/"):
                                        next_url = f"https://api.lokamc.com{next_link}"
                                    elif next_link.startswith("http"):
                                        next_url = next_link
                                    else:
                                        next_url = f"https://api.lokamc.com/{next_link}"
                                    
                                    # Make sure we're requesting a large page size
                                    if "size=" in next_url:
                                        # Replace the size parameter with our desired size
                                        parts = next_url.split("size=")
                                        if len(parts) > 1:
                                            size_part = parts[1].split("&")[0]
                                            next_url = next_url.replace(f"size={size_part}", "size=200")
                                    else:
                                        # Add size parameter if not present
                                        next_url = next_url + ("&" if "?" in next_url else "?") + "size=200"
                                        
                                    logger.info(f"Next page URL (modified): {next_url}")
                                    
                                    # Add a small delay between page requests to avoid rate limiting
                                    await asyncio.sleep(0.2)
                                else:
                                    logger.info("No next page found or reached the end")
                                    next_url = None
                            elif response.status == 429:  # Rate limited
                                retry_count += 1
                                logger.warning(f"Rate limited. Retry {retry_count}/{max_retries}. Waiting 2 seconds...")
                                await asyncio.sleep(2)  # Wait before retrying
                                continue  # Try again with the same URL
                            else:
                                logger.error(f"API request failed with status: {response.status}")
                                if retry_count < max_retries - 1:
                                    retry_count += 1
                                    logger.warning(f"Retrying {retry_count}/{max_retries}. Waiting 2 seconds...")
                                    await asyncio.sleep(2)  # Wait before retrying
                                    continue  # Try again with the same URL
                                else:
                                    await interaction.followup.send(f"API request failed with status: {response.status}")
                                    return
                    except aiohttp.ClientError as e:
                        logger.error(f"API request error: {e}")
                        if retry_count < max_retries - 1:
                            retry_count += 1
                            logger.warning(f"Retrying {retry_count}/{max_retries}. Waiting 2 seconds...")
                            await asyncio.sleep(2)  # Wait before retrying
                            continue  # Try again with the same URL
                        else:
                            await interaction.followup.send("Failed to fetch market data. Please try again later.")
                            return
            except Exception as e:
                logger.error(f"Unexpected error in sales command: {e}")
                await interaction.followup.send("An unexpected error occurred. Please try again later.")
            
            # Log pagination summary
            logger.info(f"Pagination summary: Retrieved {pages_retrieved} pages of sales. Total items: {len(all_items)}")
            
            # After fetching all orders, no need to filter again as we already filtered during processing
            logger.info(f"Total sales found: {len(all_items)}")
            
            if not all_items:
                if seller_name and item:
                    await interaction.followup.send(f"No sales found for item '{item}' from player '{seller_name}'")
                elif seller_name:
                    await interaction.followup.send(f"No sales found for player '{seller_name}'")
                elif seller:
                    await interaction.followup.send(f"No sales found for player '{seller}'")
                elif item:
                    await interaction.followup.send(f"No sales found for item '{item}'")
                else:
                    await interaction.followup.send("No sales found. This could be due to an API issue. Please try again later.")
                return
            
            # If we're not filtering by seller, try to populate seller names
            seller_names = {}
            if not seller and client.seller_lookup:
                logger.info(f"Need to look up {len(seller_ids)} unique sellers")
                
                # First check cache for sellers we already know
                for seller_id in list(seller_ids):
                    if seller_id in client.player_cache.players:
                        seller_data = client.player_cache.players[seller_id]
                        if seller_data and "name" in seller_data:
                            seller_names[seller_id] = seller_data["name"]
                            seller_ids.remove(seller_id)
                
                logger.info(f"Found {len(seller_names)} sellers in cache, {len(seller_ids)} still need lookup")
                
                # If we still have sellers to look up, only fetch a limited number to avoid rate limits
                max_lookups = min(5, len(seller_ids))  # Limit to 5 seller lookups per command
                if seller_ids and max_lookups > 0:
                    # Create a new session for seller lookups
                    async with aiohttp.ClientSession() as seller_session:
                        for seller_id in list(seller_ids)[:max_lookups]:
                            try:
                                player_url = f"https://api.lokamc.com/players/{seller_id}"
                                async with seller_session.get(player_url) as player_response:
                                    if player_response.status == 200:
                                        seller_data = await player_response.json()
                                        if seller_data and "name" in seller_data:
                                            seller_names[seller_id] = seller_data["name"]
                                            # Add to cache for future use
                                            client.player_cache.players[seller_id] = seller_data
                                            # Save cache immediately to avoid losing this info
                                            client.player_cache.save_cache()
                            except Exception as e:
                                logger.error(f"Error fetching player {seller_id}: {e}")
                            # Add a small delay between requests
                            await asyncio.sleep(0.5)

            items_per_page = 10
            current_page = 0
            sort_mode = "default"  # Default sort mode
            
            # Create a copy of all_items for sorting
            sorted_items = list(all_items)
            
            def sort_items(mode):
                nonlocal sorted_items
                # Create a fresh copy of the original items
                sorted_items = list(all_items)
                
                if mode == "price_low_high":
                    # Sort by price (low to high)
                    sorted_items.sort(key=lambda x: x.get("price", 0))
                elif mode == "price_high_low":
                    # Sort by price (high to low)
                    sorted_items.sort(key=lambda x: x.get("price", 0), reverse=True)
                # Default mode uses the original order from the API
                
                return sorted_items

            # Initial sort with default mode
            sorted_items = sort_items(sort_mode)
            
            # Calculate number of pages based on sorted items
            num_pages = (len(sorted_items) + items_per_page - 1) // items_per_page

            async def update_embed(page: int):
                start_index = page * items_per_page
                end_index = min(start_index + items_per_page, len(sorted_items))
                items = sorted_items[start_index:end_index]

                title = "Market Sales"
                if seller_name and item:
                    title = f"Sales for {item_upper} by {seller_name}"
                elif seller_name:
                    title = f"Sales by {seller_name}"
                elif seller:
                    title = f"Sales by {seller}"
                elif item:
                    title = f"Sales for {item_upper}"

                embed = discord.Embed(title=title, color=discord.Color.green())
                
                # If we're filtering by a specific seller, set their head as the thumbnail
                if seller_name:
                    embed.set_thumbnail(url=f"https://mc-heads.net/head/{seller_name}")
                    embed.set_author(name=f"{seller_name}'s Sales", icon_url=f"https://mc-heads.net/avatar/{seller_name}/32")
                
                # Handle case where no items were found
                if not items:
                    embed.add_field(
                        name="No Items Found",
                        value="No sales matching your criteria were found.",
                        inline=False
                    )
                    return embed
                
                for item_data in items:
                    name = item_data.get("material", "Unknown Material")
                    price = item_data.get("price", 0)
                    quantity = item_data.get("quantity", 0)
                    
                    # If we're not already filtering by seller, show the owner's name
                    if not seller and item_data.get("ownerId"):
                        owner_id = item_data["ownerId"]
                        # Use our lookup results
                        owner_name = seller_names.get(owner_id, "Unknown")
                        
                        # Create a field with seller information
                        value = f"Price: {price}<:PowerShard:1356559399409422336> | Quantity: {quantity}"
                        if owner_name and owner_name != "Unknown":
                            value += f"\nSeller: **{owner_name}**"
                            # Add a seller-specific icon to the embed if not already set
                            if not embed.thumbnail and page == 0 and item_data == items[0]:
                                embed.set_thumbnail(url=f"https://mc-heads.net/head/{owner_name}")
                        else:
                            value += "\nSeller: Unknown"
                            
                        embed.add_field(
                            name=name,
                            value=value,
                            inline=False
                        )
                    else:
                        embed.add_field(
                            name=name,
                            value=f"Price: {price}<:PowerShard:1356559399409422336> | Quantity: {quantity}",
                            inline=False
                        )

                # Add sort mode to footer
                sort_text = "Default Order"
                if sort_mode == "price_low_high":
                    sort_text = "Price: Low to High"
                elif sort_mode == "price_high_low":
                    sort_text = "Price: High to Low"
                    
                footer_text = f"Sort: {sort_text}"
                if len(sorted_items) > items_per_page:
                    footer_text += f" | Page {current_page + 1} of {num_pages}"
                    
                embed.set_footer(text=footer_text)
                return embed

            async def button_callback(interaction: discord.Interaction, page_num: int):
                nonlocal current_page
                current_page = page_num
                embed = await update_embed(current_page)
                
                # Update the buttons when page changes
                await update_view_and_send(interaction, "edit")

            async def sort_callback(interaction: discord.Interaction, new_sort_mode: str):
                nonlocal sort_mode, current_page, num_pages, sorted_items
                
                # Update sort mode
                sort_mode = new_sort_mode
                logger.info(f"Sorting changed to: {sort_mode}")
                
                # Reset to first page
                current_page = 0
                
                # Sort items again
                sorted_items = sort_items(sort_mode)
                
                # Recalculate pages
                num_pages = (len(sorted_items) + items_per_page - 1) // items_per_page
                
                # Update embed
                embed = await update_embed(current_page)
                
                # Update view and send
                await update_view_and_send(interaction, "edit")
                
            async def update_view_and_send(interaction: discord.Interaction, action="send"):
                # Create new view
                view = discord.ui.View(timeout=120)
                
                # Add sort dropdown
                sort_dropdown = discord.ui.Select(
                    placeholder="Sort by...",
                    options=[
                        discord.SelectOption(label="Default Order", value="default", default=(sort_mode == "default")),
                        discord.SelectOption(label="Price: Low to High", value="price_low_high", default=(sort_mode == "price_low_high")),
                        discord.SelectOption(label="Price: High to Low", value="price_high_low", default=(sort_mode == "price_high_low"))
                    ],
                    min_values=1,
                    max_values=1
                )
                
                sort_dropdown.callback = lambda i: sort_callback(i, sort_dropdown.values[0])
                view.add_item(sort_dropdown)
                
                # Add pagination buttons
                prev_button_disabled = (current_page == 0)
                next_button_disabled = (current_page >= num_pages - 1)
                
                prev_page_button = discord.ui.Button(label="Previous", style=discord.ButtonStyle.secondary, disabled=prev_button_disabled, row=1)
                next_page_button = discord.ui.Button(label="Next", style=discord.ButtonStyle.secondary, disabled=next_button_disabled, row=1)

                prev_page_button.callback = lambda i: button_callback(i, max(0, current_page - 1))
                next_page_button.callback = lambda i: button_callback(i, min(num_pages - 1, current_page + 1))

                # Add buttons to view
                view.add_item(prev_page_button)
                view.add_item(next_page_button)
                view.timeout = 120  # Set timeout to 120 seconds
                
                # Create embed
                embed = await update_embed(current_page)
                
                # Send or edit message
                if action == "send":
                    await interaction.followup.send(embed=embed, view=view)
                else:
                    await interaction.response.edit_message(embed=embed, view=view)

            # Initial send
            await update_view_and_send(interaction, "send")
            
    except Exception as e:
        logger.error(f"Error in sales command: {e}")
        try:
            await interaction.followup.send("An error occurred while processing your request. Please try again later.")
        except:
            pass  # If we can't send the error message, just log it

@client.tree.command(name="completedsales", description="Lists recently completed sales from the Loka Market")
@app_commands.describe(
    item="Item type to filter by (optional)",
    player="Player name to see their completed sales (optional)"
)
async def completed_sales(interaction: discord.Interaction, item: str = None, player: str = None):
    await interaction.response.defer()
    
    logger.info(f"Completed sales requested by {interaction.user.name} with item: {item}, player: {player}")
    
    # Convert item to uppercase for comparison
    item_upper = item.upper() if item else None
    
    # If player is specified, find their ID
    player_id = None
    player_name = None
    
    if player:
        try:
            logger.info(f"Searching for player: '{player}'")
            found_player = await client.search_player_by_name(player)
            
            if found_player and isinstance(found_player, dict):
                player_id = found_player.get("id")
                player_name = found_player.get("name")
                
                if not player_id:
                    logger.warning(f"Found player but ID is missing: {found_player}")
                    await interaction.followup.send(f"Found player '{player}' but couldn't get their ID. Please try again.")
                    return
            else:
                logger.warning(f"Could not find player with name '{player}'")
                await interaction.followup.send(f"Could not find player with name '{player}'")
                return
        except Exception as e:
            logger.error(f"Error looking up player '{player}': {e}")
            await interaction.followup.send(f"Error looking up player '{player}'. Please try again.")
            return
    
    # Get completed sales from API
    all_items = []
    
    async with aiohttp.ClientSession() as session:
        # Build the API URL based on filters
        if player_id:
            next_url = f"https://api.lokamc.com/market_completed_sales/search/findByOwnerId?id={player_id}&size=100"
        else:
            next_url = "https://api.lokamc.com/market_completed_sales/search/findAllSales?size=100"
        
        logger.info(f"Fetching completed sales from URL: {next_url}")
        
        # Process API response
        try:
            async with session.get(next_url) as response:
                if response.status == 200:
                    data = await response.json()
                    
                    # Log the data structure to debug
                    logger.info(f"Response structure: {list(data.keys()) if isinstance(data, dict) else 'Not a dict'}")
                    
                    # Extract items from the response
                    items_list = []
                    if isinstance(data, dict) and "_embedded" in data:
                        if "market_completed_sales" in data["_embedded"]:
                            items_list = data["_embedded"]["market_completed_sales"]
                            logger.info(f"Found {len(items_list)} completed sales")
                    
                    # Process each completed sale
                    for item_data in items_list:
                        # Skip if not valid item data
                        if not item_data:
                            continue
                        
                        material_type = item_data.get("type", "Unknown")
                        
                        # Apply item filter if specified
                        if item_upper and material_type.upper() != item_upper:
                            continue
                        
                        # Add to our list of items
                        all_items.append({
                            "id": item_data.get("id"),
                            "material": material_type,
                            "price": round(item_data.get("price", 0)),
                            "quantity": item_data.get("quantity", 0),
                            "seller_id": item_data.get("ownerId"),
                            "buyer_id": item_data.get("buyerId"),
                            "timestamp": item_data.get("timestamp", 0)
                        })
                    
                    # Sort by most recent first (based on timestamp)
                    all_items.sort(key=lambda x: x.get("timestamp", 0), reverse=True)
                    
                    logger.info(f"Processed {len(all_items)} completed sales matching filters")
                else:
                    logger.error(f"API request failed with status: {response.status}")
                    await interaction.followup.send(f"API request failed with status: {response.status}")
                    return
        except Exception as e:
            logger.error(f"Error fetching completed sales: {e}")
            await interaction.followup.send("Failed to fetch completed sales data. Please try again later.")
            return
    
    # Check if we found any sales
    if not all_items:
        if player_name and item:
            await interaction.followup.send(f"No completed sales found for item '{item}' by player '{player_name}'")
        elif player_name:
            await interaction.followup.send(f"No completed sales found for player '{player_name}'")
        elif item:
            await interaction.followup.send(f"No completed sales found for item '{item}'")
        else:
            await interaction.followup.send("No completed sales found.")
        return
    
    # Set up pagination
    items_per_page = 10
    num_pages = (len(all_items) + items_per_page - 1) // items_per_page
    current_page = 0
    
    # Try to populate seller names if not filtered by player
    seller_names = {}
    seller_ids = set()
    
    if not player and client.seller_lookup:
        for item_data in all_items:
            seller_id = item_data.get("seller_id")
            if seller_id:
                seller_ids.add(seller_id)
        
        logger.info(f"Need to look up {len(seller_ids)} unique sellers")
        
        # First check cache for sellers we already know
        for seller_id in list(seller_ids):
            if seller_id in client.player_cache.players:
                seller = client.player_cache.players[seller_id]
                if seller and "name" in seller:
                    seller_names[seller_id] = seller["name"]
                    seller_ids.remove(seller_id)
        
        logger.info(f"Found {len(seller_names)} sellers in cache, {len(seller_ids)} still need lookup")
        
        # If we still have sellers to look up, only fetch a limited number to avoid rate limits
        max_lookups = min(5, len(seller_ids))  # Limit to 5 seller lookups per command
        if seller_ids and max_lookups > 0:
            # Create a new session for seller lookups
            async with aiohttp.ClientSession() as seller_session:
                for seller_id in list(seller_ids)[:max_lookups]:
                    try:
                        player_url = f"https://api.lokamc.com/players/{seller_id}"
                        async with seller_session.get(player_url) as player_response:
                            if player_response.status == 200:
                                seller_data = await player_response.json()
                                if seller_data and "name" in seller_data:
                                    seller_names[seller_id] = seller_data["name"]
                                    # Add to cache for future use
                                    client.player_cache.players[seller_id] = seller_data
                                    # Save cache immediately to avoid losing this info
                                    client.player_cache.save_cache()
                    except Exception as e:
                        logger.error(f"Error fetching player {seller_id}: {e}")
                    # Add a small delay between requests
                    await asyncio.sleep(0.5)
    
    async def update_embed(page: int):
        start_index = page * items_per_page
        end_index = min(start_index + items_per_page, len(all_items))
        items = all_items[start_index:end_index]
        
        # Create appropriate title
        title = "Completed Sales"
        if player_name and item:
            title = f"Completed Sales for {item_upper} by {player_name}"
        elif player_name:
            title = f"Completed Sales by {player_name}"
        elif item:
            title = f"Completed Sales for {item_upper}"
        
        embed = discord.Embed(title=title, color=discord.Color.blue())
        
        # If showing for a specific player, set their head as thumbnail
        if player_name:
            embed.set_thumbnail(url=f"https://mc-heads.net/head/{player_name}")
        
        # Handle case where no items were found
        if not items:
            embed.add_field(
                name="No Sales Found",
                value="No completed sales matching your criteria were found.",
                inline=False
            )
            return embed
        
        # Add each completed sale to the embed
        for item_data in items:
            material = item_data.get("material", "Unknown")
            price = item_data.get("price", 0)
            quantity = item_data.get("quantity", 0)
            
            # Format timestamp if available
            timestamp = item_data.get("timestamp", 0)
            timestamp_str = "Unknown time"
            if timestamp:
                try:
                    # Convert timestamp (usually in milliseconds) to readable format
                    dt = datetime.fromtimestamp(timestamp / 1000)  # Convert ms to seconds
                    timestamp_str = f"<t:{int(timestamp/1000)}:R>"
                except Exception as e:
                    logger.error(f"Error formatting timestamp: {e}")
                    timestamp_str = "Unknown time"
            
            # Include seller name if available and not filtered by player
            value = f"Price: {price}<:PowerShard:1356559399409422336> | Quantity: {quantity}"
            if not player:
                seller_id = item_data.get("seller_id")
                seller_name = seller_names.get(seller_id, "Unknown")
                if seller_name != "Unknown":
                    value += f"\nSeller: **{seller_name}**"
            
            value += f"\nCompleted: {timestamp_str}"
            
            embed.add_field(
                name=material,
                value=value,
                inline=False
            )
        
        if len(all_items) > items_per_page:
            embed.set_footer(text=f"Page {current_page + 1} of {num_pages}")
        return embed
    
    async def button_callback(interaction: discord.Interaction, page_num: int):
        nonlocal current_page
        current_page = page_num
        embed = await update_embed(current_page)
        
        # Update the buttons when page changes
        prev_button_disabled = (current_page == 0)
        next_button_disabled = (current_page >= num_pages - 1)
        
        # Recreate view with updated button states
        view = discord.ui.View(timeout=60)
        prev_button = discord.ui.Button(label="Previous", style=discord.ButtonStyle.secondary, disabled=prev_button_disabled)
        next_button = discord.ui.Button(label="Next", style=discord.ButtonStyle.secondary, disabled=next_button_disabled)
        
        prev_button.callback = lambda i: button_callback(i, max(0, current_page - 1))
        next_button.callback = lambda i: button_callback(i, min(num_pages - 1, current_page + 1))
        
        view.add_item(prev_button)
        view.add_item(next_button)
        
        await interaction.response.edit_message(embed=embed, view=view)
    
    # Create initial buttons with proper disabled states
    prev_button_disabled = (current_page == 0)
    next_button_disabled = (current_page >= num_pages - 1)
    
    view = discord.ui.View(timeout=60)
    prev_page_button = discord.ui.Button(label="Previous", style=discord.ButtonStyle.secondary, disabled=prev_button_disabled)
    next_page_button = discord.ui.Button(label="Next", style=discord.ButtonStyle.secondary, disabled=next_button_disabled)
    
    prev_page_button.callback = lambda i: button_callback(i, max(0, current_page - 1))
    next_page_button.callback = lambda i: button_callback(i, min(num_pages - 1, current_page + 1))
    
    # Add buttons to view
    view.add_item(prev_page_button)
    view.add_item(next_page_button)
    view.timeout = 60  # Set timeout to 60 seconds
    
    embed = await update_embed(current_page)
    await interaction.followup.send(embed=embed, view=view)

client.run(os.getenv("BOT_TOKEN"))
