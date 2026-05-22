import asyncio
import base64
import datetime as dt
import json
import re
from pathlib import Path
from typing import Optional, Dict, Any, Tuple, List

import aiohttp
import discord
from discord.ext import commands, tasks

import config


# -------------------- constants / season logic --------------------

UTC = dt.timezone.utc

# Membership season: April 1 -> March 31
# Re-verify required by April 7 (enforcement begins at 00:05 UTC on April 7)
SEASON_START_MONTH = 4
SEASON_START_DAY = 1
REVERIFY_DEADLINE_MONTH = 4
REVERIFY_DEADLINE_DAY = 14

DM_ANNOUNCE_TIME_UTC = dt.time(hour=16, minute=0, tzinfo=UTC)   # April 1, 16:00 UTC
ENFORCE_TIME_UTC = dt.time(hour=7, minute=5, tzinfo=UTC)        # April 7, 00:05 UTC

REVERIFY_DM_TEXT = (
    "Ahoy! A new membership season has begun (April 1–March 31).\n\n"
    "• **If you have already renewed** (or renew on the club website at https://ubcsailing.org/ before April 14), you are all set! The bot (me!) will automatically check your status on April 14, renew your verification, and keep your existing roles intact (which you can manage in {roles_channel})—no manual action is needed.\n"
    "• **If you renew after April 14**, your opt-in roles will be temporarily removed. To get them back, simply renew on the club website (https://ubcsailing.org/) and then click the **Verify** button in the {verify_channel} channel at any time to automatically restore all of your previous roles and access!"
)


def _utcnow() -> dt.datetime:
    return dt.datetime.now(tz=UTC)


def _season_start_utc(year: int) -> dt.datetime:
    return dt.datetime(year, SEASON_START_MONTH, SEASON_START_DAY, 0, 0, 0, tzinfo=UTC)


def _reverify_deadline_utc(year: int) -> dt.datetime:
    return dt.datetime(year, REVERIFY_DEADLINE_MONTH, REVERIFY_DEADLINE_DAY, 0, 0, 0, tzinfo=UTC)


def _today_utc() -> dt.date:
    return _utcnow().date()


def _parse_utc_iso(s: Optional[str]) -> Optional[dt.datetime]:
    if not s or not isinstance(s, str):
        return None
    try:
        # Stored as "...Z"
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        d = dt.datetime.fromisoformat(s)
        if d.tzinfo is None:
            d = d.replace(tzinfo=UTC)
        return d.astimezone(UTC)
    except Exception:
        return None


# -------------------- helpers --------------------

def _norm_name(s: str) -> str:
    s = (s or "").strip().lower()
    s = re.sub(r"[^a-z0-9\s]+", " ", s)   # drop punctuation
    s = re.sub(r"\s+", " ", s).strip()
    return s


def _get_role(guild: discord.Guild, role_name: str) -> Optional[discord.Role]:
    return discord.utils.get(guild.roles, name=role_name)


def _bot_member(guild: discord.Guild, bot: commands.Bot) -> Optional[discord.Member]:
    if guild is None:
        return None
    me = getattr(guild, "me", None)
    if isinstance(me, discord.Member):
        return me
    if bot.user:
        m = guild.get_member(bot.user.id)
        if isinstance(m, discord.Member):
            return m
    return None


def _can_manage_role(bot_m: Optional[discord.Member], role: discord.Role) -> bool:
    # Must be below bot's top role and not managed
    if bot_m is None:
        return False
    if role.managed:
        return False
    return role < bot_m.top_role


