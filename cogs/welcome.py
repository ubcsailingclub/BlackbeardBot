import discord
from discord.ext import commands

import config


class WelcomeCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member) -> None:
        """Send a welcome DM to new members when they join the server."""

        # Resolve channels by name so links work in DMs
        verify_ch = discord.utils.get(
            member.guild.text_channels, name=config.VERIFY_CHANNEL_NAME
        )
        roles_ch = discord.utils.get(
            member.guild.text_channels, name=config.GET_ROLES_CHANNEL_NAME
        )

        verify_link = f"<#{verify_ch.id}>" if verify_ch else f"#{config.VERIFY_CHANNEL_NAME}"
        roles_link = f"<#{roles_ch.id}>" if roles_ch else f"#{config.GET_ROLES_CHANNEL_NAME}"

        message = (
            f"Hi! Thanks for joining the UBC Sailing discord!\n\n"
            f"Unverified members can only see a few channels. "
            f"To get full access, head over to {verify_link} and verify your "
            f"membership by connecting your discord account to your "
            f"ubcsailing.org account.\n\n"
            f"Once you're verified, you can use {roles_link} to add roles to "
            f"yourself and unlock fleet, community, and waitlist channels.\n\n"
            f"See you on the water! ⛵"
        )

        try:
            await member.send(message)
        except discord.Forbidden:
            # Member has DMs disabled for this server — skip silently
            pass
        except discord.HTTPException:
            pass


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(WelcomeCog(bot))