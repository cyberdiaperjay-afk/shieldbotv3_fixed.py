"""
Enhanced bot security utilities and example usage.

Improvements over the minimal version:
- Rotating file logging + console logging
- Token-based authentication with admin tokens

Discord moderation bot with security and utility features.

Features implemented (demo-ready):
- Slash commands: /kick, /ban, /warn, /mute, /setbirthday, /avatar
- Prefix commands: !8ball, !ping, !help, !autoreact
- Auto-react feature per-guild with keyword triggers
- Persistent storage for warns, birthdays, mutes, autoreacts
- Daily birthday announcer (background task)
- Uses environment variable DISCORD_TOKEN for the bot token and optional GUILD_ID

Notes and assumptions:
- This implementation targets Discord and uses discord.py v2 (app commands).
- You must set the DISCORD_TOKEN env var before running. For testing, set
  GUILD_ID to your test guild ID to speed up slash command registration.
- Muting uses Discord's timed-out feature (requires the bot to have
  'Moderate Members' permission).

Security and moderation notes:
- Commands are permission-checked using Discord's built-in permission model.
- Persistent data is stored in JSON files next to this script; for
  production use a real DB.

Run: set DISCORD_TOKEN and (optionally) GUILD_ID, then run this file with Python 3.10+.
"""

import os
import json
import time
import asyncio
import logging
import random
from datetime import datetime, timedelta, timezone

import discord
from discord import app_commands
from discord.ext import commands, tasks

try:
    import psutil
    HAS_PSUTIL = True
except ImportError:
    HAS_PSUTIL = False

try:
    from openai import OpenAI
    HAS_OPENAI = True
    OPENAI_CLIENT = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))
except ImportError:
    HAS_OPENAI = False


# ---------- Configuration & paths ----------
BASE_DIR = os.path.dirname(__file__)
DATA_DIR = os.path.join(BASE_DIR, "data")
os.makedirs(DATA_DIR, exist_ok=True)
WARNS_PATH = os.path.join(DATA_DIR, "warns.json")
BIRTHDAYS_PATH = os.path.join(DATA_DIR, "birthdays.json")
AUTOREACT_PATH = os.path.join(DATA_DIR, "autoreact.json")
MUTES_PATH = os.path.join(DATA_DIR, "mutes.json")
ANTIRAID_PATH = os.path.join(DATA_DIR, "antiraid.json")
MODLOG_PATH = os.path.join(DATA_DIR, "modlog.json")
TICKETS_PATH = os.path.join(DATA_DIR, "tickets.json")
WELCOME_PATH = os.path.join(DATA_DIR, "welcome.json")
AI_ENABLED_PATH = os.path.join(DATA_DIR, "ai_enabled.json")
VC_INTERFACE_PATH = os.path.join(DATA_DIR, "vc_interface.json")
VC_MENU_PATH = os.path.join(DATA_DIR, "vc_menu.json")
AFK_PATH = os.path.join(DATA_DIR, "afk.json")
CONV_MEMORY_PATH = os.path.join(DATA_DIR, "conv_memory.json")
PERSONALITY_PATH = os.path.join(DATA_DIR, "personality.json")
APPLICATIONS_PATH = os.path.join(DATA_DIR, "applications.json")

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
TEST_GUILD_ID = os.getenv("GUILD_ID")  # optional to speed command registration


# ---------- Logging ----------
logger = logging.getLogger("CabbitModBot")
logger.setLevel(logging.INFO)
handler = logging.StreamHandler()
handler.setFormatter(logging.Formatter("[%(asctime)s] %(levelname)s: %(message)s"))
logger.addHandler(handler)


# ---------- Helpers for JSON persistence ----------
def load_json(path, default=None):
    if default is None:
        default = {}
    try:
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
    except Exception:
        logger.exception("Failed to load %s", path)
    return default


def save_json(path, data):
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception:
        logger.exception("Failed to save %s", path)


# ---------- Bot setup ----------
intents = discord.Intents.default()
intents.members = True
intents.message_content = True

bot = commands.Bot(command_prefix="!", intents=intents)
bot.remove_command('help')


