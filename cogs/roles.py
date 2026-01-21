import json
from pathlib import Path
from typing import Dict, Optional, List

import discord
from discord.ext import commands

import config


def _emoji_key(e: discord.PartialEmoji) -> str:
    return str(e)


def _get_role(guild: discord.Guild, role_name: str) -> Optional[discord.Role]:
    return discord.utils.get(guild.roles, name=role_name)


class RoleAssignmentsCog(commands.Cog):
    """
    Posts multiple embed-based role panels (vertical list format everywhere)
    and toggles roles via reactions on those messages.
    Uses raw reaction events so it works without message caching.
    """

    def __init__(self, bot: commands.Bot):
        self.bot = bot

        self.roles_channel_name = getattr(config, "GET_ROLES_CHANNEL_NAME", "get-roles")
        self.state_path = Path(getattr(config, "ROLE_PANEL_STATE_FILE", "data/role_panels.json"))

        self.panel_ids: Dict[str, int] = {}                  # panel_key -> message_id
        self.message_to_map: Dict[int, Dict[str, str]] = {}  # message_id -> {emoji -> role_name}

        social_role_name = getattr(config, "ROLE_SOCIAL", "Social")

        # Panels (vertical lists everywhere: one field with newline-separated items)
        self.panels: List[Dict] = [
            {
                "key": "updates",
                "title": "Role Toggles — Updates",
                "description": "React below to toggle roles (add/remove).",
                "field_name": "Updates",
                "field_value": "🎉  Events\n👕  Merch\n🛠️  Volunteer",
                "emoji_to_role": {
                    "🎉": "Events",
                    "👕": "Merch",
                    "🛠️": "Volunteer",
                },
            },
            {
                "key": "fleets",
                "title": "Role Toggles — Fleets",
                "description": "React below to toggle roles (add/remove).",
                "field_name": "Fleets",
                "field_value": "🐌  Monohulls\n🚀  Multihulls\n✈️  Skiffs\n🌬️  Windsurfers\n🛶  Kayaks",
                "emoji_to_role": {
                    "🐌": "Monohulls",
                    "🚀": "Multihulls",
                    "✈️": "Skiffs",
                    "🌬️": "Windsurfers",
                    "🛶": "Kayaks",
                },
            },
            {
                "key": "community",
                "title": "Role Toggles — Community",
                "description": "React below to toggle roles (add/remove).",
                "field_name": "Community",
                "field_value": f"🏁  Racing\n🧜‍♀️  WNB\n🧑‍🏫  Mentor\n📚  Mentee\n👯‍♀️  {social_role_name}",
                "emoji_to_role": {
                    "🏁": "Racing",
                    "🧜‍♀️": "wnb",
                    "🧑‍🏫": "Mentor",
                    "📚": "Mentee",
                    "👯‍♀️": social_role_name,

                    # Safe variants if users manually add them
                    "🧜": "WNB",
                    "🧜‍♂️": "WNB",
                    "👯": social_role_name,
                    "👯‍♂️": social_role_name,
                },
            },
            {
                "key": "waitlists_sailing",
                "title": "Waitlists — Sailing",
                "description": "React to join waitlist roles (used for notifications when spots open).",
                "field_name": "Sailing Waitlists",
                "field_value": "1️⃣  WL - Beginner\n2️⃣  WL - Intermediate\n3️⃣  WL - A1\n4️⃣  WL - A2\n5️⃣  WL - C1\n6️⃣  WL - C2",
                "emoji_to_role": {
                    "1️⃣": "WL - Beginner",
                    "2️⃣": "WL - Intermediate",
                    "3️⃣": "WL - A1",
                    "4️⃣": "WL - A2",
                    "5️⃣": "WL - C1",
                    "6️⃣": "WL - C2",
                },
            },
            {
                "key": "waitlists_windsurf",
                "title": "Waitlists — Windsurf",
                "description": "React to join waitlist roles (used for notifications when spots open).",
                "field_name": "Windsurf Waitlists",
                "field_value": "🇦  WL - L1\n🇧  WL - L2\n🇨  WL - L2.5\n🇩  WL - L3",
                "emoji_to_role": {
                    "🇦": "WL - L1",
                    "🇧": "WL - L2",
                    "🇨": "WL - L2.5",
                    "🇩": "WL - L3",
                },
            },
            {
                "key": "waitlists_other",
                "title": "Waitlists — Other",
                "description": "React to join waitlist roles (used for notifications when spots open).",
                "field_name": "Other Waitlists",
                "field_value": "🎯  WL - Proficiency Exam\n🏓  WL - Kayak",
                "emoji_to_role": {
                    "🎯": "WL - Proficiency Exam",
                    "🏓": "WL - Kayak",
                },
            },
        ]

        self._load_state()

    # -------------------- State persistence --------------------

    def _save_state(self) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"panel_ids": self.panel_ids}
        self.state_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    def _rebuild_message_map(self) -> None:
        self.message_to_map = {}
        by_key = {p["key"]: p for p in self.panels}
        for key, msg_id in self.panel_ids.items():
            panel = by_key.get(key)
            if panel:
                self.message_to_map[int(msg_id)] = dict(panel["emoji_to_role"])

    def _load_state(self) -> None:
        if not self.state_path.exists():
            return

        try:
            raw = self.state_path.read_text(encoding="utf-8").strip()
            if not raw:
                self.panel_ids = {}
                self._save_state()
                self._rebuild_message_map()
                return

            data = json.loads(raw)
            self.panel_ids = {k: int(v) for k, v in (data.get("panel_ids") or {}).items()}

        except Exception as e:
            print(f"[ROLES] Failed to load state file: {e}")
            self.panel_ids = {}
            try:
                self._save_state()
            except Exception:
                pass

        self._rebuild_message_map()

    # -------------------- Embed builder --------------------

    def _build_embed(self, panel: Dict) -> discord.Embed:
        embed = discord.Embed(
            title=panel["title"],
            description=panel["description"],
        )
        embed.add_field(
            name=panel["field_name"],
            value=panel["field_value"] or "\u2009",
            inline=False,  # force vertical layout everywhere
        )
        embed.set_footer(text="Add/remove your reaction to toggle the role.")
        return embed

    # -------------------- Role toggling --------------------

    async def _toggle_role(self, payload: discord.RawReactionActionEvent, add: bool) -> None:
        if payload.guild_id is None:
            return

        emoji_map = self.message_to_map.get(payload.message_id)
        if not emoji_map:
            return

        if self.bot.user and payload.user_id == self.bot.user.id:
            return

        emoji = _emoji_key(payload.emoji)
        role_name = emoji_map.get(emoji)
        if not role_name:
            return

        guild = self.bot.get_guild(payload.guild_id)
        if guild is None:
            return

        role = _get_role(guild, role_name)
        if role is None:
            print(f"[ROLES] Role not found: {role_name!r} (emoji={emoji!r})")
            return

        try:
            member = guild.get_member(payload.user_id)
            if member is None:
                member = await guild.fetch_member(payload.user_id)
        except (discord.NotFound, discord.Forbidden):
            return
        except discord.HTTPException as e:
            print(f"[ROLES] fetch_member failed: {e}")
            return

        try:
            if add:
                if role not in member.roles:
                    await member.add_roles(role, reason="Self-assigned via reaction role panel")
            else:
                if role in member.roles:
                    await member.remove_roles(role, reason="Self-removed via reaction role panel")
        except discord.Forbidden:
            print("[ROLES] Missing Manage Roles or role hierarchy prevents role change.")
        except discord.HTTPException as e:
            print(f"[ROLES] Role update failed: {e}")

    @commands.Cog.listener()
    async def on_raw_reaction_add(self, payload: discord.RawReactionActionEvent) -> None:
        await self._toggle_role(payload, add=True)

    @commands.Cog.listener()
    async def on_raw_reaction_remove(self, payload: discord.RawReactionActionEvent) -> None:
        await self._toggle_role(payload, add=False)

    # -------------------- Posting panels + adding initial reacts --------------------

    @commands.command(name="post_role_panels")
    @commands.has_permissions(administrator=True)
    async def post_role_panels(self, ctx: commands.Context) -> None:
        if not isinstance(ctx.channel, discord.TextChannel):
            return

        if ctx.channel.name != self.roles_channel_name:
            await ctx.send(f"Run this in #{self.roles_channel_name}.", delete_after=10)
            return

        new_panel_ids: Dict[str, int] = {}

        for panel in self.panels:
            embed = self._build_embed(panel)
            msg = await ctx.channel.send(embed=embed)
            new_panel_ids[panel["key"]] = msg.id

            # Add initial reactions (skip variant-only emojis)
            for emoji in panel["emoji_to_role"].keys():
                if emoji in ("🧜", "🧜‍♂️", "👯", "👯‍♂️"):
                    continue
                try:
                    await msg.add_reaction(emoji)
                except discord.HTTPException as e:
                    print(f"[ROLES] Failed to add reaction {emoji!r} on {msg.id}: {e}")

        self.panel_ids = new_panel_ids
        self._save_state()
        self._rebuild_message_map()

        await ctx.send("Posted role panels (vertical embeds) and added reactions.", delete_after=10)

        # -------------------- Syncing panel content (no reaction changes) --------------------

    @commands.command(name="sync_role_panels")
    @commands.has_permissions(administrator=True)
    async def sync_role_panels(self, ctx: commands.Context) -> None:
        if not isinstance(ctx.channel, discord.TextChannel):
            return

        if ctx.channel.name != self.roles_channel_name:
            await ctx.send(f"Run this in #{self.roles_channel_name}.", delete_after=10)
            return

        by_key = {p["key"]: p for p in self.panels}
        missing: List[str] = []

        for key, panel in by_key.items():
            msg_id = self.panel_ids.get(key)
            if not msg_id:
                missing.append(key)
                continue

            try:
                msg = await ctx.channel.fetch_message(int(msg_id))
            except discord.NotFound:
                missing.append(key)
                continue
            except discord.Forbidden:
                await ctx.send("I need Read Message History in this channel to sync panels.", delete_after=10)
                return
            except discord.HTTPException as e:
                print(f"[ROLES] fetch_message failed for {key} ({msg_id}): {e}")
                continue

            try:
                await msg.edit(embed=self._build_embed(panel))
            except discord.HTTPException as e:
                print(f"[ROLES] Failed to edit panel {key} ({msg.id}): {e}")

        if missing:
            await ctx.send(
                "Synced existing panels. Missing/deleted panel messages for: "
                + ", ".join(missing)
                + " (re-run !post_role_panels if you want to recreate them).",
                delete_after=15,
            )
        else:
            await ctx.send("Synced role panels (updated embeds; reactions unchanged).", delete_after=10)

async def setup(bot: commands.Bot):
    await bot.add_cog(RoleAssignmentsCog(bot))

