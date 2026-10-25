import os

import discord
from aiohttp import web
from discord.ext import commands
from dotenv import load_dotenv

load_dotenv()

PANEL_CHANNEL_ID = 1563218768534970469

# Trap channel: anyone without the admin role who writes here gets banned.
TRAP_CHANNEL_ID = 1563707951456198686
ADMIN_ROLE_ID = 1556695504035774605
BAN_DELETE_SECONDS = 3600  # also remove the last hour of the banned user's messages

# Join to create: joining this voice channel makes a room for the member and moves
# them in. Rooms are named "<TEMP_PREFIX><member>" and deleted when empty.
JOIN_TO_CREATE_ID = 1563706992760066118
TEMP_PREFIX = "🔊 "
# What the owner can do in their own room (only what the bot itself has is granted)
OWNER_PERMISSIONS = (
    "view_channel",
    "connect",
    "speak",
    "stream",
    "use_voice_activation",
    "priority_speaker",
    "create_instant_invite",
    "manage_channels",
    "manage_permissions",
    "mute_members",
    "deafen_members",
    "move_members",
)
# What @everyone can always do in a room, whatever the join channel or category says
EVERYONE_PERMISSIONS = ("view_channel", "connect", "speak")

# (label, emoji, role id)
GAME_ROLES = [
    ("Valorant", "🎯", 1563709078935437372),
    ("Counter Strike 2", "🔫", 1563709254379241502),
    ("Minecraft", "⛏️", 1563709420263972944),
    ("FIFA", "⚽", 1563709555148329060),
    ("FiveM", "🚓", 1563709914835058868),
]

PANEL_TITLE = "🎮 Claim ur role"
PANEL_TEXT = (
    "Click a button to get the role of your game.\n"
    "Click it again to remove the role."
)


class RoleButton(discord.ui.Button):
    def __init__(self, label: str, emoji: str, role_id: int):
        super().__init__(
            label=label,
            emoji=emoji,
            style=discord.ButtonStyle.secondary,
            custom_id=f"claim_role:{role_id}",
        )
        self.role_id = role_id

    async def callback(self, interaction: discord.Interaction):
        role = interaction.guild.get_role(self.role_id)
        if role is None:
            await interaction.response.send_message(
                "This role doesn't exist anymore, tell an admin.", ephemeral=True
            )
            return

        member = interaction.user
        try:
            if role in member.roles:
                await member.remove_roles(role, reason="Claim ur role panel")
                text = f"❌ **{role.name}** removed."
            else:
                await member.add_roles(role, reason="Claim ur role panel")
                text = f"✅ You got **{role.name}**."
        except discord.Forbidden:
            text = (
                "⚠️ I can't give this role. Tell an admin to give the bot "
                "**Manage Roles** and move its role above the game roles."
            )
        await interaction.response.send_message(text, ephemeral=True)


class RolePanel(discord.ui.View):
    def __init__(self):
        # timeout=None + custom_id on every button = buttons survive restarts
        super().__init__(timeout=None)
        for label, emoji, role_id in GAME_ROLES:
            self.add_item(RoleButton(label, emoji, role_id))


async def start_web_server():
    # Render Web Services need an open HTTP port (and a pinger keeps them awake).
    # Render Background Workers and local runs have no PORT, so nothing starts.
    port = os.getenv("PORT")
    if not port:
        return

    async def health(request: web.Request) -> web.Response:
        return web.Response(text="ok")

    app = web.Application()
    app.router.add_get("/", health)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", int(port)).start()
    print(f"Web server listening on port {port}", flush=True)


class RoleBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        intents.message_content = True  # needed to read "!rolepanel"
        super().__init__(command_prefix="!", intents=intents, help_command=None)
        self.cleaned_up = False
        self.temp_channels: set[int] = set()  # rooms the bot made and still tracks
        self.creating: set[int] = set()  # members whose room is being made right now

    async def setup_hook(self):
        self.add_view(RolePanel())
        await start_web_server()

    async def clear_slash_commands(self, guild: discord.Guild):
        # /panel was removed: delete any copy of it still registered in the server
        self.tree.clear_commands(guild=guild)
        try:
            await self.tree.sync(guild=guild)
        except discord.HTTPException as exc:
            print(f"Could not clear slash commands in {guild.name}: {exc}", flush=True)

    async def on_ready(self):
        print(f"Logged in as {self.user} ({self.user.id})", flush=True)
        if self.cleaned_up:
            return
        self.cleaned_up = True
        for guild in self.guilds:
            await self.clear_slash_commands(guild)
            await self.adopt_temp_channels(guild)
        print("Ready. Type !rolepanel in your server.", flush=True)

    async def on_message(self, message: discord.Message):
        if (
            message.channel.id == TRAP_CHANNEL_ID
            and not message.author.bot
            and not is_exempt(message.author)
        ):
            await ban_writer(message)
            return
        await self.process_commands(message)

    async def adopt_temp_channels(self, guild: discord.Guild):
        # After a restart: delete empty leftover rooms, keep tracking the busy ones
        join = guild.get_channel(JOIN_TO_CREATE_ID)
        if join is None or join.category is None:
            return
        for channel in join.category.voice_channels:
            if channel.id == JOIN_TO_CREATE_ID or not channel.name.startswith(TEMP_PREFIX):
                continue
            if channel.members:
                self.temp_channels.add(channel.id)
            else:
                await delete_temp_room(channel)

    async def on_voice_state_update(
        self,
        member: discord.Member,
        before: discord.VoiceState,
        after: discord.VoiceState,
    ):
        if after.channel is not None and after.channel.id == JOIN_TO_CREATE_ID:
            await create_temp_room(member, after.channel)

        left = before.channel
        if left is not None and left.id in self.temp_channels and not left.members:
            await delete_temp_room(left)