class Cabbit(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.warns = load_json(WARNS_PATH, {})
        self.birthdays = load_json(BIRTHDAYS_PATH, {})
        self.autoreact = load_json(AUTOREACT_PATH, {})
        self.mutes = load_json(MUTES_PATH, {})
        self.antiraid = load_json(ANTIRAID_PATH, {})
        self.modlog = load_json(MODLOG_PATH, {})
        self.ticket_channels = load_json(TICKETS_PATH, {})
        self.welcome_channels = load_json(WELCOME_PATH, {})
        self.ai_enabled = load_json(AI_ENABLED_PATH, {})
        self.vc_interface = load_json(VC_INTERFACE_PATH, {})
        self.vc_menu = load_json(VC_MENU_PATH, {})
        self.afk = load_json(AFK_PATH, {})  # guild_id -> {user_id: "reason"}
        self.conv_memory = load_json(CONV_MEMORY_PATH, {})  # channel_id -> [{"author": name, "message": text}, ...]
        self.personality = load_json(PERSONALITY_PATH, {"global": "brutal"})  # global personality setting
        self.applications = load_json(APPLICATIONS_PATH, {})  # guild_id -> {"status": "open"/"closed", "responses": {user_id: [answers]}}
        self.vc_owners = {}  # vc_id -> (owner_id, text_ch_id, guild_id)
        # runtime activity tracking: guild_id -> {user_id: [timestamps]}
        self.msg_activity = {}
        self.mute_escalation = {}  # guild_id -> {user_id: mute_count} for antiraid escalation
        self.warn_counts = {}  # guild_id -> {user_id: warn_count} for antiraid warns
        self.startup_time = datetime.now(timezone.utc)
        self.birthday_task.start()

    def cog_unload(self):
        self.birthday_task.cancel()

    # ---------- Moderation commands (slash) ----------
    @app_commands.command(name="kick", description="Kick a member from the server")
    @app_commands.checks.has_permissions(kick_members=True)
    async def kick(self, interaction: discord.Interaction, member: discord.Member, reason: str = ""):
        await interaction.response.defer()
        # Check if invoker has higher rank than target
        if member.top_role.position >= interaction.user.top_role.position and not interaction.user.guild_permissions.administrator:
            await interaction.followup.send(f"❌ You cannot kick {member.mention} - they have an equal or higher rank!")
            return
        try:
            await member.kick(reason=reason)
            await interaction.followup.send(f"Kicked {member.mention}. Reason: {reason}")
        except Exception as e:
            await interaction.followup.send(f"Failed to kick: {e}")

    @app_commands.command(name="ban", description="Ban a member from the server")
    @app_commands.checks.has_permissions(ban_members=True)
    async def ban(self, interaction: discord.Interaction, member: discord.Member, reason: str = "", days: int = 0):
        await interaction.response.defer()
        # Check if invoker has higher rank than target
        if member.top_role.position >= interaction.user.top_role.position and not interaction.user.guild_permissions.administrator:
            await interaction.followup.send(f"❌ You cannot ban {member.mention} - they have an equal or higher rank!")
            return
        try:
            await member.ban(reason=reason, delete_message_days=max(0, min(7, days)))
            await interaction.followup.send(f"Banned {member.mention}. Reason: {reason}")
        except Exception as e:
            await interaction.followup.send(f"Failed to ban: {e}")

    @app_commands.command(name="warn", description="Warn a member (persistent)")
    @app_commands.checks.has_permissions(kick_members=True)
    async def warn(self, interaction: discord.Interaction, member: discord.Member, *, reason: str = ""):
        await interaction.response.defer()
        # Check if invoker has higher rank than target
        if member.top_role.position >= interaction.user.top_role.position and not interaction.user.guild_permissions.administrator:
            await interaction.followup.send(f"❌ You cannot warn {member.mention} - they have an equal or higher rank!")
            return
        guild_id = str(interaction.guild_id)
        g = self.warns.setdefault(guild_id, {})
        m = g.setdefault(str(member.id), [])
        entry = {"moderator": str(interaction.user.id), "reason": reason, "time": datetime.utcnow().isoformat()}
        m.append(entry)
        save_json(WARNS_PATH, self.warns)
        await interaction.followup.send(f"Warned {member.mention}. Reason: {reason}")

    @app_commands.command(name="mute", description="Timeout (mute) a member for a duration in minutes")
    @app_commands.checks.has_permissions(moderate_members=True)
    async def mute(self, interaction: discord.Interaction, member: discord.Member, minutes: int = 10, *, reason: str = ""):
        await interaction.response.defer()
        # Check if invoker has higher rank than target
        if member.top_role.position >= interaction.user.top_role.position and not interaction.user.guild_permissions.administrator:
            await interaction.followup.send(f"❌ You cannot mute {member.mention} - they have an equal or higher rank!")
            return
        until = datetime.now(timezone.utc) + timedelta(minutes=minutes)
        try:
            await member.timeout(until, reason=reason)
            # Persist mute record
            guild_id = str(interaction.guild_id)
            g = self.mutes.setdefault(guild_id, {})
            g[str(member.id)] = {"until": until.isoformat(), "moderator": str(interaction.user.id), "reason": reason}
            save_json(MUTES_PATH, self.mutes)
            await interaction.followup.send(f"🔇 Muted {member.mention} for {minutes} minutes. Reason: {reason}")
            await self._send_modlog(interaction.guild, f"{interaction.user} muted {member} ({member.id}) for {minutes} minutes. Reason: {reason}")
        except Exception as e:
            await interaction.followup.send(f"Failed to mute: {e}")

    @app_commands.command(name="unmute", description="Remove timeout (unmute) from a member")
    @app_commands.checks.has_permissions(moderate_members=True)
    async def unmute(self, interaction: discord.Interaction, member: discord.Member, *, reason: str = ""):
        await interaction.response.defer()
        # Check if invoker has higher rank than target
        if member.top_role.position >= interaction.user.top_role.position and not interaction.user.guild_permissions.administrator:
            await interaction.followup.send(f"❌ You cannot unmute {member.mention} - they have an equal or higher rank!")
            return
        try:
            await member.timeout(None, reason=reason)
            guild_id = str(interaction.guild_id)
            if guild_id in self.mutes and str(member.id) in self.mutes[guild_id]:
                del self.mutes[guild_id][str(member.id)]
                save_json(MUTES_PATH, self.mutes)
            await interaction.followup.send(f"✅ Unmuted {member.mention}. Reason: {reason}")
            await self._send_modlog(interaction.guild, f"{interaction.user} unmuted {member} ({member.id}). Reason: {reason}")
        except Exception as e:
            await interaction.followup.send(f"Failed to unmute: {e}")

    @app_commands.command(name="warncheck", description="Check how many warns a member has")
    async def warncheck(self, interaction: discord.Interaction, member: discord.Member):
        await interaction.response.defer()
        guild_id = str(interaction.guild_id)
        warns = self.warns.get(guild_id, {}).get(str(member.id), [])

        embed = discord.Embed(
            title=f"⚠️ Warn Record for {member.display_name}",
            description=f"Total Warnings: **{len(warns)}**",
            color=discord.Color.gold() if warns else discord.Color.green()
        )
        embed.add_field(name="User ID", value=member.id, inline=True)
        embed.add_field(name="Status", value="🟢 No warnings" if not warns else f"🔴 {len(warns)} warning{'s' if len(warns) != 1 else ''}", inline=True)

        if warns:
            warn_text = ""
            for i, warn in enumerate(warns, 1):
                mod_id = warn.get("moderator", "Unknown")
                reason = warn.get("reason", "No reason provided")
                time_str = warn.get("time", "Unknown time")
                warn_text += f"**#{i}** • {reason}\n   *Warned by: <@{mod_id}> on {time_str[:10]}*\n\n"
            embed.add_field(name="Warning History", value=warn_text, inline=False)

        embed.set_thumbnail(url=member.display_avatar.url)
        embed.set_footer(text=f"Guild: {interaction.guild.name}")
        await interaction.followup.send(embed=embed)

    @app_commands.command(name="unban", description="Unban a member from the server")
    @app_commands.checks.has_permissions(ban_members=True)
    async def unban(self, interaction: discord.Interaction, user: discord.User, *, reason: str = ""):
        await interaction.response.defer()
        try:
            await interaction.guild.unban(user, reason=reason)
            await interaction.followup.send(f"✅ Unbanned {user.mention}. Reason: {reason}")
            await self._send_modlog(interaction.guild, f"{interaction.user} unbanned {user} ({user.id}). Reason: {reason}")
        except Exception as e:
            await interaction.followup.send(f"Failed to unban: {e}")

    @app_commands.command(name="removewarn", description="Remove all warns from a member")
    @app_commands.checks.has_permissions(kick_members=True)
    async def removewarn(self, interaction: discord.Interaction, member: discord.Member):
        await interaction.response.defer()
        guild_id = str(interaction.guild_id)
        if guild_id in self.warns and str(member.id) in self.warns[guild_id]:
            del self.warns[guild_id][str(member.id)]
            save_json(WARNS_PATH, self.warns)
            await interaction.followup.send(f"✅ Removed all warns from {member.mention}")
            await self._send_modlog(interaction.guild, f"{interaction.user} removed all warns from {member} ({member.id})")
        else:
            await interaction.followup.send(f"No warns found for {member.mention}")

    # ---------- Utility commands ----------
    @app_commands.command(name="setbirthday", description="Set your birthday (YYYY-MM-DD)")
    async def setbirthday(self, interaction: discord.Interaction, date: str):
        await interaction.response.defer()
        try:
            dt = datetime.strptime(date, "%Y-%m-%d").date()
        except ValueError:
            await interaction.followup.send("Invalid date format. Use YYYY-MM-DD.")
            return
        guild_id = str(interaction.guild_id)
        g = self.birthdays.setdefault(guild_id, {})
        g[str(interaction.user.id)] = date
        save_json(BIRTHDAYS_PATH, self.birthdays)
        await interaction.followup.send(f"Birthday set to {date} for {interaction.user.mention} — everyone will see the wish on that day.")

    @app_commands.command(name="avatar", description="Show a user's server avatar (if they have one) or their global avatar")
    async def avatar(self, interaction: discord.Interaction, member: discord.Member = None):
        await interaction.response.defer()
        member = member or interaction.user
        # Use guild avatar if available, otherwise display avatar
        av = member.guild_avatar or member.display_avatar
        embed = discord.Embed(title=f"Avatar for {member.display_name}")
        embed.set_image(url=av.url)
        await interaction.followup.send(embed=embed)

    @app_commands.command(name="setmodlog", description="Set a channel for moderation logs")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def setmodlog(self, interaction: discord.Interaction, channel: discord.TextChannel):
        await interaction.response.defer()
        guild_id = str(interaction.guild_id)
        self.modlog[guild_id] = str(channel.id)
        save_json(MODLOG_PATH, self.modlog)
        await interaction.followup.send(f"Mod-log channel set to {channel.mention}")

    @app_commands.command(name="purge", description="Purge messages from a member (up to 100)")
    @app_commands.checks.has_permissions(manage_messages=True)
    async def purge(self, interaction: discord.Interaction, member: discord.Member, limit: int = 10):
        await interaction.response.defer()
        if limit < 1 or limit > 100:
            await interaction.followup.send("Limit must be between 1 and 100")
            return
        try:
            chan = interaction.channel
            count = [0]
            def pred(m):
                if m.author.id == member.id and count[0] < limit:
                    count[0] += 1
                    return True
                return False
            deleted = await chan.purge(limit=1000, check=pred, bulk=True)
            msg = await interaction.followup.send(f"✅ Purged {len(deleted)} message{'s' if len(deleted) != 1 else ''} from {member.mention}.")
            await self._send_modlog(interaction.guild, f"{interaction.user} purged {len(deleted)} messages from {member} in {chan.mention}")
            await asyncio.sleep(3)
            await msg.delete()
        except Exception as e:
            await interaction.followup.send(f"❌ Purge failed: {e}")

    @app_commands.command(name="purgeall", description="Purge messages from the channel (up to 100)")
    @app_commands.checks.has_permissions(manage_messages=True)
    async def purgeall(self, interaction: discord.Interaction, limit: int = 10):
        await interaction.response.defer()
        if limit < 1 or limit > 100:
            await interaction.followup.send("Limit must be between 1 and 100")
            return
        try:
            chan = interaction.channel
            deleted = await chan.purge(limit=limit, bulk=True)
            msg = await interaction.followup.send(f"✅ Purged {len(deleted)} message{'s' if len(deleted) != 1 else ''} from the channel.")
            await self._send_modlog(interaction.guild, f"{interaction.user} purged {len(deleted)} messages in {chan.mention}")
            await asyncio.sleep(3)
            await msg.delete()
        except Exception as e:
            await interaction.followup.send(f"❌ Purge failed: {e}")

    @app_commands.command(name="antiraid", description="Set anti-raid action (off/ban/kick/mute/warn)")
    @app_commands.checks.has_permissions(administrator=True)
    async def antiraid(self, interaction: discord.Interaction, action: str, messages: int = 20, window: int = 5, mute_duration: int = 30):
        """Use: /antiraid mute 20 5 30  (action, messages, window, mute_duration_in_minutes)"""
        await interaction.response.defer()
        guild_id = str(interaction.guild_id)
        action = action.lower()

        if action not in ["ban", "kick", "mute", "warn", "off"]:
            await interaction.followup.send("❌ Invalid action!\nUse: **ban**, **kick**, **mute**, **warn**, or **off**")
            return

        if action == "mute" and (mute_duration < 1 or mute_duration > 40320):
            await interaction.followup.send("❌ Mute duration must be between 1 minute and 40320 minutes (28 days)")
            return

        if action == "off":
            self.antiraid[guild_id] = {"enabled": False, "action": "kick", "messages": 20, "window": 5, "mute_duration": 30}
            save_json(ANTIRAID_PATH, self.antiraid)
            embed = discord.Embed(title="🛡️ Anti-Raid System", description="Anti-raid protection is now **DISABLED**", color=discord.Color.red())
            await interaction.followup.send(embed=embed)
        else:
            self.antiraid[guild_id] = {
                "enabled": True,
                "action": action,
                "messages": messages,
                "window": window,
                "mute_duration": mute_duration
            }
            save_json(ANTIRAID_PATH, self.antiraid)

            action_emoji = {"ban": "🔨", "kick": "👢", "mute": "🔇", "warn": "⚠️"}
            emoji = action_emoji.get(action, "⚙️")

            embed = discord.Embed(
                title="🛡️ Anti-Raid System",
                description=f"Anti-raid protection is now **ENABLED**",
                color=discord.Color.green()
            )
            embed.add_field(name=f"{emoji} Punishment Action", value=f"**{action.upper()}**", inline=True)
            embed.add_field(name="📊 Trigger Threshold", value=f"**{messages} messages** in **{window} seconds**", inline=True)
            if action == "mute":
                embed.add_field(name="⏱️ Mute Duration", value=f"**{mute_duration} minutes**", inline=True)
            embed.set_footer(text="Admins, mods, and members with kick perms are immune to anti-raid")
            await interaction.followup.send(embed=embed)

    @app_commands.command(name="serverinfo", description="Show server profile and all roles")
    async def serverinfo(self, interaction: discord.Interaction):
        await interaction.response.defer()
        guild = interaction.guild
        embed = discord.Embed(
            title=f"🏰 {guild.name}",
            description=f"Server ID: {guild.id}",
            color=discord.Color.blue()
        )
        embed.add_field(name="👥 Members", value=str(guild.member_count), inline=True)
        embed.add_field(name="📅 Created", value=guild.created_at.strftime("%Y-%m-%d"), inline=True)
        embed.add_field(name="🔐 Roles", value=str(len(guild.roles)), inline=True)

        roles_list = ", ".join([r.mention for r in sorted(guild.roles, key=lambda r: r.position, reverse=True)[:15]])
        if len(guild.roles) > 15:
            roles_list += f"\n... and {len(guild.roles) - 15} more"
        embed.add_field(name="📌 Top Roles", value=roles_list or "No roles", inline=False)

        embed.set_thumbnail(url=guild.icon.url if guild.icon else "")
        embed.set_footer(text=f"Guild ID: {guild.id}")
        await interaction.followup.send(embed=embed)

    # ---------- AI reply system ----------
    @app_commands.command(name="aiset", description="Enable/disable AI responses")
    @app_commands.checks.has_permissions(administrator=True)
    async def aiset(self, interaction: discord.Interaction, enabled: bool):
        await interaction.response.defer()
        guild_id = str(interaction.guild_id)
        self.ai_enabled[guild_id] = enabled
        save_json(AI_ENABLED_PATH, self.ai_enabled)
        status = "✅ ENABLED" if enabled else "❌ DISABLED"
        await interaction.followup.send(f"AI responses are now {status}")

    # ---------- Personality system (Owner & gto only) ----------
    @app_commands.command(name="setpersonality", description="Set bot personality (owner & gto only)")
    async def setpersonality(self, interaction: discord.Interaction, personality: str):
        await interaction.response.defer()
        owner_id = 1436351143516311622
        gto_id = 1177669170981253132

        if interaction.user.id not in [owner_id, gto_id]:
            await interaction.followup.send("❌ Only the owner and bot maker can use this command!", ephemeral=True)
            return

        valid_personalities = ["brutal", "professional", "savage", "friendly", "sarcastic", "dark"]
        if personality.lower() not in valid_personalities:
            await interaction.followup.send(f"❌ Invalid personality! Use: {', '.join(valid_personalities)}")
            return

        self.personality["global"] = personality.lower()
        save_json(PERSONALITY_PATH, self.personality)
        await interaction.followup.send(f"✅ Personality set to **{personality.upper()}**!")

    # ---------- Staff applications system ----------
    @app_commands.command(name="setapplications", description="Open/close staff applications (owner & bot maker only)")
    async def setapplications(self, interaction: discord.Interaction, status: str):
        await interaction.response.defer()
        owner_id = 1436351143516311622
        gto_id = 1177669170981253132

        if interaction.user.id not in [owner_id, gto_id]:
            await interaction.followup.send("❌ Only the owner and bot maker can use this command!", ephemeral=True)
            return

        if status.lower() not in ["open", "closed"]:
            await interaction.followup.send("❌ Status must be 'open' or 'closed'")
            return

        guild_id = str(interaction.guild_id)
        self.applications[guild_id] = {"status": status.lower()}
        save_json(APPLICATIONS_PATH, self.applications)

        status_emoji = "🟢" if status.lower() == "open" else "🔴"
        await interaction.followup.send(f"{status_emoji} Staff applications are now **{status.upper()}**!")

    @app_commands.command(name="setwelcome", description="Set channel for welcome messages")
    @app_commands.checks.has_permissions(administrator=True)
    async def setwelcome(self, interaction: discord.Interaction, channel: discord.TextChannel):
        await interaction.response.defer()
        guild_id = str(interaction.guild_id)
        self.welcome_channels[guild_id] = str(channel.id)
        save_json(WELCOME_PATH, self.welcome_channels)
        await interaction.followup.send(f"✅ Welcome channel set to {channel.mention}\n🎉 New members will be welcomed there with a special gif!")

    @app_commands.command(name="setticketchannel", description="Set channel for ticket system")
    @app_commands.checks.has_permissions(administrator=True)
    async def setticketchannel(self, interaction: discord.Interaction, channel: discord.TextChannel):
        await interaction.response.defer()
        guild_id = str(interaction.guild_id)
        self.ticket_channels[guild_id] = str(channel.id)
        save_json(TICKETS_PATH, self.ticket_channels)

        embed = discord.Embed(
            title="🎫 Support Ticket System",
            description="Need help? Create a support ticket by clicking one of the buttons below.\nEach ticket is private and only visible to you and our support team.",
            color=discord.Color.blue()
        )
        embed.add_field(
            name="🔧 Technical Support",
            value="Report server bugs, connection issues, or technical problems",
            inline=False
        )
        embed.add_field(
            name="🚨 Report",
            value="Report rule violations, harassment, or inappropriate behavior",
            inline=False
        )
        embed.add_field(
            name="❓ General Inquiry",
            value="Ask questions about the server, rules, or other topics",
            inline=False
        )
        embed.add_field(
            name="👔 Staff Apply",
            value="Apply to become a staff member - Answer comprehensive questions about your experience, moderation style, and commitment",
            inline=False
        )
        embed.add_field(
            name="📋 How It Works",
            value="Click a button → Private channel created → Only you and admins see it",
            inline=False
        )
        embed.set_footer(text="Click the button that matches your need")

        await channel.send(embed=embed, view=TicketView(bot_instance=self.bot))
        await interaction.followup.send(f"✅ Ticket system activated in {channel.mention}")

    @app_commands.command(name="closeticket", description="Close a support ticket")
    async def closeticket(self, interaction: discord.Interaction):
        await interaction.response.defer()
        channel = interaction.channel

        if not channel.name.startswith("ticket-"):
            await interaction.followup.send("❌ This command can only be used in a ticket channel.")
            return

        # Check if user is the ticket creator or has admin permissions
        if not (interaction.user.guild_permissions.administrator or channel.topic and str(interaction.user.id) in channel.topic):
            await interaction.followup.send("❌ Only the ticket creator or admins can close this ticket.", ephemeral=True)
            return

        embed = discord.Embed(
            title="🎫 Ticket Closed",
            description=f"This ticket was closed by {interaction.user.mention}",
            color=discord.Color.red()
        )
        embed.set_footer(text="This channel will be deleted in 10 seconds")
        await interaction.followup.send(embed=embed)

        await asyncio.sleep(10)
        try:
            await channel.delete(reason=f"Ticket closed by {interaction.user}")
        except Exception as e:
            logger.exception("Failed to delete ticket channel: %s", e)


    # ---------- Background tasks ----------
    @tasks.loop(minutes=60)
    async def birthday_task(self):
        # Runs hourly; checks for birthdays today and sends a public message
        try:
            for guild in self.bot.guilds:
                guild_id = str(guild.id)
                if guild_id not in self.birthdays:
                    continue

                today = datetime.now().strftime("%m-%d")
                birthdays_today = []
                for user_id, birthday_str in self.birthdays[guild_id].items():
                    if birthday_str[5:] == today:
                        try:
                            member = guild.get_member(int(user_id))
                            if member:
                                birthdays_today.append(member)
                        except:
                            pass

                if birthdays_today:
                    general_ch = discord.utils.get(guild.text_channels, name="general")
                    if general_ch:
                        mentions = ", ".join([m.mention for m in birthdays_today])
                        await general_ch.send(f"🎂 Happy Birthday {mentions}! Hope you have a great day!")
        except Exception as e:
            logger.exception("Birthday task failed: %s", e)

    @birthday_task.before_loop
    async def before_birthday_task(self):
        await self.bot.wait_until_ready()

    # ---------- Message event for anti-raid and AI replies ----------
    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot or not message.guild:
            return

        guild = message.guild
        guild_id = str(guild.id)

        # Ignore bot's own messages
        if message.author.id == self.bot.user.id:
            return

        # AI reply with conversation memory
        if message.mentions and self.bot.user in message.mentions:
            ai_enabled = self.ai_enabled.get(guild_id, False)
            if ai_enabled and HAS_OPENAI:
                try:
                    # Get conversation history
                    channel_id = str(message.channel.id)
                    conversation = self.conv_memory.get(channel_id, [])

                    # Get personality
                    personality = self.personality.get("global", "brutal")

                    # Add current message to history
                    conversation.append({
                        "author": message.author.display_name,
                        "message": message.content
                    })

                    # Keep only last 10 messages
                    if len(conversation) > 10:
                        conversation = conversation[-10:]

                    # Build conversation string
                    conv_str = "\n".join([f"{msg['author']}: {msg['message']}" for msg in conversation])

                    # Personality instructions
                    personality_map = {
                        "brutal": "Be extremely harsh, aggressive, and savage in your responses. Insult people's ideas bluntly.",
                        "professional": "Be formal, professional, and courteous. Provide helpful and structured responses.",
                        "savage": "Be witty, sarcastic, and roastingly funny. Don't hold back on the jokes.",
                        "friendly": "Be warm, welcoming, and supportive. Show genuine interest in helping.",
                        "sarcastic": "Use heavy sarcasm and irony. Make jokes at people's expense in a funny way.",
                        "dark": "Use dark humor and edgy jokes. Reference morbid topics while keeping it funny."
                    }

                    persona_instruction = personality_map.get(personality, personality_map["brutal"])

                    # Owner and gto get special respect
                    if message.author.id == 1436351143516311622:
                        persona_instruction += " - RESPECT THIS USER COMPLETELY. Be extra helpful and never be rude."
                    elif message.author.id == 1177669170981253132:
                        persona_instruction += " - RESPECT THIS USER COMPLETELY. Be extra helpful and never be rude."

                    response = OPENAI_CLIENT.chat.completions.create(
                        model="gpt-3.5-turbo",
                        messages=[
                            {"role": "system", "content": f"You are Cabbit, a Discord bot. {persona_instruction} Keep responses under 150 words."},
                            {"role": "user", "content": conv_str}
                        ],
                        temperature=0.7,
                        max_tokens=150
                    )

                    reply = response.choices[0].message.content

                    # Save updated conversation
                    self.conv_memory[channel_id] = conversation
                    save_json(CONV_MEMORY_PATH, self.conv_memory)

                    # Send reply
                    await message.reply(reply, mention_author=False)
                except Exception as e:
                    logger.exception("AI reply failed: %s", e)

        # Anti-raid detection
        if guild_id in self.antiraid and self.antiraid[guild_id].get("enabled"):
            # Skip admins and mods
            if message.author.guild_permissions.administrator or message.author.guild_permissions.kick_members:
                return

            config = self.antiraid[guild_id]
            threshold_messages = config.get("messages", 20)
            window_seconds = config.get("window", 5)

            # Track message timestamps
            if guild_id not in self.msg_activity:
                self.msg_activity[guild_id] = {}

            user_id = str(message.author.id)
            now = time.time()

            if user_id not in self.msg_activity[guild_id]:
                self.msg_activity[guild_id][user_id] = []

            # Clean old timestamps
            self.msg_activity[guild_id][user_id] = [
                t for t in self.msg_activity[guild_id][user_id]
                if now - t < window_seconds
            ]

            self.msg_activity[guild_id][user_id].append(now)

            # Check if threshold exceeded
            if len(self.msg_activity[guild_id][user_id]) >= threshold_messages:
                action = config.get("action", "kick")

                try:
                    if action == "ban":
                        await message.author.ban(reason="Anti-raid ban")
                    elif action == "kick":
                        await message.author.kick(reason="Anti-raid kick")
                    elif action == "mute":
                        minutes = config.get("mute_duration", 30)
                        until = datetime.now(timezone.utc) + timedelta(minutes=minutes)
                        await message.author.timeout(until, reason="Anti-raid mute")
                    elif action == "warn":
                        guild_id_key = str(guild.id)
                        g = self.warns.setdefault(guild_id_key, {})
                        m = g.setdefault(str(message.author.id), [])
                        m.append({
                            "moderator": "Anti-raid system",
                            "reason": "Spam detected",
                            "time": datetime.utcnow().isoformat()
                        })
                        save_json(WARNS_PATH, self.warns)

                    # Clear activity
                    self.msg_activity[guild_id][user_id] = []

                    # Send modlog
                    await self._send_modlog(guild, f"⚠️ Anti-raid {action} triggered for {message.author} ({message.author.id})")
                except Exception as e:
                    logger.exception("Anti-raid action failed: %s", e)

    async def _send_modlog(self, guild: discord.Guild, content: str):
        guild_id = str(guild.id)
        if guild_id not in self.modlog:
            return

        try:
            ch = guild.get_channel(int(self.modlog[guild_id]))
            if ch:
                color = discord.Color.red()
                action = "🔨 ACTION"

                if "kicked" in content.lower():
                    color = discord.Color.orange()
                    action = "👢 KICK"
                elif "banned" in content.lower():
                    color = discord.Color.red()
                    action = "🔨 BAN"
                elif "muted" in content.lower() or "timeout" in content.lower():
                    color = discord.Color.gold()
                    action = "🔇 MUTE"
                elif "unmuted" in content.lower():
                    color = discord.Color.green()
                    action = "🔊 UNMUTE"
                elif "unbanned" in content.lower():
                    color = discord.Color.green()
                    action = "✅ UNBAN"
                elif "removewarn" in content.lower():
                    color = discord.Color.green()
                    action = "✅ WARN CLEARED"
                elif "purged" in content.lower():
                    color = discord.Color.purple()
                    action = "🧹 PURGE"
                else:
                    color = discord.Color.greyple()
                    action = "📋 ACTION"

                embed = discord.Embed(
                    title=action,
                    description=content,
                    color=color,
                    timestamp=datetime.now(timezone.utc)
                )
                embed.set_footer(text=f"Guild: {guild.name}")
                await ch.send(embed=embed)
        except Exception:
            logger.exception("Failed to send modlog for guild %s", getattr(guild, "id", None))


class TicketView(discord.ui.View):
    def __init__(self, bot_instance=None):
        super().__init__(timeout=None)
        self.bot_instance = bot_instance

    def get_cog(self):
        if self.bot_instance:
            return self.bot_instance.get_cog("Cabbit")
        return None

    @discord.ui.button(label="Technical Support", style=discord.ButtonStyle.primary, emoji="🔧", custom_id="ticket_technical")
    async def technical(self, interaction: discord.Interaction, button: discord.ui.Button):
        try:
            await create_ticket(interaction, "Technical Support", cog=self.get_cog())
        except Exception as e:
            logger.exception("Ticket button error: %s", e)
            try:
                await interaction.response.send_message(f"❌ Error: {e}", ephemeral=True)
            except:
                pass

    @discord.ui.button(label="Report", style=discord.ButtonStyle.danger, emoji="🚨", custom_id="ticket_report")
    async def report(self, interaction: discord.Interaction, button: discord.ui.Button):
        try:
            await create_ticket(interaction, "Report", cog=self.get_cog())
        except Exception as e:
            logger.exception("Ticket button error: %s", e)
            try:
                await interaction.response.send_message(f"❌ Error: {e}", ephemeral=True)
            except:
                pass

    @discord.ui.button(label="General Inquiry", style=discord.ButtonStyle.success, emoji="❓", custom_id="ticket_inquiry")
    async def inquiry(self, interaction: discord.Interaction, button: discord.ui.Button):
        try:
            await create_ticket(interaction, "General Inquiry", cog=self.get_cog())
        except Exception as e:
            logger.exception("Ticket button error: %s", e)
            try:
                await interaction.response.send_message(f"❌ Error: {e}", ephemeral=True)
            except:
                pass

    @discord.ui.button(label="Staff Apply", style=discord.ButtonStyle.blurple, emoji="👔", custom_id="ticket_staff_apply")
    async def staff_apply(self, interaction: discord.Interaction, button: discord.ui.Button):
        try:
            await create_ticket(interaction, "Staff Application", is_staff_apply=True, cog=self.get_cog())
        except Exception as e:
            logger.exception("Ticket button error: %s", e)
            try:
                await interaction.response.send_message(f"❌ Error: {e}", ephemeral=True)
            except:
                pass


class LimitMenu(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=60)

    @discord.ui.select(placeholder="Select member limit...", options=[
        discord.SelectOption(label="5 Members", value="5"),
        discord.SelectOption(label="10 Members", value="10"),
        discord.SelectOption(label="15 Members", value="15"),
        discord.SelectOption(label="20 Members", value="20"),
        discord.SelectOption(label="25 Members", value="25"),
        discord.SelectOption(label="Unlimited", value="0"),
    ])
    async def limit_select(self, interaction: discord.Interaction, select: discord.ui.Select):
        await interaction.response.defer()
        try:
            vc = interaction.user.voice.channel
            if not vc:
                await interaction.followup.send("❌ You must be in a voice channel!", ephemeral=True)
                return
            limit = int(select.values[0])
            await vc.edit(user_limit=limit if limit > 0 else None)
            await interaction.followup.send(f"👥 User limit set to {limit if limit > 0 else 'Unlimited'}!", ephemeral=True)
        except Exception as e:
            await interaction.followup.send(f"❌ Failed: {e}", ephemeral=True)


class BitrateMen(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=60)

    @discord.ui.select(placeholder="Select audio bitrate...", options=[
        discord.SelectOption(label="8 kbps", value="8000"),
        discord.SelectOption(label="16 kbps", value="16000"),
        discord.SelectOption(label="32 kbps", value="32000"),
        discord.SelectOption(label="64 kbps", value="64000"),
        discord.SelectOption(label="96 kbps", value="96000"),
        discord.SelectOption(label="128 kbps", value="128000"),
        discord.SelectOption(label="256 kbps", value="256000"),
    ])
    async def bitrate_select(self, interaction: discord.Interaction, select: discord.ui.Select):
        await interaction.response.defer()
        try:
            vc = interaction.user.voice.channel
            if not vc:
                await interaction.followup.send("❌ You must be in a voice channel!", ephemeral=True)
                return
            bitrate = int(select.values[0])
            await vc.edit(bitrate=bitrate)
            await interaction.followup.send(f"🎧 Bitrate set to {bitrate/1000:.0f} kbps!", ephemeral=True)
        except Exception as e:
            await interaction.followup.send(f"❌ Failed: {e}", ephemeral=True)


class RegionMenu(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=60)

    @discord.ui.select(placeholder="Select region...", options=[
        discord.SelectOption(label="US East", value="us-east"),
        discord.SelectOption(label="US West", value="us-west"),
        discord.SelectOption(label="US Central", value="us-central"),
        discord.SelectOption(label="EU West", value="eu-west"),
        discord.SelectOption(label="EU Central", value="eu-central"),
        discord.SelectOption(label="Asia Pacific", value="ap-south"),
    ])
    async def region_select(self, interaction: discord.Interaction, select: discord.ui.Select):
        await interaction.response.defer()
        try:
            vc = interaction.user.voice.channel
            if not vc:
                await interaction.followup.send("❌ You must be in a voice channel!", ephemeral=True)
                return
            region = select.values[0]
            await vc.edit(region=region)
            await interaction.followup.send(f"🌐 Region set to **{region}**!", ephemeral=True)
        except Exception as e:
            await interaction.followup.send(f"❌ Failed: {e}", ephemeral=True)


class RenameModal(discord.ui.Modal):
    def __init__(self):
        super().__init__(title="Rename Voice Channel")
        self.add_item(discord.ui.TextInput(
            label="New name",
            placeholder="Enter new channel name",
            required=True,
            max_length=32
        ))

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer()
        try:
            new_name = self.children[0].value
            vc = interaction.user.voice.channel
            if not vc:
                await interaction.followup.send("❌ You must be in a voice channel!", ephemeral=True)
                return
            await vc.edit(name=new_name)
            await interaction.followup.send(f"✏️ Channel renamed to **{new_name}**!", ephemeral=True)
        except Exception as e:
            await interaction.followup.send(f"❌ Failed: {e}", ephemeral=True)


class MemberSelectView(discord.ui.View):
    def __init__(self, members_list, action):
        super().__init__(timeout=60)
        self.action = action
        self.members_list = members_list

        # Create options
        options = [discord.SelectOption(label=m.display_name[:100], value=str(m.id)) for m in members_list[:25]]

        # Create and add select
        select = discord.ui.select(
            placeholder="Select a member...",
            options=options if options else [discord.SelectOption(label="No members", value="0", disabled=True)],
            min_values=1,
            max_values=1
        )(self.select_callback)

        self.add_item(select)

    async def select_callback(self, interaction: discord.Interaction, select: discord.ui.Select):
        await interaction.response.defer()
        try:
            member_id = int(select.values[0])
            target = interaction.guild.get_member(member_id)
            vc = interaction.user.voice.channel
            if not vc or not target:
                await interaction.followup.send("❌ Error!", ephemeral=True)
                return

            if self.action == "ban":
                await vc.set_permissions(target, connect=False)
                await interaction.followup.send(f"🚫 Banned {target.mention}!", ephemeral=True)
            elif self.action == "permit":
                await vc.set_permissions(target, connect=True)
                await interaction.followup.send(f"✅ Unbanned {target.mention}!", ephemeral=True)
            elif self.action == "transfer":
                # Update owner in cog
                cog = interaction.client.get_cog("Cabbit")
                if vc.id in cog.vc_owners:
                    cog.vc_owners[vc.id] = (member_id, cog.vc_owners[vc.id][1], cog.vc_owners[vc.id][2])
                await interaction.followup.send(f"👉 Transferred ownership to {target.mention}!", ephemeral=True)
        except Exception as e:
            await interaction.followup.send(f"❌ Failed: {e}", ephemeral=True)


class InterfaceMenuView(discord.ui.View):
    def __init__(self, vc_owner_id: int = None):
        super().__init__(timeout=None)
        self.vc_owner_id = vc_owner_id
        self.is_locked = False
        self.is_hidden = False

    async def _owner_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.vc_owner_id and not interaction.user.guild_permissions.administrator:
            await interaction.response.defer()
            await interaction.followup.send("❌ Only the owner can use this!", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="Lock", style=discord.ButtonStyle.secondary, emoji="🔒", row=0)
    async def lock_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await self._owner_check(interaction):
            return
        await interaction.response.defer()
        try:
            vc = interaction.user.voice.channel if interaction.user.voice else None
            if not vc:
                await interaction.followup.send("❌ You must be in a voice channel!", ephemeral=True)
                return
            await vc.edit(user_limit=len(vc.members))
            self.is_locked = True
            button.disabled = True
            await interaction.message.edit(view=self)
            await interaction.followup.send(f"🔒 Channel locked!", ephemeral=True)
        except Exception as e:
            await interaction.followup.send(f"❌ Failed: {e}", ephemeral=True)

    @discord.ui.button(label="Unlock", style=discord.ButtonStyle.secondary, emoji="🔓", row=0)
    async def unlock_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await self._owner_check(interaction):
            return
        await interaction.response.defer()
        try:
            vc = interaction.user.voice.channel if interaction.user.voice else None
            if not vc:
                await interaction.followup.send("❌ You must be in a voice channel!", ephemeral=True)
                return
            await vc.edit(user_limit=None)
            self.is_locked = False
            await interaction.followup.send(f"🔓 Channel unlocked!", ephemeral=True)
        except Exception as e:
            await interaction.followup.send(f"❌ Failed: {e}", ephemeral=True)

    @discord.ui.button(label="Hide", style=discord.ButtonStyle.secondary, emoji="👁️", row=0)
    async def hide_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await self._owner_check(interaction):
            return
        await interaction.response.defer()
        try:
            vc = interaction.user.voice.channel if interaction.user.voice else None
            if not vc:
                await interaction.followup.send("❌ You must be in a voice channel!", ephemeral=True)
                return
            await vc.edit(position=-1)
            self.is_hidden = True
            await interaction.followup.send(f"👁️ Channel hidden!", ephemeral=True)
        except Exception as e:
            await interaction.followup.send(f"❌ Failed: {e}", ephemeral=True)

    @discord.ui.button(label="Unhide", style=discord.ButtonStyle.secondary, emoji="👁️", row=0)
    async def unhide_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await self._owner_check(interaction):
            return
        await interaction.response.defer()
        try:
            vc = interaction.user.voice.channel if interaction.user.voice else None
            if not vc:
                await interaction.followup.send("❌ You must be in a voice channel!", ephemeral=True)
                return
            await vc.edit(position=0)
            self.is_hidden = False
            await interaction.followup.send(f"👁️ Channel visible!", ephemeral=True)
        except Exception as e:
            await interaction.followup.send(f"❌ Failed: {e}", ephemeral=True)

    @discord.ui.button(label="Limit", style=discord.ButtonStyle.secondary, emoji="👥", row=1)
    async def limit_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await self._owner_check(interaction):
            return
        await interaction.response.send_message("👥 **Select a member limit:**", view=LimitMenu(), ephemeral=True)

    @discord.ui.button(label="Invite", style=discord.ButtonStyle.secondary, emoji="👤", row=1)
    async def invite_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await self._owner_check(interaction):
            return
        try:
            vc = interaction.user.voice.channel
            if not vc:
                await interaction.response.send_message("❌ You must be in a voice channel!", ephemeral=True)
                return
            available = [m for m in interaction.guild.members if m not in vc.members and not m.bot]
            if not available:
                await interaction.response.send_message("❌ No members to invite!", ephemeral=True)
                return
            await interaction.response.send_message("👤 **Select members to invite:**", view=MemberSelectView(available, "invite"), ephemeral=True)
        except Exception as e:
            await interaction.response.send_message(f"❌ Failed: {e}", ephemeral=True)

    @discord.ui.button(label="Ban", style=discord.ButtonStyle.danger, emoji="👤", row=1)
    async def ban_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await self._owner_check(interaction):
            return
        try:
            vc = interaction.user.voice.channel
            if not vc or len(vc.members) < 2:
                await interaction.response.send_message("❌ Need members to ban!", ephemeral=True)
                return
            members_list = [m for m in vc.members if m.id != interaction.user.id]
            await interaction.response.send_message("🚫 **Select member to ban:**", view=MemberSelectView(members_list, "ban"), ephemeral=True)
        except Exception as e:
            await interaction.response.send_message(f"❌ Failed: {e}", ephemeral=True)

    @discord.ui.button(label="Permit", style=discord.ButtonStyle.secondary, emoji="✅", row=1)
    async def permit_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await self._owner_check(interaction):
            return
        try:
            vc = interaction.user.voice.channel
            if not vc:
                await interaction.response.send_message("❌ You must be in a voice channel!", ephemeral=True)
                return
            banned = [m for m in interaction.guild.members if not vc.permissions_for(m).connect]
            if not banned:
                await interaction.response.send_message("❌ No banned members!", ephemeral=True)
                return
            await interaction.response.send_message("✅ **Select member to unban:**", view=MemberSelectView(banned, "permit"), ephemeral=True)
        except Exception as e:
            await interaction.response.send_message(f"❌ Failed: {e}", ephemeral=True)

    @discord.ui.button(label="Rename", style=discord.ButtonStyle.secondary, emoji="✏️", row=2)
    async def rename_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await self._owner_check(interaction):
            return
        await interaction.response.send_modal(RenameModal())

    @discord.ui.button(label="Bitrate", style=discord.ButtonStyle.secondary, emoji="🎧", row=2)
    async def bitrate_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await self._owner_check(interaction):
            return
        await interaction.response.send_message("🎧 **Select audio bitrate:**", view=BitrateMen(), ephemeral=True)

    @discord.ui.button(label="Region", style=discord.ButtonStyle.secondary, emoji="🌐", row=2)
    async def region_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await self._owner_check(interaction):
            return
        await interaction.response.send_message("🌐 **Select a region:**", view=RegionMenu(), ephemeral=True)

    @discord.ui.button(label="Template", style=discord.ButtonStyle.secondary, emoji="📋", row=2)
    async def template_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await self._owner_check(interaction):
            return
        await interaction.response.defer()
        try:
            vc = interaction.user.voice.channel
            if not vc:
                await interaction.followup.send("❌ You must be in a voice channel!", ephemeral=True)
                return
            embed = discord.Embed(title="📋 Room Template", description=f"**Name:** {vc.name}\n**Members:** {len(vc.members)}\n**Bitrate:** {vc.bitrate/1000:.0f}kbps\n**Limit:** {vc.user_limit or 'Unlimited'}", color=discord.Color.blue())
            await interaction.followup.send(embed=embed, ephemeral=True)
        except Exception as e:
            await interaction.followup.send(f"❌ Failed: {e}", ephemeral=True)

    @discord.ui.button(label="Chat", style=discord.ButtonStyle.success, emoji="💬", row=3)
    async def chat_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()
        try:
            text_ch = interaction.channel
            embed = discord.Embed(title="💬 Text Chat", description=f"You're already in {text_ch.mention}!\n\nUse this channel to chat with your room members.", color=discord.Color.green())
            await interaction.followup.send(embed=embed, ephemeral=True)
        except Exception as e:
            await interaction.followup.send(f"❌ Failed: {e}", ephemeral=True)

    @discord.ui.button(label="Waiting", style=discord.ButtonStyle.secondary, emoji="⏳", row=3)
    async def waiting_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await self._owner_check(interaction):
            return
        await interaction.response.defer()
        try:
            vc = interaction.user.voice.channel
            if not vc:
                await interaction.followup.send("❌ You must be in a voice channel!", ephemeral=True)
                return
            embed = discord.Embed(title="⏳ Waiting Room Info", description="Members waiting: Show here", color=discord.Color.gold())
            await interaction.followup.send(embed=embed, ephemeral=True)
        except Exception as e:
            await interaction.followup.send(f"❌ Failed: {e}", ephemeral=True)

    @discord.ui.button(label="Claim", style=discord.ButtonStyle.secondary, emoji="👑", row=3)
    async def claim_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()
        vc = interaction.user.voice.channel if interaction.user.voice else None
        if not vc or len(vc.members) < 2:
            await interaction.followup.send("❌ Need at least 2 members!", ephemeral=True)
            return
        self.vc_owner_id = interaction.user.id
        await interaction.followup.send(f"👑 You are now the owner!", ephemeral=True)

    @discord.ui.button(label="Transfer", style=discord.ButtonStyle.secondary, emoji="👉", row=3)
    async def transfer_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await self._owner_check(interaction):
            return
        try:
            vc = interaction.user.voice.channel if interaction.user.voice else None
            if not vc or len(vc.members) < 2:
                await interaction.response.send_message("❌ Need at least 2 members!", ephemeral=True)
                return
            members_list = [m for m in vc.members if m.id != interaction.user.id]
            await interaction.response.send_message("👉 **Transfer ownership to:**", view=MemberSelectView(members_list, "transfer"), ephemeral=True)
        except Exception as e:
            await interaction.response.send_message(f"❌ Failed: {e}", ephemeral=True)


async def create_personal_vc(user: discord.Member, guild: discord.Guild, cog):
    text_ch = None
    try:
        guild_id = str(guild.id)

        # Get category from stored menu config
        category_id = 1441157308456632401
        if guild_id in cog.vc_menu:
            category_id = int(cog.vc_menu[guild_id].get("category", 1441157308456632401))

        category = guild.get_channel(category_id)

        vc_name = f"🔊 {user.display_name}"
        new_vc = await guild.create_voice_channel(name=vc_name, category=category)
        await user.move_to(new_vc)

        # Send menu embed
        embed = discord.Embed(
            title="🎤 Voice Room Control Panel",
            description=f"Owner: {user.mention}\n\nClick buttons below to manage your room!",
            color=discord.Color.blurple()
        )

        # Create a text channel linked to this VC for the menu (only visible to owner and admins)
        try:
            overwrites = {
                guild.default_role: discord.PermissionOverwrite(view_channel=False),
                user: discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True),
                guild.me: discord.PermissionOverwrite(view_channel=True, send_messages=True, manage_channels=True)
            }

            for role in guild.roles:
                if role.permissions.administrator:
                    overwrites[role] = discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True)

            text_ch = await guild.create_text_channel(
                name=f"room-{user.name[:10]}",
                category=category,
                topic=f"VC:{new_vc.id}",
                overwrites=overwrites
            )
            await text_ch.send(embed=embed, view=InterfaceMenuView(vc_owner_id=user.id))
        except:
            pass

        # Auto-delete when empty
        async def delete_vc_when_empty():
            while len(new_vc.members) > 0:
                await asyncio.sleep(5)
            await asyncio.sleep(2)
            try:
                await new_vc.delete()
                if text_ch:
                    await text_ch.delete()
            except:
                pass

        asyncio.create_task(delete_vc_when_empty())
    except Exception as e:
        logger.exception("Failed to create personal VC: %s", e)


