import asyncio
import logging
from typing import Optional

import discord
from discord.ext import commands

log = logging.getLogger(__name__)

# -----------------------
# CONFIG (edit these)
# -----------------------
FORUM_CHANNEL_ID = 1400306843401850972

PENDING_TAG_NAME = "Pending"
COMPLETE_TAG_NAME = "Complete"

CHECK_EMOJI = "☑️"  # :ballot_box_with_check:

ALLOWED_ROLE_NAMES = {
    "anton",
    "Boatswain",
    "Steering Committee",
    "Fleet Captain",
}

# Custom ID must be stable for persistent views
BTN_COMPLETE_CUSTOM_ID = "oops_broke_mark_complete_v1"
BTN_PENDING_CUSTOM_ID = "oops_broke_mark_pending_v1"

def _has_allowed_role(member: discord.Member) -> bool:
    role_names = {r.name.casefold() for r in member.roles}
    return any(name.casefold() in role_names for name in ALLOWED_ROLE_NAMES)


def _is_target_forum_thread(thread: discord.Thread) -> bool:
    return thread.parent_id == FORUM_CHANNEL_ID


def _find_forum_tag(parent: discord.ForumChannel, name: str) -> Optional[discord.ForumTag]:
    target = name.casefold()
    for tag in parent.available_tags:
        if tag.name.casefold() == target:
            return tag
    return None


async def _set_status_tag(thread: discord.Thread, *, status: str) -> bool:
    """
    status: "pending" or "complete"
    Returns True if tag edit was attempted successfully, False otherwise.
    """
    if not isinstance(thread.parent, discord.ForumChannel):
        return False

    parent: discord.ForumChannel = thread.parent

    pending_tag = _find_forum_tag(parent, PENDING_TAG_NAME)
    complete_tag = _find_forum_tag(parent, COMPLETE_TAG_NAME)

    if pending_tag is None or complete_tag is None:
        log.warning(
            "Missing required forum tags in #%s (need '%s' and '%s').",
            getattr(parent, "name", "unknown"),
            PENDING_TAG_NAME,
            COMPLETE_TAG_NAME,
        )
        return False

    keep_ids = {t.id for t in (thread.applied_tags or [])}
    # Remove both status tags from the "keep" set
    keep_ids.discard(pending_tag.id)
    keep_ids.discard(complete_tag.id)

    # Rebuild kept tags as ForumTag objects from parent.available_tags
    kept_tags = [t for t in parent.available_tags if t.id in keep_ids]

    if status == "pending":
        new_tags = kept_tags + [pending_tag]
    elif status == "complete":
        new_tags = kept_tags + [complete_tag]
    else:
        raise ValueError("status must be 'pending' or 'complete'")

    try:
        await thread.edit(applied_tags=new_tags)
        return True
    except discord.Forbidden:
        log.warning("Forbidden: cannot edit tags for thread %s. Check forum 'Manage Posts/Threads' perms.", thread.id)
        return False
    except discord.HTTPException as e:
        log.exception("HTTPException editing tags for thread %s: %s", thread.id, e)
        return False


async def _react_to_starter_message(thread: discord.Thread) -> None:
    """
    For forum posts, the starter message can be fetched by message_id == thread.id. :contentReference[oaicite:4]{index=4}
    """
    # Sometimes the message isn't immediately fetchable right at creation; retry once.
    for attempt in (1, 2):
        try:
            starter = await thread.fetch_message(thread.id)
            await starter.add_reaction(CHECK_EMOJI)
            return
        except discord.NotFound:
            if attempt == 1:
                await asyncio.sleep(0.8)
                continue
        except discord.Forbidden:
            log.warning("Forbidden: cannot add reaction in thread %s (missing perms?)", thread.id)
            return
        except discord.HTTPException as e:
            log.exception("HTTPException adding reaction in thread %s: %s", thread.id, e)
            return