async def _apply_discord_updates(
    interaction: discord.Interaction,
    wa_full: str,
    membership_level: Optional[str],
    registry: "VerifiedRegistry",
    wa_contact_id: int,
) -> List[discord.Role]:
    """
    Applies Discord updates for successfully verified ACTIVE members:
      - nickname set to WA name
      - roles set based on membership level
      - removes "past member" if present
      - restores old roles if previously demoted
    """
    guild = interaction.guild
    if guild is None:
        return []

    member = interaction.user
    if not isinstance(member, discord.Member):
        member = guild.get_member(interaction.user.id)
        if member is None:
            return []

    # --- nickname (Discord limit is 32 chars) ---
    new_nick = (wa_full or "").strip()[:32] or None
    try:
        await member.edit(nick=new_nick, reason="Verified via WildApricot")
    except (discord.Forbidden, discord.HTTPException):
        pass

    # --- roles ---
    role_social = _get_role(guild, getattr(config, "ROLE_SOCIAL", "social"))
    role_swabbie = _get_role(guild, getattr(config, "ROLE_SWABBIE", "swabbie"))
    role_past_member = _get_role(guild, getattr(config, "ROLE_PAST_MEMBER", "past member"))

    level = (membership_level or "").strip()

    bot_m = _bot_member(guild, interaction.client)

    add_roles: List[discord.Role] = []
    remove_roles: List[discord.Role] = []

    # Always remove "past member" on successful verification if we have permission
    if role_past_member and role_past_member in member.roles:
        if bot_m is None or _can_manage_role(bot_m, role_past_member):
            remove_roles.append(role_past_member)

    if level == "Social":
        if role_social and (bot_m is None or _can_manage_role(bot_m, role_social)):
            add_roles.append(role_social)
        if role_swabbie and role_swabbie in member.roles and (bot_m is None or _can_manage_role(bot_m, role_swabbie)):
            remove_roles.append(role_swabbie)
    elif level in ("General Member", "UBC Student"):
        if role_swabbie and (bot_m is None or _can_manage_role(bot_m, role_swabbie)):
            add_roles.append(role_swabbie)
        if role_social and role_social in member.roles and (bot_m is None or _can_manage_role(bot_m, role_social)):
            remove_roles.append(role_social)

    # --- restore old roles ---
    wa_records = await registry.list_wa_records(guild.id)
    rec = wa_records.get(str(wa_contact_id))
    if rec and rec.get("restorable_role_ids"):
        for rid in rec.get("restorable_role_ids", []):
            role = guild.get_role(rid)
            if role and role not in add_roles:
                if bot_m is None or _can_manage_role(bot_m, role):
                    add_roles.append(role)
        
        # Clear the old saved roles so we don't re-apply them needlessly later
        await registry.update_wa_record(guild.id, wa_contact_id, {"restorable_role_ids": []})

    # Only add roles the member doesn't already have
    actual_add_roles = [r for r in add_roles if r not in member.roles]

    try:
        if remove_roles:
            await member.remove_roles(*remove_roles, reason="Verified via WildApricot")
        if actual_add_roles:
            await member.add_roles(*actual_add_roles, reason="Verified via WildApricot")
    except (discord.Forbidden, discord.HTTPException):
        pass

    return add_roles


async def _demote_to_past_member_and_social(
    bot: commands.Bot,
    member: discord.Member,
    role_past_member: Optional[discord.Role],
    role_social: Optional[discord.Role],
) -> List[discord.Role]:
    """
    Remove all removable roles, then ensure member has:
      - past member
      - social
    Returns a list of roles that were removed.
    """
    guild = member.guild
    bot_m = _bot_member(guild, bot)

    keep = set()
    if role_past_member:
        keep.add(role_past_member)
    if role_social:
        keep.add(role_social)

    # Remove everything we can, except @everyone and keep-roles
    to_remove: List[discord.Role] = []
    for r in member.roles:
        if r == guild.default_role:
            continue
        if r in keep:
            continue
        if bot_m and _can_manage_role(bot_m, r):
            to_remove.append(r)

    try:
        if to_remove:
            await member.remove_roles(*to_remove, reason="Season re-verification enforcement")
    except (discord.Forbidden, discord.HTTPException):
        pass

    # Add required roles (if manageable)
    to_add: List[discord.Role] = []
    if role_past_member and (role_past_member not in member.roles) and (bot_m is None or _can_manage_role(bot_m, role_past_member)):
        to_add.append(role_past_member)
    if role_social and (role_social not in member.roles) and (bot_m is None or _can_manage_role(bot_m, role_social)):
        to_add.append(role_social)

    try:
        if to_add:
            await member.add_roles(*to_add, reason="Season re-verification enforcement")
    except (discord.Forbidden, discord.HTTPException):
        pass

    return to_remove


# -------------------- verified registry (WA ID <-> Discord user) --------------------

