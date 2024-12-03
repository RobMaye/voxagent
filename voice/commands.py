from discord.ext import commands
from loguru import logger

class VoiceCommands(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @commands.command()
    async def audio_status(self, ctx):
        """Show current audio settings."""
        if not ctx.voice_client or not hasattr(ctx.voice_client, 'get_audio_settings'):
            await ctx.send("Not connected to a voice channel or voice client doesn't support settings.")
            return
        
        settings = ctx.voice_client.get_audio_settings()
        if not settings:
            await ctx.send("No audio settings available.")
            return
        
        status = "**Current Audio Settings:**\n"
        status += f"• Speed: {settings['speed']}x\n"
        status += f"• Pitch: {settings['pitch']}x\n"
        status += f"• Buffer Size: {settings['buffer_size']}ms\n"
        status += f"• Buffer Usage: {settings['buffer_used']}/{settings['buffer_size']/20:.0f} frames\n"
        
        await ctx.send(status)

    @commands.command()
    async def audio_adjust(self, ctx, setting: str, value: float):
        """
        Adjust audio settings in real-time.
        
        Settings:
        - speed: Playback speed (0.5 to 2.0)
        - pitch: Voice pitch (0.5 to 2.0)
        - buffer: Buffer size in milliseconds (50 to 500)
        
        Example:
        !audio_adjust speed 1.0  # Normal speed
        !audio_adjust pitch 1.0  # Normal pitch
        !audio_adjust buffer 100 # 100ms buffer
        """
        if not ctx.voice_client or not hasattr(ctx.voice_client, 'update_audio_settings'):
            await ctx.send("Not connected to a voice channel or voice client doesn't support settings.")
            return
        
        setting = setting.lower()
        if setting not in ['speed', 'pitch', 'buffer']:
            await ctx.send("Invalid setting. Use 'speed', 'pitch', or 'buffer'.")
            return
        
        # Validate and clamp values
        if setting in ['speed', 'pitch']:
            value = max(0.5, min(2.0, value))
        elif setting == 'buffer':
            value = max(50, min(500, value))
        
        # Convert setting name to kwargs
        kwargs = {
            'speed': 'speed',
            'pitch': 'pitch',
            'buffer': 'buffer_size'
        }
        
        try:
            new_settings = await ctx.voice_client.update_audio_settings(**{kwargs[setting]: value})
            if new_settings:
                await ctx.send(f"Updated {setting} to {value}")
                # Show full status after update
                await self.audio_status(ctx)
            else:
                await ctx.send("Failed to update settings.")
        except Exception as e:
            logger.error(f"Error updating audio settings: {e}")
            await ctx.send(f"Error updating settings: {str(e)}")

    @commands.command()
    async def audio_reset(self, ctx):
        """Reset audio settings to defaults."""
        if not ctx.voice_client or not hasattr(ctx.voice_client, 'update_audio_settings'):
            await ctx.send("Not connected to a voice channel or voice client doesn't support settings.")
            return
        
        try:
            new_settings = await ctx.voice_client.update_audio_settings(
                speed=1.0,
                pitch=1.0,
                buffer_size=100
            )
            if new_settings:
                await ctx.send("Reset audio settings to defaults.")
                # Show full status after reset
                await self.audio_status(ctx)
            else:
                await ctx.send("Failed to reset settings.")
        except Exception as e:
            logger.error(f"Error resetting audio settings: {e}")
            await ctx.send(f"Error resetting settings: {str(e)}")

async def setup(bot):
    await bot.add_cog(VoiceCommands(bot)) 