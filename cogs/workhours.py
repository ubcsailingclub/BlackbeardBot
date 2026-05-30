import asyncio
import datetime as dt
import logging
import re
from typing import Optional, Dict, Any, Tuple, List
from zoneinfo import ZoneInfo

import discord
from discord.ext import commands
from google.oauth2.service_account import Credentials
import gspread

import config

log = logging.getLogger(__name__)

# -------------------- helper functions --------------------

def _norm_name(s: str) -> str:
    """Normalize names to match string equality robust to spacing, punctuation, and casing."""
    s = (s or "").strip().lower()
    s = re.sub(r"[^a-z0-9\s]+", " ", s)  # drop punctuation
    s = re.sub(r"\s+", " ", s).strip()
    return s


def _parse_date(val: str) -> Optional[dt.date]:
    """Robust parser for various date formats from Google Sheets."""
    if not val:
        return None
    val = str(val).strip()
    # Common formats with 4-digit years
    for fmt in ("%m/%d/%Y", "%Y-%m-%d", "%d/%m/%Y", "%Y/%m/%d", "%m-%d-%Y", "%d-%m-%Y"):
        try:
            return dt.datetime.strptime(val, fmt).date()
        except ValueError:
            continue
    # Common formats with 2-digit years
    for fmt in ("%m/%d/%y", "%d/%m/%y", "%y-%m-%d"):
        try:
            return dt.datetime.strptime(val, fmt).date()
        except ValueError:
            continue
    return None


def _parse_timestamp(val: str) -> Optional[dt.date]:
    """Parse just the date part of a timestamp string."""
    if not val:
        return None
    val = str(val).strip().split()[0]
    return _parse_date(val)


def _parse_hours(val: str) -> float:
    """Robust parser to extract numerical digit from hours column."""
    if not val:
        return 0.0
    val = str(val).strip().lower()
    val = re.sub(r"[^\d\.-]", "", val)
    try:
        return float(val)
    except ValueError:
        return 0.0


def _parse_verified(val: str) -> bool:
    """Boolean parser for 'verified?' column in Google Sheets."""
    if not val:
        return False
    val = str(val).strip().lower()
    return val in ("true", "yes", "y", "verified", "1", "t", "x", "checked", "checked?")


def _get_current_season_bounds() -> Tuple[dt.date, dt.date]:
    """
    Get the start and end dates for the current season (April 1st to March 31st)
    based on America/Vancouver (Pacific Time).
    """
    try:
        tz = ZoneInfo("America/Vancouver")
    except Exception:
        # Fallback if tzdata is missing or platform doesn't support America/Vancouver
        tz = dt.timezone(dt.timedelta(hours=-7))  # Approximation (PDT)

    now = dt.datetime.now(tz).date()
    if now.month >= 4:
        start_year = now.year
    else:
        start_year = now.year - 1
    return dt.date(start_year, 4, 1), dt.date(start_year + 1, 3, 31)


# -------------------- Cog implementation --------------------

class WorkhoursCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    async def _find_verified_member_record(self, user_id: int, guild_id: Optional[int] = None) -> Optional[Dict[str, Any]]:
        """Search the WildApricot VerifiedRegistry across guilds to find this Discord user."""
        verify_cog = self.bot.get_cog("VerifyCog")
        if not verify_cog:
            log.warning("VerifyCog not loaded, cannot search registry.")
            return None

        registry = verify_cog.registry
        async with registry._lock:
            data = registry._load()
            guilds = data.get("guilds", {})

            # 1. Search specified guild first if available
            if guild_id:
                g = guilds.get(str(guild_id), {})
                user_map = g.get("discord_user_map", {})
                wa_id = user_map.get(str(user_id))
                if wa_id:
                    wa_map = g.get("wa_id_map", {})
                    record = wa_map.get(str(wa_id))
                    if isinstance(record, dict):
                        return record

            # 2. Check all other guilds if not found in current/specified guild
            for gid, g in guilds.items():
                if guild_id and str(gid) == str(guild_id):
                    continue
                user_map = g.get("discord_user_map", {})
                wa_id = user_map.get(str(user_id))
                if wa_id:
                    wa_map = g.get("wa_id_map", {})
                    record = wa_map.get(str(wa_id))
                    if isinstance(record, dict):
                        return record

        return None

    async def _get_or_fetch_wa_name(self, record: Dict[str, Any], user_id: int) -> Optional[str]:
        """Get the WA name from registry record, or retrieve it from WildApricot if missing."""
        wa_name = record.get("wa_full_name")
        if wa_name:
            return wa_name

        verify_cog = self.bot.get_cog("VerifyCog")
        if not verify_cog:
            return None

        wa_client = verify_cog.wa
        wa_contact_id = record.get("wa_contact_id")
        if not wa_contact_id:
            return None

        log.info(f"WA name missing in registry for contact_id={wa_contact_id}. Retrieving from WA...")
        try:
            contact = await wa_client.get_contact(wa_contact_id)
            if contact:
                wa_first = (contact.get("FirstName") or "").strip()
                wa_last = (contact.get("LastName") or "").strip()
                wa_full = f"{wa_first} {wa_last}".strip()

                if wa_full:
                    # Update name in registry for all guilds where user is mapped to this contact ID
                    registry = verify_cog.registry
                    async with registry._lock:
                        data = registry._load()
                        for gid, g in data.get("guilds", {}).items():
                            user_map = g.get("discord_user_map", {})
                            wa_map = g.get("wa_id_map", {})
                            if user_map.get(str(user_id)) == str(wa_contact_id):
                                wa_rec = wa_map.setdefault(str(wa_contact_id), {})
                                wa_rec["wa_full_name"] = wa_full
                        registry._atomic_write(data)
                    log.info(f"WA name successfully cached in registry: {wa_full}")
                    return wa_full
        except Exception as e:
            log.exception(f"Failed to fetch contact details from WA for contact_id={wa_contact_id}: {e}")

        return None

    async def _has_duplicate_name_conflict(self, target_name: str, discord_user_id: int) -> bool:
        """
        Check if there is another active, non-social member in the registry
        with the exact same normalized WildApricot full name.
        """
        verify_cog = self.bot.get_cog("VerifyCog")
        if not verify_cog:
            return False

        registry = verify_cog.registry
        target_norm = _norm_name(target_name)

        async with registry._lock:
            data = registry._load()
            guilds = data.get("guilds", {})

            for gid, g in guilds.items():
                wa_map = g.get("wa_id_map", {})
                for wa_id, record in wa_map.items():
                    if not isinstance(record, dict):
                        continue

                    # Skip the querying user
                    rec_user_id = record.get("discord_user_id")
                    if rec_user_id == discord_user_id:
                        continue

                    # Make sure the other contact is also an active, non-social member
                    status = (record.get("membership_status") or "").strip().lower()
                    level = (record.get("membership_level") or "").strip().lower()
                    if status != "active" or level == "social":
                        continue

                    name_norm = _norm_name(record.get("wa_full_name"))
                    if name_norm == target_norm:
                        log.warning(
                            f"[WORKHOURS] Duplicate name conflict found for name={target_name!r}. "
                            f"discord_user_id={discord_user_id} and another user discord_user_id={rec_user_id} "
                            f"both share this name."
                        )
                        return True

        return False

    def _get_verify_channel_mention(self, guild: Optional[discord.Guild] = None) -> str:
        """Resolve the clickable mention for the verification channel."""
        # 1. Search specified guild first
        if guild:
            ch = discord.utils.get(guild.text_channels, name=config.VERIFY_CHANNEL_NAME)
            if ch:
                return f"<#{ch.id}>"

        # 2. Search all bot guilds
        for g in self.bot.guilds:
            ch = discord.utils.get(g.text_channels, name=config.VERIFY_CHANNEL_NAME)
            if ch:
                return f"<#{ch.id}>"

        return f"#{config.VERIFY_CHANNEL_NAME}"

    def _fetch_sheet_rows_sync(self) -> List[List[str]]:
        """Synchronous Google Sheet reading method, to be run in a separate thread executor."""
        spreadsheet_id = config.GOOGLE_SPREADSHEET_ID
        if not spreadsheet_id:
            raise ValueError("GOOGLE_SPREADSHEET_ID is missing in your environment configuration.")

        creds_source = config.GOOGLE_SERVICE_ACCOUNT_JSON
        if not creds_source:
            raise ValueError("GOOGLE_SERVICE_ACCOUNT_JSON is missing in your environment configuration.")

        scopes = ["https://www.googleapis.com/auth/spreadsheets.readonly"]

        if creds_source.strip().startswith("{"):
            import json
            info = json.loads(creds_source)
            credentials = Credentials.from_service_account_info(info, scopes=scopes)
        else:
            credentials = Credentials.from_service_account_file(creds_source, scopes=scopes)

        client = gspread.authorize(credentials)
        sheet = client.open_by_key(spreadsheet_id)
        worksheet = sheet.worksheet(config.WORKHOURS_SHEET_NAME)
        return worksheet.get_all_values()

    async def _calculate_hours(self, wa_name: str) -> Tuple[float, float, List[Dict[str, Any]]]:
        """
        Fetch rows from Google Spreadsheet and aggregate verified vs. unverified hours
        for the given WildApricot name within the current season.
        """
        # Fetch spreadsheet rows in a thread-safe executor (doesn't block the main event loop)
        loop = asyncio.get_running_loop()
        rows = await loop.run_in_executor(None, self._fetch_sheet_rows_sync)

        if not rows or len(rows) <= 1:
            return 0.0, 0.0, []

        season_start, season_end = _get_current_season_bounds()
        target_name_norm = _norm_name(wa_name)

        verified_sum = 0.0
        unverified_sum = 0.0
        matching_submissions = []

        # Row indices for columns A-G:
        # A: 0 (timestamp), B: 1 (name), C: 2 (hours), D: 3 (desc), E: 4 (supervisor), F: 5 (date of work), G: 6 (verified)
        for row in rows[1:]:  # skip headers
            if len(row) < 2:
                continue

            name_norm = _norm_name(row[1])
            if name_norm != target_name_norm:
                continue

            # Parse date of work (with fallback to timestamp)
            work_date = None
            if len(row) > 5:
                work_date = _parse_date(row[5])
            if not work_date and len(row) > 0:
                work_date = _parse_timestamp(row[0])

            # Skip row if date is missing or not in current season
            if not work_date or not (season_start <= work_date <= season_end):
                continue

            hours = _parse_hours(row[2]) if len(row) > 2 else 0.0
            is_verified = _parse_verified(row[6]) if len(row) > 6 else False
            desc = row[3] if len(row) > 3 else ""
            supervisor = row[4] if len(row) > 4 else ""

            submission = {
                "date": work_date,
                "hours": hours,
                "description": desc,
                "supervisor": supervisor,
                "verified": is_verified
            }
            matching_submissions.append(submission)

            if is_verified:
                verified_sum += hours
            else:
                unverified_sum += hours

        return verified_sum, unverified_sum, matching_submissions

    @commands.hybrid_command(
        name="workhours",
        description="View your submitted and verified workhours for the season."
    )
    async def workhours(self, ctx: commands.Context) -> None:
        """
        Check your workhours for the season.
        Responds ephemerally if triggered in a channel, or normally in DMs.
        """
        # Determine if we should handle response ephemerally
        is_dm = ctx.guild is None
        ephemeral = not is_dm

        # If it's a prefix command in a channel, we cannot send a true ephemeral response,
        # so we will send the user a DM and let them know.
        # If it's a prefix command in a channel (ctx.interaction is None and not is_dm),
        # we will send the response in DMs, but print a small friendly note in the channel.
        is_slash = ctx.interaction is not None

        guild_id = ctx.guild.id if ctx.guild else None
        user = ctx.author

        # Acknowledge immediately if Slash Command to prevent timeouts
        if is_slash:
            await ctx.interaction.response.defer(ephemeral=ephemeral)
        # If it wasn't a Slash, delete the initial msg to keep channels/DMs clean
        else:
            await ctx.message.delete()

        # 1. Lookup verified member record
        record = await self._find_verified_member_record(user.id, guild_id)
        
        # 2. Check if they have a linked WildApricot account
        if not record:
            verify_mention = self._get_verify_channel_mention(ctx.guild)
            msg = (
                "I don't track inactive members. If you're an active member, remember to "
                f"verify your discord account in the {verify_mention} channel, then you can try "
                "checking your hours with me again. If something is still not quite what you expect, "
                "check your account status at [ubcsailing.org](https://ubcsailing.org) or reach out to [hello@ubcsailing.org](mailto:hello@ubcsailing.org) for help with your account."
            )
            if is_slash:
                await ctx.interaction.followup.send(msg, ephemeral=ephemeral)
            else:
                if is_dm:
                    await ctx.send(msg)
                else:
                    try:
                        await user.send(f"{ctx.author.mention}, {msg}")
                        await ctx.send(f"{user.mention}, I've sent you a DM with instructions!", delete_after=10)
                    except discord.Forbidden:
                        await ctx.send(f"{user.mention}, {msg}\n*(Tip: Enable DMs from server members so I can message you privately!)*")
            return

        # 3. Check if they are active
        status = (record.get("membership_status") or "").strip().lower()
        if status != "active":
            verify_mention = self._get_verify_channel_mention(ctx.guild)
            msg = (
                "I don't track inactive members. If you're an active member, remember to "
                f"verify your discord account in the {verify_mention} channel, then you can try "
                "checking your hours with me again. If something is still not quite what you expect, "
                "check your account status at [ubcsailing.org](https://ubcsailing.org) or reach out to [hello@ubcsailing.org](mailto:hello@ubcsailing.org) for help with your account."
            )
            if is_slash:
                await ctx.interaction.followup.send(msg, ephemeral=ephemeral)
            else:
                if is_dm:
                    await ctx.send(msg)
                else:
                    try:
                        await user.send(f"{ctx.author.mention}, {msg}")
                        await ctx.send(f"{user.mention}, I've sent you a DM with instructions!", delete_after=10)
                    except discord.Forbidden:
                        await ctx.send(f"{user.mention}, {msg}\n*(Tip: Enable DMs from server members so I can message you privately!)*")
            return

        # 4. Check if they are a social member
        level = (record.get("membership_level") or "").strip().lower()
        if level == "social":
            verify_mention = self._get_verify_channel_mention(ctx.guild)
            msg = (
                "Sailing club social members' work hours aren't tracked. If you recently became "
                f"a general member, be sure to verify in the {verify_mention} channel and you can try "
                "checking your hours with me again. If something is still not quite what you expect, "
                "check your account status at [ubcsailing.org](https://ubcsailing.org) or reach out to [hello@ubcsailing.org](mailto:hello@ubcsailing.org) for help with your account."
            )
            if is_slash:
                await ctx.interaction.followup.send(msg, ephemeral=ephemeral)
            else:
                if is_dm:
                    await ctx.send(msg)
                else:
                    try:
                        await user.send(f"{ctx.author.mention}, {msg}")
                        await ctx.send(f"{user.mention}, I've sent you a DM with instructions!", delete_after=10)
                    except discord.Forbidden:
                        await ctx.send(f"{user.mention}, {msg}\n*(Tip: Enable DMs from server members so I can message you privately!)*")
            return

        # 4. Get or fetch WildApricot full name
        wa_name = await self._get_or_fetch_wa_name(record, user.id)
        if not wa_name:
            msg = "I could not retrieve your WildApricot account name. Please contact a staff member."
            if is_slash:
                await ctx.interaction.followup.send(msg, ephemeral=ephemeral)
            else:
                await ctx.send(msg)
            return

        # Check for duplicate name conflicts across active members in the registry
        if await self._has_duplicate_name_conflict(wa_name, user.id):
            msg = "I had trouble confirming your hours, email the club treasurer at [treasurer@ubcsailing.org](mailto:treasurer@ubcsailing.org)"
            if is_slash:
                await ctx.interaction.followup.send(msg, ephemeral=ephemeral)
            else:
                if is_dm:
                    await ctx.send(msg)
                else:
                    try:
                        await user.send(f"{ctx.author.mention}, {msg}")
                        await ctx.send(f"{user.mention}, I've sent you a DM regarding your inquiry.", delete_after=10)
                    except discord.Forbidden:
                        await ctx.send(f"{user.mention}, {msg}\n*(Tip: Enable DMs from server members so I can message you privately!)*")
            return

        # 5. Fetch and calculate hours from Google Sheets
        try:
            verified_hours, unverified_hours, submissions = await self._calculate_hours(wa_name)
        except Exception as e:
            log.exception(f"Failed to fetch workhours from Google Sheet: {e}")
            msg = "Sorry, I encountered an error connecting to the workhours database. Please try again later."
            if is_slash:
                await ctx.interaction.followup.send(msg, ephemeral=ephemeral)
            else:
                await ctx.send(msg)
            return

        # 6. Build a beautiful premium Embed
        season_start, season_end = _get_current_season_bounds()
        total_hours = verified_hours + unverified_hours

        embed = discord.Embed(
            title="⚓  Your Sailing Season Workhours",
            description=f"Showing workhours for member **{wa_name}**.",
            color=discord.Color.blue() if verified_hours > 0 else discord.Color.orange()
        )
        embed.set_author(name=str(user), icon_url=user.display_avatar.url)
        
        # Season bounds formatted beautifully
        season_text = f"📅  **Season:** `{season_start.strftime('%B %d, %Y')}` to `{season_end.strftime('%B %d, %Y')}`"
        embed.add_field(name="Current Season", value=season_text, inline=False)

        # Pair labels and hours values directly to avoid scan/wrap confusion
        hours_summary = (
            f"✅  **Verified Hours:** `{verified_hours:g}`\n"
            f"⏳  **Pending Review:** `{unverified_hours:g}`\n"
            f"📊  **Total Submitted:** `{total_hours:g}`"
        )
        embed.add_field(name="Season Tally", value=hours_summary, inline=False)

        # Workhours link and correction instructions moved to the very end of the message
        info_value = (
            "Submit hours here: https://ubcsailing.org/Workhour\n"
            "If this seems wrong, reach out to [treasurer@ubcsailing.org](mailto:treasurer@ubcsailing.org) with any corrections and get an official tally."
        )
        embed.add_field(name="\u200b", value=info_value, inline=False)

        # Elegant footer and thumbnail if any
        embed.set_footer(text="BlackbeardBot — UBC Sailing Club", icon_url=ctx.bot.user.display_avatar.url if ctx.bot.user else None)
        embed.timestamp = dt.datetime.now(dt.timezone.utc)

        # 7. Send the response
        if is_slash:
            await ctx.interaction.followup.send(embed=embed, ephemeral=ephemeral)
        else:
            if is_dm:
                await ctx.send(embed=embed)
            else:
                try:
                    # In channel, send the embed via DM to keep it private (as required)
                    await user.send(embed=embed)
                    await ctx.send(f"{user.mention}, I've DMed you your workhours breakdown!", delete_after=10)
                except discord.Forbidden:
                    # Fallback if DMs are blocked
                    await ctx.send(
                        f"{user.mention}, I couldn't DM you your workhours. "
                        f"Please temporarily allow direct messages from server members, or use `/workhours` slash command instead!"
                    )

async def setup(bot: commands.Bot):
    await bot.add_cog(WorkhoursCog(bot))