class VerifiedRegistry:
    """
    Stores verified WildApricot contact IDs mapped to Discord users, per guild.

    Enforces uniqueness:
      - A WA contact ID can be linked to only one Discord user *currently in the guild*.
      - If the linked Discord user is no longer in the guild, another user may claim it.
      - If the same user re-verifies, it updates the record.

    Also stores per-guild per-season state:
      - dm_sent_at_utc
      - enforced_at_utc
    """

    def __init__(self, path: Path):
        self.path = path
        self._lock = asyncio.Lock()

    def _load(self) -> Dict[str, Any]:
        if not self.path.exists():
            return {"guilds": {}}
        try:
            raw = self.path.read_text(encoding="utf-8").strip()
            if not raw:
                return {"guilds": {}}
            data = json.loads(raw)
            if not isinstance(data, dict):
                return {"guilds": {}}
            data.setdefault("guilds", {})
            return data
        except Exception as e:
            print(f"[VERIFY_REGISTRY] Failed to load registry JSON: {e}")
            return {"guilds": {}}

    def _atomic_write(self, data: Dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")
        tmp.replace(self.path)

    async def _is_member_present(self, guild: discord.Guild, user_id: int) -> bool:
        # Prefer cache
        if guild.get_member(user_id) is not None:
            return True

        # Fall back to API fetch (does not require privileged intents)
        try:
            await guild.fetch_member(user_id)
            return True
        except discord.NotFound:
            return False
        except discord.Forbidden:
            # Conservative: if we cannot verify absence, do not allow reassignment.
            print("[VERIFY_REGISTRY] Forbidden to fetch_member; treating as present for safety.")
            return True
        except discord.HTTPException:
            print("[VERIFY_REGISTRY] HTTPException on fetch_member; treating as present for safety.")
            return True

    async def claim(
        self,
        guild: discord.Guild,
        wa_contact_id: int,
        discord_user: discord.abc.User,
        wa_full_name: str,
        membership_level: Optional[str],
        membership_status: Optional[str],
    ) -> Tuple[bool, str]:
        """
        Attempt to link wa_contact_id to discord_user.id in this guild.
        Returns (ok, message). If ok is False, message is a user-facing error.
        """
        if guild is None:
            return False, "Verification must be used inside the server."

        gid = str(guild.id)
        wa_key = str(wa_contact_id)
        duid = str(discord_user.id)
        now = _utcnow().isoformat(timespec="seconds").replace("+00:00", "Z")

        async with self._lock:
            data = self._load()
            g = data["guilds"].setdefault(gid, {})
            wa_map = g.setdefault("wa_id_map", {})              # wa_id -> record
            user_map = g.setdefault("discord_user_map", {})     # discord_user_id -> wa_id

            # If this Discord user previously claimed a different WA ID, clean up the old link
            prev_wa = user_map.get(duid)
            if prev_wa and prev_wa != wa_key:
                old_rec = wa_map.get(prev_wa)
                if isinstance(old_rec, dict) and str(old_rec.get("discord_user_id")) == duid:
                    wa_map.pop(prev_wa, None)

            existing = wa_map.get(wa_key)

            # No existing mapping: claim it
            if not existing:
                wa_map[wa_key] = {
                    "wa_contact_id": wa_contact_id,
                    "discord_user_id": discord_user.id,
                    "discord_tag": str(discord_user),
                    "discord_name": getattr(discord_user, "name", None),
                    "discord_global_name": getattr(discord_user, "global_name", None),
                    "wa_full_name": wa_full_name,
                    "membership_level": membership_level,
                    "membership_status": membership_status,
                    "first_verified_at_utc": now,
                    "last_verified_at_utc": now,
                }
                user_map[duid] = wa_key
                self._atomic_write(data)
                print(f"[VERIFY_REGISTRY] Linked wa_id={wa_key} -> discord_user_id={duid} (new)")
                return True, "OK"

            # Existing mapping: allow same account to re-verify (update info)
            existing_duid = int(existing.get("discord_user_id", 0) or 0)
            if existing_duid == discord_user.id:
                existing.update({
                    "discord_tag": str(discord_user),
                    "discord_name": getattr(discord_user, "name", None),
                    "discord_global_name": getattr(discord_user, "global_name", None),
                    "wa_full_name": wa_full_name,
                    "membership_level": membership_level,
                    "membership_status": membership_status,
                    "last_verified_at_utc": now,
                })
                wa_map[wa_key] = existing
                user_map[duid] = wa_key
                self._atomic_write(data)
                print(f"[VERIFY_REGISTRY] Linked wa_id={wa_key} -> discord_user_id={duid} (re-verify)")
                return True, "OK"

            # Existing mapping points to a different Discord account:
            # Only block if that account is still in the guild.
            present = await self._is_member_present(guild, existing_duid)
            if present:
                print(
                    f"[VERIFY_REGISTRY] Reject: wa_id={wa_key} already linked to discord_user_id={existing_duid} "
                    f"(still in guild). Attempt by discord_user_id={duid}"
                )
                return (
                    False,
                    "That Member ID is already linked to another Discord account that is still in this server. "
                    "If you believe this is an error, please contact a staff member."
                )

            # Previous account not on server: allow takeover
            wa_map[wa_key] = {
                "wa_contact_id": wa_contact_id,
                "discord_user_id": discord_user.id,
                "discord_tag": str(discord_user),
                "discord_name": getattr(discord_user, "name", None),
                "discord_global_name": getattr(discord_user, "global_name", None),
                "wa_full_name": wa_full_name,
                "membership_level": membership_level,
                "membership_status": membership_status,
                "first_verified_at_utc": existing.get("first_verified_at_utc", now),
                "last_verified_at_utc": now,
                "reassigned_from_discord_user_id": existing_duid,
                "reassigned_at_utc": now,
                "restorable_role_ids": existing.get("restorable_role_ids", []),
            }
            user_map[duid] = wa_key
            self._atomic_write(data)
            print(
                f"[VERIFY_REGISTRY] Linked wa_id={wa_key} -> discord_user_id={duid} (reassigned; "
                f"previous discord_user_id={existing_duid} not in guild)"
            )
            return True, "OK"

    async def list_wa_records(self, guild_id: int) -> Dict[str, Dict[str, Any]]:
        gid = str(guild_id)
        async with self._lock:
            data = self._load()
            g = data.get("guilds", {}).get(gid, {})
            wa_map = g.get("wa_id_map", {}) or {}
            # return a shallow copy to avoid accidental mutation without lock
            return {k: (v.copy() if isinstance(v, dict) else {}) for k, v in wa_map.items()}

    async def update_wa_record(self, guild_id: int, wa_contact_id: int, updates: Dict[str, Any]) -> None:
        gid = str(guild_id)
        wa_key = str(wa_contact_id)
        async with self._lock:
            data = self._load()
            g = data["guilds"].setdefault(gid, {})
            wa_map = g.setdefault("wa_id_map", {})
            rec = wa_map.get(wa_key)
            if isinstance(rec, dict):
                rec.update(updates)
                wa_map[wa_key] = rec
                self._atomic_write(data)

    async def _get_season_state(self, data: Dict[str, Any], gid: str) -> Dict[str, Any]:
        g = data["guilds"].setdefault(gid, {})
        return g.setdefault("season_state", {})

    async def was_dm_sent(self, guild_id: int, season_year: int) -> bool:
        gid = str(guild_id)
        async with self._lock:
            data = self._load()
            season_state = data.get("guilds", {}).get(gid, {}).get("season_state", {}) or {}
            rec = season_state.get(str(season_year), {}) or {}
            return bool(rec.get("dm_sent_at_utc"))

    async def mark_dm_sent(self, guild_id: int, season_year: int) -> None:
        gid = str(guild_id)
        now = _utcnow().isoformat(timespec="seconds").replace("+00:00", "Z")
        async with self._lock:
            data = self._load()
            season_state = data["guilds"].setdefault(gid, {}).setdefault("season_state", {})
            rec = season_state.setdefault(str(season_year), {})
            rec["dm_sent_at_utc"] = now
            season_state[str(season_year)] = rec
            self._atomic_write(data)

    async def was_enforced(self, guild_id: int, season_year: int) -> bool:
        gid = str(guild_id)
        async with self._lock:
            data = self._load()
            season_state = data.get("guilds", {}).get(gid, {}).get("season_state", {}) or {}
            rec = season_state.get(str(season_year), {}) or {}
            return bool(rec.get("enforced_at_utc"))

    async def mark_enforced(self, guild_id: int, season_year: int) -> None:
        gid = str(guild_id)
        now = _utcnow().isoformat(timespec="seconds").replace("+00:00", "Z")
        async with self._lock:
            data = self._load()
            season_state = data["guilds"].setdefault(gid, {}).setdefault("season_state", {})
            rec = season_state.setdefault(str(season_year), {})
            rec["enforced_at_utc"] = now
            season_state[str(season_year)] = rec
            self._atomic_write(data)


# -------------------- WildApricot client --------------------

class WildApricotClient:
    AUTH_URL = "https://oauth.wildapricot.org/auth/token"
    API_BASE = "https://api.wildapricot.org"

    def __init__(self, api_key: str, account_id: int, api_version: str = "v2.1"):
        if not api_key:
            raise ValueError("WA_API_KEY is empty")
        if not account_id:
            raise ValueError("WA_ACCOUNT_ID is empty/0")

        self.api_key = api_key
        self.account_id = account_id
        self.api_version = api_version

        self._token: Optional[str] = None
        self._token_expiry_utc: Optional[dt.datetime] = None
        self._session: Optional[aiohttp.ClientSession] = None

    async def start(self) -> None:
        if self._session is None:
            self._session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=20))

    async def close(self) -> None:
        if self._session is not None:
            await self._session.close()
            self._session = None

    async def _ensure_token(self) -> str:
        if self._token and self._token_expiry_utc:
            if dt.datetime.utcnow() < (self._token_expiry_utc - dt.timedelta(seconds=30)):
                return self._token

        if self._session is None:
            raise RuntimeError("WildApricotClient not started (session is None)")

        basic = base64.b64encode(f"APIKEY:{self.api_key}".encode("utf-8")).decode("ascii")
        headers = {"Authorization": f"Basic {basic}"}
        data = {"grant_type": "client_credentials", "scope": "auto"}

        async with self._session.post(self.AUTH_URL, data=data, headers=headers) as resp:
            text = await resp.text()
            if resp.status != 200:
                raise RuntimeError(f"WA token request failed: HTTP {resp.status} body={text[:300]}")
            payload = await resp.json()

        token = payload.get("access_token")
        if not token:
            raise RuntimeError(f"WA token response missing access_token: {payload}")

        expires_in = int(payload.get("expires_in", 3600))
        self._token = token
        self._token_expiry_utc = dt.datetime.utcnow() + dt.timedelta(seconds=expires_in)
        return token

    async def get_contact(self, contact_id: int) -> Dict[str, Any]:
        if self._session is None:
            raise RuntimeError("WildApricotClient not started (session is None)")

        token = await self._ensure_token()
        url = f"{self.API_BASE}/{self.api_version}/accounts/{self.account_id}/contacts/{contact_id}"
        headers = {"Authorization": f"Bearer {token}"}

        async with self._session.get(url, headers=headers) as resp:
            if resp.status == 404:
                return {}
            text = await resp.text()
            if resp.status != 200:
                raise RuntimeError(f"WA contact lookup failed: HTTP {resp.status} body={text[:300]}")
            return await resp.json()


