import os
import asyncio
from typing import Optional
import discord
from discord.ext import commands
from loguru import logger

class DiscordService:
    def __init__(self):
        intents = discord.Intents.default()
        intents.message_content = True
        intents.voice_states = True
        
        self.bot = commands.Bot(command_prefix="!", intents=intents)
        self.voice_clients = {}
        
        # Register event handlers
        @self.bot.event
        async def on_ready():
            logger.info(f"Bot connected as {self.bot.user}")
            
        @self.bot.command()
        async def join(ctx):
            """Join the user's voice channel"""
            if ctx.author.voice:
                channel = ctx.author.voice.channel
                await channel.connect()
                await ctx.send("👋 I've joined your voice channel! Say something!")
            else:
                await ctx.send("You need to be in a voice channel first!")
                
        @self.bot.command()
        async def leave(ctx):
            """Leave the voice channel"""
            if ctx.voice_client:
                await ctx.voice_client.disconnect()
                await ctx.send("👋 Goodbye!")
            else:
                await ctx.send("I'm not in a voice channel!")

        @self.bot.event
        async def on_message(message):
            # Don't respond to our own messages
            if message.author == self.bot.user:
                return

            # Process commands
            await self.bot.process_commands(message)
            
            # Handle regular messages
            if not message.content.startswith('!'):
                # Here you would integrate with your agent logic
                response = f"I received your message: {message.content}"
                await message.channel.send(response)

        @self.bot.event
        async def on_voice_state_update(member, before, after):
            """Handle voice state changes"""
            if member == self.bot.user:
                return
                
            # Handle user joining/leaving voice channels
            if before.channel != after.channel:
                if after.channel:
                    logger.info(f"User {member.name} joined voice channel {after.channel.name}")
                if before.channel:
                    logger.info(f"User {member.name} left voice channel {before.channel.name}")

    async def start(self):
        """Start the Discord bot"""
        try:
            await self.bot.start(os.getenv("DISCORD_BOT_TOKEN"))
        except Exception as e:
            logger.error(f"Failed to start Discord bot: {e}")
            raise

    async def stop(self):
        """Stop the Discord bot"""
        try:
            await self.bot.close()
        except Exception as e:
            logger.error(f"Error stopping Discord bot: {e}")
            raise

    def is_connected(self):
        """Check if the bot is connected"""
        return self.bot.is_ready() 