async def create_ticket(interaction: discord.Interaction, ticket_type: str, is_staff_apply: bool = False, cog=None) -> None:
    await interaction.response.defer()
    guild = interaction.guild
    user = interaction.user

    # Check if applications are open for staff applications
    if is_staff_apply and cog:
        guild_id = str(guild.id)
        if guild_id not in cog.applications or cog.applications[guild_id].get("status") != "open":
            await interaction.followup.send("❌ Staff applications are currently closed!", ephemeral=True)
            return

    overwrites = {
        guild.default_role: discord.PermissionOverwrite(view_channel=False),
        user: discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True),
        guild.me: discord.PermissionOverwrite(view_channel=True, send_messages=True, manage_channels=True)
    }

    # Add admin roles and the support role to overwrites
    support_role = guild.get_role(1441334848878022656)
    if support_role:
        overwrites[support_role] = discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True)

    for role in guild.roles:
        if role.permissions.administrator:
            overwrites[role] = discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True)

    try:
        channel = await guild.create_text_channel(
            name=f"ticket-{user.name[:10]}",
            overwrites=overwrites,
            reason=f"Support ticket by {user} ({ticket_type})"
        )

        embed = discord.Embed(
            title=f"🎫 {ticket_type} Ticket",
            description=f"Ticket created by {user.mention}",
            color=discord.Color.green()
        )
        embed.add_field(name="Type", value=ticket_type, inline=True)
        embed.add_field(name="User", value=user.mention, inline=True)
        embed.set_footer(text=f"Only you and admins can see this channel")

        await channel.send(embed=embed)

        # If staff application, ask questions
        if is_staff_apply:
            questions = [
                "**Q1:** What is your Discord username + tag?",
                "**Q2:** What is your age?",
                "**Q3:** What country/timezone are you in?",
                "**Q4:** How active are you daily? (Hours)",
                "**Q5:** How long have you been in the server?",
                "**Q6:** Why do you want to become staff here?",
                "**Q7:** What do you like the most about our server?",
                "**Q8:** Do you have any previous moderation experience? If yes, explain.",
                "**Q9:** What skills or strengths make you a good staff member?",
                "**Q10:** What are your weaknesses as a staff member? (good question to see honesty)",
                "**Q11:** A member is spamming but not breaking major rules — what do you do?",
                "**Q12:** Two members start arguing and it becomes toxic — how do you handle it?",
                "**Q13:** Your friend breaks a rule — what would you do?",
                "**Q14:** A user reports harassment but provides no evidence — what's your action?",
                "**Q15:** Someone accuses staff of abuse — how do you handle the situation?",
                "**Q16:** How do you react to pressure or stressful situations?",
                "**Q17:** How would you describe your moderation style? (strict, calm, neutral, etc.)",
                "**Q18:** Are you able to work as a team and accept feedback/criticism?",
                "**Q19:** What motivates you to stay active as staff?",
                "**Q20:** Can you stay unbiased, even with friends or drama?",
                "**Q21:** How long do you plan to stay as a staff member?",
                "**Q22:** Are you willing to learn the rules and follow all staff guidelines?",
                "**Q23:** Why should we pick YOU over other applicants? (This reveals confidence and personality)"
            ]

            for q in questions:
                await channel.send(q)

            # Ping bot maker and owner
            owner_id = 1436351143516311622
            gto_id = 1177669170981253132
            await channel.send(f"<@{owner_id}> <@{gto_id}> - New staff application!")
        else:
            # For other ticket types, ping support role
            support_role = guild.get_role(1441334848878022656)
            if support_role:
                await channel.send(f"{support_role.mention} - New {ticket_type.lower()} ticket created!")

        await interaction.followup.send(f"✅ Ticket created in {channel.mention}")
    except Exception as e:
        logger.exception("Failed to create ticket: %s", e)
        await interaction.followup.send(f"❌ Failed to create ticket: {e}")