# -------------------- UI: modal + view --------------------

class VerifyModal(discord.ui.Modal, title="Member Verification"):
    name = discord.ui.TextInput(
        label="Name",
        placeholder="e.g., Hugo Ricart",
        required=True,
        max_length=100,
    )
    member_id = discord.ui.TextInput(
        label="Member ID",
        placeholder="e.g., 12345",
        required=True,
        max_length=32,
    )

    def __init__(self, wa: WildApricotClient, registry: VerifiedRegistry):
        super().__init__()
        self.wa = wa
        self.registry = registry

    async def on_submit(self, interaction: discord.Interaction):
        # Critical: acknowledge immediately so Discord doesn't time out the interaction
        await interaction.response.defer(ephemeral=True, thinking=True)

        name_val = (self.name.value or "").strip()
        member_id_val = (self.member_id.value or "").strip()

        # Validate ID format
        try:
            contact_id = int(member_id_val)
        except ValueError:
            await interaction.followup.send(
                "Member ID must be a number (WildApricot contact ID).",
                ephemeral=True,
            )
            return

        # Lookup contact
        try:
            contact = await self.wa.get_contact(contact_id)
        except Exception as e:
            print(f"[VERIFY][WA_ERROR] user={interaction.user} id={contact_id} err={e}")
            await interaction.followup.send(
                "Verification service error. Please try again later.",
                ephemeral=True,
            )
            return

        # Standard failure message
        failure_msg = (
            f"Failed to verify {name_val} with ID {member_id_val}. "
            f"Please ensure information is correct and try again."
        )

        if not contact:
            print(f"[VERIFY] user={interaction.user} id={contact_id} NOT_FOUND input_name={name_val!r}")
            await interaction.followup.send(failure_msg, ephemeral=True)
            return

        wa_first = (contact.get("FirstName") or "").strip()
        wa_last = (contact.get("LastName") or "").strip()
        wa_full = f"{wa_first} {wa_last}".strip()

        # Membership status
        status = contact.get("Status")
        if not status:
            for fv in contact.get("FieldValues", []) or []:
                if (fv.get("FieldName") or "").strip().lower() == "membership status":
                    status = fv.get("Value")
                    break
        status_norm = (status or "").strip().lower()

        # Membership level
        membership_level = None
        ml = contact.get("MembershipLevel")
        if isinstance(ml, dict):
            membership_level = ml.get("Name")
        if not membership_level:
            for fv in contact.get("FieldValues", []) or []:
                if (fv.get("FieldName") or "").strip().lower() == "membership level":
                    membership_level = fv.get("Value")
                    break

        match = _norm_name(name_val) == _norm_name(wa_full)

        print(
            f"[VERIFY] user={interaction.user} ({interaction.user.id}) "
            f"input_name={name_val!r} input_member_id={contact_id} "
            f"wa_name={wa_full!r} wa_status={status!r} wa_level={membership_level!r} match={match}"
        )

        # Must match AND be Active
        if (not match) or (status_norm != "active"):
            # Note: do NOT remove social role here; we only fail the verification.
            await interaction.followup.send(failure_msg, ephemeral=True)
            return

        # Enforce uniqueness: only block if prior owner is still on the server
        guild = interaction.guild
        if guild is None:
            await interaction.followup.send("Verification must be used inside the server.", ephemeral=True)
            return

        ok, msg = await self.registry.claim(
            guild=guild,
            wa_contact_id=contact_id,
            discord_user=interaction.user,
            wa_full_name=wa_full,
            membership_level=membership_level,
            membership_status=status,
        )
        if not ok:
            await interaction.followup.send(msg, ephemeral=True)
            return

        # Success: nickname + roles
        added_roles = await _apply_discord_updates(
            interaction, 
            wa_full=wa_full, 
            membership_level=membership_level,
            registry=self.registry,
            wa_contact_id=contact_id
        )

        added_roles_text = ", ".join(f"`{r.name}`" for r in added_roles) if added_roles else "None"
        
        channel_name = getattr(config, "GET_ROLES_CHANNEL_NAME", "get-roles")
        roles_ch = discord.utils.get(guild.channels, name=channel_name)
        ch_mention = f"<#{roles_ch.id}>" if roles_ch else f"#{channel_name}"

        await interaction.followup.send(
            f"Verified.\n"
            f"Membership status: `{status or 'Unknown'}`\n"
            f"Membership level: `{membership_level or 'Unknown'}`\n"
            f"**Roles Assigned:** {added_roles_text}\n\n"
            f"To manage your roles, head over to the {ch_mention} channel.",
            ephemeral=True,
        )