class OopsMarkCompleteView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    async def _auth_and_get_thread(self, interaction: discord.Interaction) -> Optional[discord.Thread]:
        if not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message("This action is only available in-server.", ephemeral=True)
            return None

        if not _has_allowed_role(interaction.user):
            await interaction.response.send_message("You don’t have permission to change status.", ephemeral=True)
            return None

        if not isinstance(interaction.channel, discord.Thread):
            await interaction.response.send_message("This can only be used inside a forum post.", ephemeral=True)
            return None

        thread: discord.Thread = interaction.channel
        if not _is_target_forum_thread(thread):
            await interaction.response.send_message("This isn’t the tracked forum channel.", ephemeral=True)
            return None

        return thread

    @discord.ui.button(
        label="Mark complete",
        style=discord.ButtonStyle.success,
        emoji="✅",
        custom_id=BTN_COMPLETE_CUSTOM_ID,
    )
    async def mark_complete(self, interaction: discord.Interaction, button: discord.ui.Button):
        thread = await self._auth_and_get_thread(interaction)
        if thread is None:
            return

        ok = await _set_status_tag(thread, status="complete")
        if ok:
            await interaction.response.send_message("Status set to **Complete**.", ephemeral=True)
        else:
            await interaction.response.send_message("I couldn’t update tags (missing tags or permissions).", ephemeral=True)

    @discord.ui.button(
        label="Reopen (Pending)",
        style=discord.ButtonStyle.secondary,
        emoji="↩️",
        custom_id=BTN_PENDING_CUSTOM_ID,
    )
    async def mark_pending(self, interaction: discord.Interaction, button: discord.ui.Button):
        thread = await self._auth_and_get_thread(interaction)
        if thread is None:
            return

        ok = await _set_status_tag(thread, status="pending")
        if ok:
            await interaction.response.send_message("Status set to **Pending**.", ephemeral=True)
        else:
            await interaction.response.send_message("I couldn’t update tags (missing tags or permissions).", ephemeral=True)


class OopsSomethingBrokeCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        # Register persistent view so old buttons keep working after restart. :contentReference[oaicite:6]{index=6}
        self.bot.add_view(OopsMarkCompleteView())

    @commands.Cog.listener()
    async def on_thread_create(self, thread: discord.Thread):
        # on_thread_create requires Intents.guilds :contentReference[oaicite:7]{index=7}
        if not _is_target_forum_thread(thread):
            return

        # 1) Tag as Pending
        await _set_status_tag(thread, status="pending")

        # 2) React with ballot box with check on starter message
        await _react_to_starter_message(thread)

        # 3) Post a small control message with a "Mark complete" button (optional but useful)
        try:
            await thread.send(
                "Status set to **Pending**.\n"
                "Authorized roles can set **Complete** or revert to **Pending** using the buttons below.",
                view=OopsMarkCompleteView(),
                silent=True,
            )
        except discord.Forbidden:
            log.warning("Forbidden: cannot send control message in thread %s", thread.id)
        except discord.HTTPException as e:
            log.exception("HTTPException sending control message in thread %s: %s", thread.id, e)

    @commands.Cog.listener()
    async def on_raw_reaction_add(self, payload: discord.RawReactionActionEvent):
        # Raw reaction events require Intents.reactions :contentReference[oaicite:8]{index=8}
        if str(payload.emoji) != CHECK_EMOJI:
            return
        if payload.guild_id is None:
            return

        guild = self.bot.get_guild(payload.guild_id)
        if guild is None:
            return

        # Ignore the bot's own reaction
        if payload.user_id == self.bot.user.id:
            return

        # The channel_id for a forum post reaction will be the thread channel
        channel = guild.get_channel(payload.channel_id)
        if channel is None:
            try:
                channel = await guild.fetch_channel(payload.channel_id)
            except discord.HTTPException:
                return

        if not isinstance(channel, discord.Thread):
            return

        thread: discord.Thread = channel
        if not _is_target_forum_thread(thread):
            return

        # Only treat reactions on the starter message as "complete"
        if payload.message_id != thread.id:
            return

        member = payload.member
        if member is None:
            # fallback: fetch member
            try:
                member = await guild.fetch_member(payload.user_id)
            except discord.HTTPException:
                return

        if not _has_allowed_role(member):
            return

        await _set_status_tag(thread, status="complete")


async def setup(bot: commands.Bot):
    await bot.add_cog(OopsSomethingBrokeCog(bot))