client = RoleBot()


async def post_panel() -> str:
    try:
        channel = client.get_channel(PANEL_CHANNEL_ID) or await client.fetch_channel(
            PANEL_CHANNEL_ID
        )
    except discord.NotFound:
        return f"❌ Channel {PANEL_CHANNEL_ID} was not found."
    except discord.Forbidden:
        return f"❌ The bot has no access to channel {PANEL_CHANNEL_ID}."

    embed = discord.Embed(
        title=PANEL_TITLE, description=PANEL_TEXT, color=discord.Color.blurple()
    )

    try:
        # Reuse the panel already in the channel instead of posting a duplicate
        async for message in channel.history(limit=50):
            if (
                message.author.id == client.user.id
                and message.embeds
                and message.embeds[0].title == PANEL_TITLE
            ):
                await message.edit(embed=embed, view=RolePanel())
                return f"✅ Panel updated in {channel.mention}."

        await channel.send(embed=embed, view=RolePanel())
        return f"✅ Panel posted in {channel.mention}."
    except discord.Forbidden:
        return (
            f"❌ The bot needs View Channel, Send Messages, Embed Links and "
            f"Read Message History in {channel.mention}."
        )


def is_exempt(user: discord.abc.User) -> bool:
    # Admins and the server owner may write in the trap channel
    return isinstance(user, discord.Member) and (
        user.id == user.guild.owner_id
        or any(role.id == ADMIN_ROLE_ID for role in user.roles)
    )


async def ban_writer(message: discord.Message):
    author = message.author
    try:
        await message.guild.ban(
            author,
            reason="Wrote in the trap channel",
            delete_message_seconds=BAN_DELETE_SECONDS,
        )
    except discord.HTTPException as exc:
        # Usually Ban Members is missing or the member's top role is above the bot's
        print(f"Could not ban {author} ({author.id}): {exc}", flush=True)
        try:
            await message.delete()
        except discord.HTTPException:
            pass
        return
    print(f"Banned {author} ({author.id}) for writing in the trap channel.", flush=True)


async def delete_temp_room(channel: discord.VoiceChannel):
    client.temp_channels.discard(channel.id)
    try:
        await channel.delete(reason="Temp voice room is empty")
    except discord.HTTPException as exc:
        print(f"Could not delete room {channel.name}: {exc}", flush=True)


async def create_temp_room(member: discord.Member, join: discord.VoiceChannel):
    if member.id in client.creating:
        return
    client.creating.add(member.id)
    try:
        # Copy the join channel's own permissions so every room looks like it, and give
        # the owner full control. Discord refuses permissions the bot does not hold,
        # so the owner only gets what the bot itself has.
        bot_perms = join.permissions_for(member.guild.me)
        overwrites = dict(join.overwrites)

        # Everyone can see, join and talk in every room
        everyone = member.guild.default_role
        template = overwrites.get(everyone)
        open_to_all = (
            discord.PermissionOverwrite.from_pair(*template.pair())
            if template is not None
            else discord.PermissionOverwrite()
        )
        open_to_all.update(
            **{name: True for name in EVERYONE_PERMISSIONS if getattr(bot_perms, name)}
        )
        overwrites[everyone] = open_to_all

        overwrites[member] = discord.PermissionOverwrite(
            **{name: True for name in OWNER_PERMISSIONS if getattr(bot_perms, name)}
        )
        room = await member.guild.create_voice_channel(
            f"{TEMP_PREFIX}{member.display_name}"[:100],
            category=join.category,
            overwrites=overwrites,
            reason=f"Join to create for {member}",
        )
        client.temp_channels.add(room.id)
        try:
            await member.move_to(room)
        except discord.HTTPException as exc:
            # They left already, or the bot is missing Move Members
            print(f"Could not move {member} into their room: {exc}", flush=True)
            await delete_temp_room(room)
    except discord.HTTPException as exc:
        print(
            f"Could not create a room for {member}: {exc}. The bot needs "
            "Manage Channels, Connect and Move Members.",
            flush=True,
        )
    finally:
        client.creating.discard(member.id)


@client.command(name="rolepanel")
@commands.guild_only()
@commands.has_permissions(administrator=True)
async def rolepanel(ctx: commands.Context):
    result = await post_panel()
    try:
        await ctx.message.delete()  # keep the channel clean (needs Manage Messages)
    except discord.HTTPException:
        pass
    await ctx.send(result, delete_after=8)


@client.event
async def on_command_error(ctx: commands.Context, error: commands.CommandError):
    # Ignore random "!something" and non-admins silently
    if isinstance(
        error,
        (commands.CommandNotFound, commands.MissingPermissions, commands.NoPrivateMessage),
    ):
        return
    print(f"Command error in !{ctx.command}: {error}")


if __name__ == "__main__":
    token = os.getenv("DISCORD_TOKEN")
    if not token:
        raise SystemExit("DISCORD_TOKEN is missing. Put it in the .env file.")
    try:
        client.run(token)
    except discord.PrivilegedIntentsRequired:
        raise SystemExit(
            "Message Content Intent is off. Developer Portal > Bot > Privileged "
            "Gateway Intents > enable MESSAGE CONTENT INTENT, save, then restart."
        )