class VerifyView(discord.ui.View):
    def __init__(self, wa: WildApricotClient, registry: VerifiedRegistry):
        super().__init__(timeout=None)  # persistent view
        self.wa = wa
        self.registry = registry

    @discord.ui.button(label="Verify", style=discord.ButtonStyle.primary, custom_id="verify:open_modal")
    async def verify_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if getattr(interaction.channel, "name", None) != config.VERIFY_CHANNEL_NAME:
            await interaction.response.send_message(
                f"Please use this in #{config.VERIFY_CHANNEL_NAME}.",
                ephemeral=True,
            )
            return

        await interaction.response.send_modal(VerifyModal(self.wa, self.registry))


# -------------------- Cog: auto-post + season enforcement --------------------

class VerifyCog(commands.Cog):
    def __init__(self, bot: commands.Bot, wa: WildApricotClient):
        self.bot = bot
        self.wa = wa

        # Message state file (existing behavior)
        self.state_path = Path(getattr(config, "VERIFY_MESSAGE_STATE_FILE", "data/verify_message.json"))

        # Verified registry file (new behavior)
        verified_path = Path(getattr(config, "VERIFIED_MEMBERS_FILE", "data/verified_members.json"))
        self.registry = VerifiedRegistry(verified_path)

        self.view = VerifyView(wa, self.registry)

        self._lock = asyncio.Lock()
        self._bootstrapped = False
        self._season_tasks_started = False

    def _build_embed(self) -> discord.Embed:
        return discord.Embed(
            title="Member Verification",
            description=(
                "Click **Verify** to submit your **full name** (as on club files) and your **Member ID**.\n\n"
            ),
        )

    def _load_state(self) -> Dict[str, Any]:
        if not self.state_path.exists():
            return {}
        try:
            raw = self.state_path.read_text(encoding="utf-8").strip()
            if not raw:
                return {}
            return json.loads(raw)
        except Exception as e:
            print(f"[VERIFY] Failed to load state: {e}")
            return {}

    def _save_state(self, guild_id: int, channel_id: int, message_id: int) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"guild_id": guild_id, "channel_id": channel_id, "message_id": message_id}
        self.state_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    async def _find_target_channel(self) -> Optional[discord.TextChannel]:
        # Prefer saved guild/channel
        state = self._load_state()
        gid = state.get("guild_id")
        cid = state.get("channel_id")

        if isinstance(gid, int) and isinstance(cid, int):
            g = self.bot.get_guild(gid)
            if g:
                ch = g.get_channel(cid)
                if isinstance(ch, discord.TextChannel):
                    return ch

        # Fallback: search by name across guilds
        for g in self.bot.guilds:
            ch = discord.utils.get(g.text_channels, name=config.VERIFY_CHANNEL_NAME)
            if isinstance(ch, discord.TextChannel):
                return ch

        return None

    async def ensure_verify_message(self) -> None:
        async with self._lock:
            if self._bootstrapped:
                return
            self._bootstrapped = True

            ch = await self._find_target_channel()
            if ch is None:
                print(f"[VERIFY] Could not find channel named #{config.VERIFY_CHANNEL_NAME}")
                return

            state = self._load_state()
            mid = state.get("message_id")

            # If prior message exists, update it and keep using it
            if isinstance(mid, int):
                try:
                    msg = await ch.fetch_message(mid)
                    await msg.edit(embed=self._build_embed(), view=self.view)
                    self._save_state(ch.guild.id, ch.id, msg.id)
                    print(f"[VERIFY] Using existing verify message_id={msg.id} in #{ch.name}")
                    return
                except discord.NotFound:
                    print("[VERIFY] Previous verify message not found (deleted). Creating a new one.")
                except Exception as e:
                    print(f"[VERIFY] Failed to fetch/edit previous verify message: {e}")

            # Otherwise create a new verify message
            try:
                msg = await ch.send(embed=self._build_embed(), view=self.view)
                self._save_state(ch.guild.id, ch.id, msg.id)
                print(f"[VERIFY] Created new verify message_id={msg.id} in #{ch.name}")
            except Exception as e:
                print(f"[VERIFY] Failed to create verify message in #{ch.name}: {e}")

    async def _dm_all_members_for_season(self, guild: discord.Guild, season_year: int) -> None:
        # Avoid duplicates
        if await self.registry.was_dm_sent(guild.id, season_year):
            return

        sent = 0
        failed = 0

        # Resolve channel links
        verify_ch = discord.utils.get(guild.text_channels, name=config.VERIFY_CHANNEL_NAME)
        verify_mention = f"<#{verify_ch.id}> (https://discord.com/channels/{guild.id}/{verify_ch.id})" if verify_ch else f"#{config.VERIFY_CHANNEL_NAME}"

        roles_ch_name = getattr(config, "GET_ROLES_CHANNEL_NAME", "get-roles")
        roles_ch = discord.utils.get(guild.text_channels, name=roles_ch_name)
        roles_mention = f"<#{roles_ch.id}> (https://discord.com/channels/{guild.id}/{roles_ch.id})" if roles_ch else f"#{roles_ch_name}"

        dm_text = REVERIFY_DM_TEXT.format(
            verify_channel=verify_mention,
            roles_channel=roles_mention
        )

        for m in guild.members:
            if m.bot:
                continue
            try:
                await m.send(dm_text)
                sent += 1
            except discord.Forbidden:
                failed += 1
            except discord.HTTPException:
                failed += 1

            # Gentle pacing to reduce rate-limit pressure
            await asyncio.sleep(1.0)

        await self.registry.mark_dm_sent(guild.id, season_year)
        print(f"[SEASON] DM sent for season_year={season_year} in guild={guild.id} (sent={sent}, failed={failed})")

    async def _enforce_reverify_for_season(self, guild: discord.Guild, season_year: int) -> None:
        # Avoid duplicates
        if await self.registry.was_enforced(guild.id, season_year):
            return

        season_start = _season_start_utc(season_year)

        role_social = _get_role(guild, getattr(config, "ROLE_SOCIAL", "social"))
        role_past_member = _get_role(guild, getattr(config, "ROLE_PAST_MEMBER", "past member"))

        wa_records = await self.registry.list_wa_records(guild.id)

        demoted = 0
        skipped_absent = 0
        already_ok = 0

        for wa_id_str, rec in wa_records.items():
            if not isinstance(rec, dict):
                continue

            duid = rec.get("discord_user_id")
            if not isinstance(duid, int):
                continue

            # Only block/demote if the linked Discord user is still in the server
            member = guild.get_member(duid)
            if member is None:
                try:
                    member = await guild.fetch_member(duid)
                except discord.NotFound:
                    skipped_absent += 1
                    continue
                except (discord.Forbidden, discord.HTTPException):
                    # Conservative: if we cannot confirm, skip demotion for this record
                    skipped_absent += 1
                    continue

            last_v = _parse_utc_iso(rec.get("last_verified_at_utc"))
            if last_v and last_v >= season_start:
                already_ok += 1
                continue

            # If they haven't re-verified on Discord yet, check WildApricot to see if they are already active for the new season
            contact_id = rec.get("wa_contact_id")
            is_active_on_wa = False
            status = None
            membership_level = None

            if isinstance(contact_id, int):
                try:
                    contact = await self.wa.get_contact(contact_id)
                    if contact:
                        # Extract membership status
                        status = contact.get("Status")
                        if not status:
                            for fv in contact.get("FieldValues", []) or []:
                                if (fv.get("FieldName") or "").strip().lower() == "membership status":
                                    status = fv.get("Value")
                                    break
                        status_norm = (status or "").strip().lower()

                        if status_norm == "active":
                            is_active_on_wa = True
                            # Also pull updated membership level if any
                            ml = contact.get("MembershipLevel")
                            if isinstance(ml, dict):
                                membership_level = ml.get("Name")
                            if not membership_level:
                                for fv in contact.get("FieldValues", []) or []:
                                    if (fv.get("FieldName") or "").strip().lower() == "membership level":
                                        membership_level = fv.get("Value")
                                        break
                except Exception as e:
                    print(f"[SEASON][WA_ERROR] Failed to check status for contact_id={contact_id} during enforcement: {e}")

            if is_active_on_wa:
                # User is active on WA! Automatically renew their verification for this season
                now_str = _utcnow().isoformat(timespec="seconds").replace("+00:00", "Z")
                await self.registry.update_wa_record(
                    guild_id=guild.id,
                    wa_contact_id=contact_id,
                    updates={
                        "last_verified_at_utc": now_str,
                        "membership_level": membership_level,
                        "membership_status": status,
                    }
                )
                already_ok += 1
                print(f"[SEASON] Auto-renewed active member wa_id={contact_id} (discord={duid}) without demotion")
                continue

            # Not re-verified this season: demote
            removed_roles = await _demote_to_past_member_and_social(
                bot=self.bot,
                member=member,
                role_past_member=role_past_member,
                role_social=role_social,
            )
            demoted += 1

            # Record demotion metadata (optional but useful)
            demotion_updates = {
                "demoted_for_season_year": season_year,
                "demoted_at_utc": _utcnow().isoformat(timespec="seconds").replace("+00:00", "Z"),
            }
            if removed_roles:
                demotion_updates["restorable_role_ids"] = [r.id for r in removed_roles]

            await self.registry.update_wa_record(
                guild_id=guild.id,
                wa_contact_id=int(rec.get("wa_contact_id", 0) or 0),
                updates=demotion_updates,
            )

            await asyncio.sleep(0.5)

        await self.registry.mark_enforced(guild.id, season_year)
        print(
            f"[SEASON] Enforced for season_year={season_year} in guild={guild.id} "
            f"(demoted={demoted}, already_ok={already_ok}, skipped_absent={skipped_absent})"
        )

    async def _season_catchup(self) -> None:
        """
        If the bot was offline at the scheduled time:
          - Between Apr 1 and Apr 6: send DM if not sent
          - On/after Apr 7: enforce if not enforced
        """
        now = _utcnow()
        year = now.year

        season_start = _season_start_utc(year)
        deadline = _reverify_deadline_utc(year)

        dm_announce_dt = dt.datetime.combine(season_start.date(), DM_ANNOUNCE_TIME_UTC, tzinfo=UTC)
        enforce_dt = dt.datetime.combine(deadline.date(), ENFORCE_TIME_UTC, tzinfo=UTC)

        for g in self.bot.guilds:
            # DM catchup window
            if dm_announce_dt <= now < deadline:
                if not await self.registry.was_dm_sent(g.id, year):
                    await self._dm_all_members_for_season(g, year)

            # Enforcement catchup
            if now >= enforce_dt:
                if not await self.registry.was_enforced(g.id, year):
                    await self._enforce_reverify_for_season(g, year)

    @tasks.loop(time=DM_ANNOUNCE_TIME_UTC)
    async def season_dm_loop(self) -> None:
        # Trigger only on April 1 (UTC)
        today = _today_utc()
        if today.month != SEASON_START_MONTH or today.day != SEASON_START_DAY:
            return

        season_year = today.year
        for g in self.bot.guilds:
            await self._dm_all_members_for_season(g, season_year)

    @tasks.loop(time=ENFORCE_TIME_UTC)
    async def season_enforce_loop(self) -> None:
        # Trigger only on April 7 (UTC)
        today = _today_utc()
        if today.month != REVERIFY_DEADLINE_MONTH or today.day != REVERIFY_DEADLINE_DAY:
            return

        season_year = today.year
        for g in self.bot.guilds:
            await self._enforce_reverify_for_season(g, season_year)

    @commands.Cog.listener()
    async def on_ready(self):
        print("[VerifyCog] ready")
        await self.ensure_verify_message()

        # Start season loops once
        if not self._season_tasks_started:
            self._season_tasks_started = True
            try:
                self.season_dm_loop.start()
            except RuntimeError:
                pass
            try:
                self.season_enforce_loop.start()
            except RuntimeError:
                pass

        # Catch up if bot was offline at scheduled times
        self.bot.loop.create_task(self._season_catchup())


# -------------------- extension setup --------------------

async def setup(bot: commands.Bot):
    wa = WildApricotClient(
        api_key=config.WA_API_KEY,
        account_id=config.WA_ACCOUNT_ID,
        api_version=getattr(config, "WA_API_VERSION", "v2.1"),
    )
    await wa.start()

    cog = VerifyCog(bot, wa)

    # Persistent button wiring (required for interactions after restarts)
    bot.add_view(cog.view)

    await bot.add_cog(cog)
