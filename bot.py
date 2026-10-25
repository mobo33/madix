import os

import discord
from aiohttp import web
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv

load_dotenv()

PANEL_CHANNEL_ID = 1563218768534970469

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
    print(f"Web server listening on port {port}")


class RoleBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        intents.message_content = True  # needed to read "!rolepanel"
        super().__init__(command_prefix="!", intents=intents, help_command=None)
        self.synced = False

    async def setup_hook(self):
        self.add_view(RolePanel())
        await start_web_server()

    async def sync_commands(self, guild: discord.Guild):
        # Syncing per guild makes the command show up instantly
        self.tree.copy_global_to(guild=guild)
        try:
            await self.tree.sync(guild=guild)
        except discord.Forbidden:
            print(
                f"Can't register commands in {guild.name}. Re-invite the bot "
                "with the applications.commands scope."
            )

    async def on_ready(self):
        print(f"Logged in as {self.user} ({self.user.id})")
        if self.synced:
            return
        self.synced = True
        for guild in self.guilds:
            await self.sync_commands(guild)
        print("Commands ready. Type !rolepanel (or /panel) in your server.")

    async def on_guild_join(self, guild: discord.Guild):
        await self.sync_commands(guild)


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


@client.tree.command(name="panel", description="Post the Claim ur role panel")
@app_commands.default_permissions(administrator=True)
@app_commands.guild_only()
async def panel(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    await interaction.followup.send(await post_panel(), ephemeral=True)


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