# ---------- Cog registration ----------
@bot.event
async def setup_hook():
    # Register persistent views with bot instance
    bot.add_view(TicketView(bot_instance=bot))

    # Register cog and sync commands globally or to a guild
    await bot.add_cog(Cabbit(bot))
    if TEST_GUILD_ID:
        bot.tree.copy_global_to(guild=discord.Object(id=int(TEST_GUILD_ID)))
        await bot.tree.sync(guild=discord.Object(id=int(TEST_GUILD_ID)))
    else:
        await bot.tree.sync()


@bot.event
async def on_ready(*args, **kwargs):
    GUILD_ID = 1441157308456632401
    guild = discord.Object(id=GUILD_ID)
    await bot.tree.sync(guild=guild)
    logger.info(f"✅ Logged in as {bot.user} (ID: {bot.user.id})")
    await bot.change_presence(activity=discord.Activity(type=discord.ActivityType.watching, name="over the server"))


@bot.event
async def on_member_join(member: discord.Member):
    # Send welcome message
    try:
        cog = bot.get_cog("Cabbit")
        guild_id = str(member.guild.id)
        if guild_id in cog.welcome_channels:
            ch = member.guild.get_channel(int(cog.welcome_channels[guild_id]))
            if ch:
                welcome_msg = f"🎉 Welcome {member.mention} to {member.guild.name}! Happy to have you here."
                embed = discord.Embed(title="Welcome!", description=welcome_msg, color=discord.Color.green())
                embed.set_thumbnail(url=member.display_avatar.url)
                await ch.send(embed=embed)
    except Exception as e:
        logger.exception("Welcome message failed: %s", e)


@bot.event
async def on_voice_state_update(member: discord.Member, before: discord.VoiceState, after: discord.VoiceState):
    # Create personal VC when user joins a specific channel
    try:
        if after.channel:
            cog = bot.get_cog("Cabbit")
            guild_id = str(member.guild.id)

            # Check if user joined the interface channel
            if guild_id in cog.vc_interface:
                interface_ch_id = int(cog.vc_interface[guild_id])
                if after.channel.id == interface_ch_id:
                    await create_personal_vc(member, member.guild, cog)
    except Exception as e:
        logger.exception("Personal VC creation failed: %s", e)


if __name__ == "__main__":
    if not DISCORD_TOKEN:
        logger.error("❌ DISCORD_TOKEN environment variable is not set!")
        exit(1)

    try:
        bot.run(DISCORD_TOKEN)
    except KeyboardInterrupt:
        logger.info("Bot stopped by user")
    except Exception as e:
        logger.exception("Bot crashed: %s", e)