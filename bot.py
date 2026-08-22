#!/usr/bin/env python3
"""Ein kleiner Discord-Bot mit nützlichen Startbefehlen."""

import logging
import json
import os
import re
import asyncio
import random
from datetime import timedelta
from pathlib import Path
from typing import Any

import discord
from discord import app_commands
from discord.ext import commands


CONFIG_PATH = Path("welcome_config.json")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
logger = logging.getLogger("discord-bot")


def load_welcome_config() -> dict[str, dict[str, Any]]:
    """Lädt die Server- und Ticket-Konfiguration aus einer lokalen JSON-Datei."""
    if not CONFIG_PATH.exists():
        return {}
    try:
        data: Any = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        logger.exception("Willkommens-Konfiguration konnte nicht geladen werden")
        return {}


def save_welcome_config(config: dict[str, dict[str, Any]]) -> None:
    """Speichert die Server-Konfiguration sicher und formatiert."""
    CONFIG_PATH.write_text(
        json.dumps(config, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


def render_welcome_message(message: str, member: discord.Member) -> str:
    """Ersetzt bekannte Platzhalter, ohne bei normalen geschweiften Klammern zu scheitern."""
    try:
        rendered = message.format(
            member=member.mention,
            username=member.display_name,
            server=member.guild.name,
            count=member.guild.member_count,
        )
    except (KeyError, ValueError):
        rendered = message
    return rendered[:4096]


def safe_ticket_name(member: discord.Member) -> str:
    """Erzeugt einen Discord-tauglichen Kanalnamen."""
    username = re.sub(r"[^a-z0-9-]+", "-", member.display_name.lower()).strip("-")
    return f"ticket-{username or member.id}"[:90]


def create_bot() -> commands.Bot:
    """Erstellt den Bot und registriert seine Befehle."""
    intents = discord.Intents.default()
    intents.message_content = True
    intents.members = True

    # Keine Textpräfixe: alle Befehle werden ausschließlich über Discord-Slash-Commands ausgeführt.
    bot = commands.Bot(command_prefix=(), intents=intents)
    welcome_config = load_welcome_config()
    views_registered = False
    commands_synced = False

    async def send_log(guild: discord.Guild, title: str, description: str) -> None:
        """Sendet ein Ereignis in den konfigurierten Log-Kanal."""
        channel_id = welcome_config.get(str(guild.id), {}).get("log_channel_id")
        channel = guild.get_channel(channel_id) if channel_id else None
        if isinstance(channel, discord.TextChannel):
            embed = discord.Embed(
                title=title,
                description=description[:4096],
                color=discord.Color.dark_grey(),
                timestamp=discord.utils.utcnow(),
            )
            try:
                await channel.send(embed=embed)
            except discord.Forbidden:
                logger.warning("Keine Berechtigung im Log-Kanal zu schreiben")

    async def finish_giveaway(
        message: discord.Message,
        duration: int,
        prize: str,
    ) -> None:
        """Wählt nach Ablauf eines Giveaways einen Gewinner."""
        await asyncio.sleep(duration)
        try:
            message = await message.channel.fetch_message(message.id)
            reaction = discord.utils.get(message.reactions, emoji="🎉")
            if reaction is None:
                await message.channel.send(
                    "Das Giveaway ist beendet – niemand hat teilgenommen."
                )
                return
            users = [user async for user in reaction.users() if not user.bot]
            if not users:
                await message.channel.send(
                    "Das Giveaway ist beendet – niemand hat teilgenommen."
                )
                return
            winner = random.choice(users)
            await message.channel.send(
                f"Glückwunsch {winner.mention}! Du gewinnst **{prize}**."
            )
            if message.embeds:
                ended_embed = message.embeds[0].copy()
                ended_embed.title = "Giveaway beendet"
                ended_embed.color = discord.Color.dark_grey()
                await message.edit(embed=ended_embed)
        except (discord.NotFound, discord.Forbidden):
            logger.warning("Giveaway konnte nicht beendet werden")

    class TicketCloseView(discord.ui.View):
        """Persistenter Button zum Schließen eines Tickets."""

        def __init__(self) -> None:
            super().__init__(timeout=None)

        @discord.ui.button(
            label="Ticket schließen",
            style=discord.ButtonStyle.danger,
            emoji="🔒",
            custom_id="ticket:close",
        )
        async def close_ticket(
            self,
            interaction: discord.Interaction,
            button: discord.ui.Button[discord.ui.View],
        ) -> None:
            channel = interaction.channel
            if not isinstance(channel, discord.TextChannel):
                await interaction.response.send_message(
                    "Das funktioniert nur in einem Ticketkanal.",
                    ephemeral=True,
                )
                return

            owner_id = None
            if channel.topic and channel.topic.startswith("ticket-owner:"):
                owner_id = channel.topic.removeprefix("ticket-owner:")

            guild_config = welcome_config.get(str(channel.guild.id), {})
            support_role_id = guild_config.get("ticket_support_role_id")
            is_support = (
                isinstance(interaction.user, discord.Member)
                and support_role_id is not None
                and any(role.id == support_role_id for role in interaction.user.roles)
            )
            is_owner = str(interaction.user.id) == owner_id
            has_permission = isinstance(interaction.user, discord.Member) and (
                interaction.user.guild_permissions.manage_channels
                or interaction.user.guild_permissions.manage_guild
            )
            if not is_owner and not is_support and not has_permission:
                await interaction.response.send_message(
                    "Nur der Ersteller oder ein Server-Administrator kann dieses Ticket schließen.",
                    ephemeral=True,
                )
                return

            await interaction.response.send_message(
                "Das Ticket wird in Kürze geschlossen.",
                ephemeral=True,
            )
            await channel.delete(reason=f"Ticket geschlossen von {interaction.user}")

    class TicketPanelView(discord.ui.View):
        """Persistenter Button zum Erstellen eines Tickets."""

        def __init__(self) -> None:
            super().__init__(timeout=None)

        @discord.ui.button(
            label="Ticket erstellen",
            style=discord.ButtonStyle.primary,
            emoji="🎫",
            custom_id="ticket:create",
        )
        async def create_ticket(
            self,
            interaction: discord.Interaction,
            button: discord.ui.Button[discord.ui.View],
        ) -> None:
            if not isinstance(interaction.guild, discord.Guild) or not isinstance(
                interaction.user, discord.Member
            ):
                await interaction.response.send_message(
                    "Tickets können nur auf einem Server erstellt werden.",
                    ephemeral=True,
                )
                return

            guild = interaction.guild
            guild_config = welcome_config.get(str(guild.id), {})
            existing = next(
                (
                    channel
                    for channel in guild.text_channels
                    if channel.topic == f"ticket-owner:{interaction.user.id}"
                ),
                None,
            )
            if existing is not None:
                await interaction.response.send_message(
                    f"Du hast bereits ein offenes Ticket: {existing.mention}",
                    ephemeral=True,
                )
                return

            overwrites: dict[discord.abc.Snowflake, discord.PermissionOverwrite] = {
                guild.default_role: discord.PermissionOverwrite(view_channel=False),
                interaction.user: discord.PermissionOverwrite(
                    view_channel=True,
                    send_messages=True,
                    read_message_history=True,
                    attach_files=True,
                ),
            }
            if bot.user is not None:
                overwrites[bot.user] = discord.PermissionOverwrite(
                    view_channel=True,
                    send_messages=True,
                    manage_channels=True,
                    read_message_history=True,
                )

            support_role_id = guild_config.get("ticket_support_role_id")
            support_role = guild.get_role(support_role_id) if support_role_id else None
            if support_role is not None:
                overwrites[support_role] = discord.PermissionOverwrite(
                    view_channel=True,
                    send_messages=True,
                    read_message_history=True,
                    attach_files=True,
                )

            category_id = guild_config.get("ticket_category_id")
            category = guild.get_channel(category_id) if category_id else None
            if not isinstance(category, discord.CategoryChannel):
                category = None

            try:
                ticket_channel = await guild.create_text_channel(
                    safe_ticket_name(interaction.user),
                    category=category,
                    overwrites=overwrites,
                    topic=f"ticket-owner:{interaction.user.id}",
                    reason="Neues Support-Ticket",
                )
            except discord.Forbidden:
                await interaction.response.send_message(
                    "Ich kann keinen Ticketkanal erstellen. Prüfe meine Berechtigungen.",
                    ephemeral=True,
                )
                return

            embed = discord.Embed(
                title="Support-Ticket",
                description=(
                    f"Hallo {interaction.user.mention}, dein Ticket wurde erstellt.\n"
                    "Beschreibe hier bitte dein Anliegen. Das Support-Team meldet sich so schnell wie möglich."
                ),
                color=discord.Color.blurple(),
            )
            embed.set_footer(text="Bitte keine sensiblen Daten teilen.")
            await ticket_channel.send(
                content=(
                    f"{interaction.user.mention}"
                    + (f" {support_role.mention}" if support_role else "")
                ),
                embed=embed,
                view=TicketCloseView(),
                allowed_mentions=discord.AllowedMentions(
                    users=True,
                    roles=bool(support_role),
                    everyone=False,
                ),
            )
            await interaction.response.send_message(
                f"Dein Ticket wurde erstellt: {ticket_channel.mention}",
                ephemeral=True,
            )

    @bot.event
    async def on_ready() -> None:
        nonlocal views_registered, commands_synced
        if not views_registered:
            bot.add_view(TicketPanelView())
            bot.add_view(TicketCloseView())
            views_registered = True
        if not commands_synced:
            synced_commands = await bot.tree.sync()
            commands_synced = True
            logger.info("%d Slash-Commands synchronisiert", len(synced_commands))
        if bot.user is not None:
            logger.info("Eingeloggt als %s (ID: %s)", bot.user, bot.user.id)

    @bot.event
    async def on_member_join(member: discord.Member) -> None:
        """Begrüßt neue Mitglieder und weist ihnen die Standardrolle zu."""
        guild_config = welcome_config.get(str(member.guild.id), {})
        channel_id = guild_config.get("channel_id")
        role_id = guild_config.get("role_id")
        welcome_message = guild_config.get("message")

        if role_id:
            role = member.guild.get_role(role_id)
            if role is not None:
                try:
                    await member.add_roles(role, reason="Automatische Standardrolle")
                except discord.Forbidden:
                    logger.warning(
                        "Keine Berechtigung, %s die Rolle %s zuzuweisen",
                        member,
                        role.name,
                    )
            else:
                logger.warning(
                    "Konfigurierte Standardrolle %s wurde nicht gefunden", role_id
                )

        if not channel_id:
            return

        channel = member.guild.get_channel(channel_id)
        if not isinstance(channel, discord.TextChannel):
            logger.warning("Willkommenskanal %s wurde nicht gefunden", channel_id)
            return

        embed = discord.Embed(
            title="Willkommen auf dem Server!",
            description=(
                render_welcome_message(welcome_message, member)
                if welcome_message
                else (
                    f"Schön, dass du da bist, {member.mention}!\n\n"
                    "Schau dich gerne um und lerne die Community kennen."
                )
            ),
            color=discord.Color.blurple(),
        )
        embed.set_footer(text=f"Mitglied #{member.guild.member_count}")
        if member.display_avatar:
            embed.set_thumbnail(url=member.display_avatar.url)

        try:
            await channel.send(
                embed=embed,
                allowed_mentions=discord.AllowedMentions(
                    users=True,
                    roles=False,
                    everyone=False,
                ),
            )
        except discord.Forbidden:
            logger.warning("Keine Berechtigung, im Willkommenskanal zu schreiben")

    @bot.event
    async def on_member_remove(member: discord.Member) -> None:
        await send_log(
            member.guild,
            "Mitglied verlassen",
            f"**{member}** (`{member.id}`) hat den Server verlassen.",
        )

    @bot.event
    async def on_message_delete(message: discord.Message) -> None:
        if message.guild is not None and message.author != bot.user:
            await send_log(
                message.guild,
                "Nachricht gelöscht",
                f"Autor: **{message.author}**\nKanal: {message.channel.mention}\n"
                f"Inhalt: {message.content or '[kein Text]'}",
            )

    @bot.event
    async def on_message_edit(
        before: discord.Message,
        after: discord.Message,
    ) -> None:
        if before.guild is not None and before.content != after.content:
            await send_log(
                before.guild,
                "Nachricht bearbeitet",
                f"Autor: **{before.author}**\nKanal: {before.channel.mention}\n"
                f"Vorher: {before.content or '[kein Text]'}\n"
                f"Nachher: {after.content or '[kein Text]'}",
            )

    @bot.event
    async def on_command_error(
        ctx: commands.Context[commands.Bot],
        error: commands.CommandError,
    ) -> None:
        if isinstance(error, commands.CommandNotFound):
            return
        if isinstance(error, commands.MissingPermissions):
            await ctx.send("Dafür fehlen dir die nötigen Berechtigungen.")
            return
        if isinstance(error, commands.MissingRequiredArgument):
            await ctx.send(
                "Dafür fehlt noch ein Argument. Nutze `!hilfe` für Beispiele."
            )
            return
        if isinstance(error, commands.BadArgument):
            await ctx.send(
                "Ich konnte die Angabe nicht finden. Erwähne Kanal, Rolle oder Kategorie direkt."
            )
            return
        logger.exception("Fehler beim Ausführen eines Befehls", exc_info=error)
        await ctx.send("Beim Ausführen des Befehls ist ein Fehler aufgetreten.")

    @bot.hybrid_command(name="ping")
    async def ping(ctx: commands.Context[commands.Bot]) -> None:
        """Antwortet mit der aktuellen Latenz."""
        latency_ms = round(bot.latency * 1000)
        await ctx.send(f"Pong! Latenz: {latency_ms} ms")

    @bot.hybrid_command(name="hallo")
    async def hello(ctx: commands.Context[commands.Bot]) -> None:
        """Begrüßt den Nutzer."""
        await ctx.send(f"Hallo {ctx.author.mention}!")

    @bot.hybrid_command(name="welcomekanal")
    @app_commands.describe(
        channel="Der Textkanal, in dem neue Mitglieder begrüßt werden"
    )
    @commands.guild_only()
    @commands.has_guild_permissions(manage_guild=True)
    async def set_welcome_channel(
        ctx: commands.Context[commands.Bot],
        channel: discord.TextChannel,
    ) -> None:
        """Setzt den Kanal für automatische Begrüßungen."""
        guild_id = str(ctx.guild.id)
        welcome_config.setdefault(guild_id, {})["channel_id"] = channel.id
        save_welcome_config(welcome_config)
        await ctx.send(
            f"Willkommensnachrichten werden künftig in {channel.mention} gesendet."
        )

    @bot.hybrid_command(name="standardrolle")
    @app_commands.describe(role="Die Rolle, die neue Mitglieder automatisch erhalten")
    @commands.guild_only()
    @commands.has_guild_permissions(manage_guild=True)
    async def set_default_role(
        ctx: commands.Context[commands.Bot],
        role: discord.Role,
    ) -> None:
        """Setzt die Rolle für neue Mitglieder."""
        guild_id = str(ctx.guild.id)
        welcome_config.setdefault(guild_id, {})["role_id"] = role.id
        save_welcome_config(welcome_config)
        await ctx.send(
            f"Neue Mitglieder erhalten automatisch die Rolle **{role.name}**."
        )

    @bot.hybrid_command(name="willkommen")
    @app_commands.describe(
        message="Dein Begrüßungstext. Platzhalter: {member}, {username}, {server}, {count}"
    )
    @commands.guild_only()
    @commands.has_guild_permissions(manage_guild=True)
    async def set_welcome_message(
        ctx: commands.Context[commands.Bot],
        *,
        message: str,
    ) -> None:
        """Setzt den frei formulierbaren Begrüßungstext."""
        guild_id = str(ctx.guild.id)
        if message.lower() == "zurücksetzen":
            welcome_config.setdefault(guild_id, {}).pop("message", None)
            save_welcome_config(welcome_config)
            await ctx.send("Der Standard-Begrüßungstext ist wieder aktiv.")
            return

        welcome_config.setdefault(guild_id, {})["message"] = message
        save_welcome_config(welcome_config)
        await ctx.send("Dein persönlicher Begrüßungstext wurde gespeichert.")

    @bot.hybrid_command(name="serverinfo")
    @commands.guild_only()
    async def server_info(ctx: commands.Context[commands.Bot]) -> None:
        """Zeigt kompakte Informationen über den Server."""
        guild = ctx.guild
        embed = discord.Embed(title=guild.name, color=discord.Color.blurple())
        embed.add_field(name="Besitzer", value=f"<@{guild.owner_id}>")
        embed.add_field(name="Mitglieder", value=str(guild.member_count))
        embed.add_field(name="Kanäle", value=str(len(guild.channels)))
        embed.add_field(name="Rollen", value=str(len(guild.roles)))
        embed.add_field(
            name="Erstellt", value=discord.utils.format_dt(guild.created_at, "D")
        )
        if guild.icon:
            embed.set_thumbnail(url=guild.icon.url)
        await ctx.send(embed=embed)

    @bot.hybrid_command(name="userinfo")
    @app_commands.describe(
        member="Das Mitglied, über das du Informationen sehen möchtest"
    )
    @commands.guild_only()
    async def user_info(
        ctx: commands.Context[commands.Bot],
        member: discord.Member | None = None,
    ) -> None:
        """Zeigt Informationen über ein Mitglied."""
        target = member or ctx.author
        embed = discord.Embed(title=str(target), color=target.color)
        embed.add_field(name="ID", value=str(target.id))
        embed.add_field(
            name="Beigetreten", value=discord.utils.format_dt(target.joined_at, "D")
        )
        embed.add_field(
            name="Account erstellt",
            value=discord.utils.format_dt(target.created_at, "D"),
        )
        embed.add_field(
            name="Rollen",
            value=", ".join(role.mention for role in target.roles[1:]) or "Keine",
        )
        embed.set_thumbnail(url=target.display_avatar.url)
        await ctx.send(embed=embed)

    @bot.hybrid_command(name="avatar")
    @app_commands.describe(member="Das Mitglied, dessen Avatar du sehen möchtest")
    async def avatar(
        ctx: commands.Context[commands.Bot], member: discord.Member | None = None
    ) -> None:
        """Zeigt den Avatar eines Mitglieds."""
        target = member or ctx.author
        embed = discord.Embed(
            title=f"Avatar von {target.display_name}", color=discord.Color.blurple()
        )
        embed.set_image(url=target.display_avatar.url)
        await ctx.send(embed=embed)

    @bot.hybrid_command(name="membercount")
    @commands.guild_only()
    async def member_count(ctx: commands.Context[commands.Bot]) -> None:
        """Zeigt die aktuelle Mitgliederzahl."""
        await ctx.send(
            f"Dieser Server hat aktuell **{ctx.guild.member_count} Mitglieder**."
        )

    @bot.hybrid_command(name="channelinfo")
    @app_commands.describe(
        channel="Der Textkanal, über den du Informationen sehen möchtest"
    )
    @commands.guild_only()
    async def channel_info(
        ctx: commands.Context[commands.Bot],
        channel: discord.TextChannel | None = None,
    ) -> None:
        """Zeigt Informationen über einen Textkanal."""
        target = channel or ctx.channel
        await ctx.send(
            f"**{target.name}**\nID: `{target.id}`\n"
            f"Erstellt: {discord.utils.format_dt(target.created_at, 'D')}\n"
            f"Kategorie: {target.category.name if target.category else 'Keine'}"
        )

    @bot.hybrid_command(name="roleinfo")
    @app_commands.describe(role="Die Rolle, über die du Informationen sehen möchtest")
    @commands.guild_only()
    async def role_info(
        ctx: commands.Context[commands.Bot], role: discord.Role
    ) -> None:
        """Zeigt Informationen über eine Rolle."""
        await ctx.send(
            f"**{role.name}**\nID: `{role.id}`\n"
            f"Mitglieder: **{len(role.members)}**\n"
            f"Farbe: `{role.color}`\n"
            f"Position: `{role.position}`"
        )

    @bot.hybrid_command(name="say")
    @app_commands.describe(message="Der Text, den der Bot senden soll")
    @commands.guild_only()
    @commands.has_guild_permissions(manage_messages=True)
    async def say(ctx: commands.Context[commands.Bot], *, message: str) -> None:
        """Sendet eine Nachricht als Bot."""
        if isinstance(ctx.channel, discord.TextChannel):
            await ctx.channel.send(message[:2000])
            await ctx.send("Nachricht gesendet.", ephemeral=True)

    @bot.hybrid_command(name="announce")
    @app_commands.describe(
        channel="Der Kanal, in dem die Ankündigung erscheinen soll",
        message="Der Inhalt deiner Ankündigung",
    )
    @commands.guild_only()
    @commands.has_guild_permissions(manage_guild=True)
    async def announce(
        ctx: commands.Context[commands.Bot],
        channel: discord.TextChannel,
        *,
        message: str,
    ) -> None:
        """Sendet eine formatierte Ankündigung."""
        embed = discord.Embed(
            title="Ankündigung", description=message[:4096], color=discord.Color.gold()
        )
        embed.set_footer(text=f"Angekündigt von {ctx.author.display_name}")
        await channel.send(embed=embed)
        await ctx.send(f"Ankündigung in {channel.mention} gesendet.", ephemeral=True)

    @bot.hybrid_command(name="poll")
    @app_commands.describe(question="Die Frage, über die die Community abstimmen soll")
    @commands.guild_only()
    async def poll(ctx: commands.Context[commands.Bot], *, question: str) -> None:
        """Erstellt eine Ja/Nein-Umfrage."""
        embed = discord.Embed(
            title="Umfrage", description=question[:4096], color=discord.Color.blurple()
        )
        embed.set_footer(text=f"Gestartet von {ctx.author.display_name}")
        message = await ctx.send(embed=embed)
        if isinstance(message, discord.Message):
            await message.add_reaction("✅")
            await message.add_reaction("❌")

    @bot.hybrid_command(name="giveaway")
    @app_commands.describe(
        duration="Dauer in Sekunden, mindestens 10 und höchstens 604800",
        prize="Der Preis, den die Gewinnerin oder der Gewinner bekommt",
    )
    @commands.guild_only()
    @commands.has_guild_permissions(manage_guild=True)
    async def giveaway(
        ctx: commands.Context[commands.Bot],
        duration: int,
        *,
        prize: str,
    ) -> None:
        """Startet ein Giveaway mit Reaktions-Teilnahme."""
        if duration < 10 or duration > 604800:
            await ctx.send("Die Dauer muss zwischen 10 und 604800 Sekunden liegen.")
            return
        embed = discord.Embed(
            title="Giveaway",
            description=f"**Gewinn:** {prize[:1000]}\n\n"
            f"Reagiere mit 🎉, um teilzunehmen.\n"
            f"Ende: {discord.utils.format_dt(discord.utils.utcnow() + timedelta(seconds=duration), 'R')}",
            color=discord.Color.green(),
        )
        embed.set_footer(text=f"Gestartet von {ctx.author.display_name}")
        message = await ctx.send(embed=embed)
        if isinstance(message, discord.Message):
            await message.add_reaction("🎉")
            asyncio.create_task(finish_giveaway(message, duration, prize))

    @bot.hybrid_command(name="clear")
    @app_commands.describe(
        amount="Anzahl der Nachrichten, die gelöscht werden sollen (1 bis 100)"
    )
    @commands.guild_only()
    @commands.has_guild_permissions(manage_messages=True)
    async def clear(ctx: commands.Context[commands.Bot], amount: int) -> None:
        """Löscht mehrere Nachrichten."""
        if not isinstance(ctx.channel, discord.TextChannel):
            return
        if amount < 1 or amount > 100:
            await ctx.send(
                "Du kannst zwischen 1 und 100 Nachrichten löschen.", ephemeral=True
            )
            return
        deleted = await ctx.channel.purge(limit=amount)
        await ctx.send(f"**{len(deleted)} Nachrichten** gelöscht.", ephemeral=True)

    @bot.hybrid_command(name="slowmode")
    @app_commands.describe(seconds="Wartezeit in Sekunden, 0 deaktiviert den Slowmode")
    @commands.guild_only()
    @commands.has_guild_permissions(manage_channels=True)
    async def slowmode(ctx: commands.Context[commands.Bot], seconds: int) -> None:
        """Setzt den Slowmode des aktuellen Kanals."""
        if not isinstance(ctx.channel, discord.TextChannel):
            return
        if seconds < 0 or seconds > 21600:
            await ctx.send("Der Slowmode muss zwischen 0 und 21600 Sekunden liegen.")
            return
        await ctx.channel.edit(slowmode_delay=seconds)
        await ctx.send(f"Slowmode auf **{seconds} Sekunden** gesetzt.", ephemeral=True)

    @bot.hybrid_command(name="lock")
    @commands.guild_only()
    @commands.has_guild_permissions(manage_channels=True)
    async def lock(ctx: commands.Context[commands.Bot]) -> None:
        """Sperrt den aktuellen Kanal für normale Mitglieder."""
        if isinstance(ctx.channel, discord.TextChannel):
            await ctx.channel.set_permissions(
                ctx.guild.default_role, send_messages=False
            )
            await ctx.send("🔒 Kanal gesperrt.", ephemeral=True)

    @bot.hybrid_command(name="unlock")
    @commands.guild_only()
    @commands.has_guild_permissions(manage_channels=True)
    async def unlock(ctx: commands.Context[commands.Bot]) -> None:
        """Entsperrt den aktuellen Kanal."""
        if isinstance(ctx.channel, discord.TextChannel):
            await ctx.channel.set_permissions(
                ctx.guild.default_role, send_messages=None
            )
            await ctx.send("🔓 Kanal wieder freigegeben.", ephemeral=True)

    @bot.hybrid_command(name="giverole")
    @app_commands.describe(
        member="Das Mitglied, das die Rolle erhalten soll",
        role="Die Rolle, die vergeben werden soll",
    )
    @commands.guild_only()
    @commands.has_guild_permissions(manage_roles=True)
    async def give_role(
        ctx: commands.Context[commands.Bot],
        member: discord.Member,
        role: discord.Role,
    ) -> None:
        """Weist einem Mitglied eine Rolle zu."""
        try:
            await member.add_roles(role, reason=f"Vergeben von {ctx.author}")
            await ctx.send(f"{role.mention} wurde {member.mention} gegeben.")
        except discord.Forbidden:
            await ctx.send("Die Rolle ist zu hoch oder ich habe keine Berechtigung.")

    @bot.hybrid_command(name="removerole")
    @app_commands.describe(
        member="Das Mitglied, dem die Rolle entzogen werden soll",
        role="Die Rolle, die entfernt werden soll",
    )
    @commands.guild_only()
    @commands.has_guild_permissions(manage_roles=True)
    async def remove_role(
        ctx: commands.Context[commands.Bot],
        member: discord.Member,
        role: discord.Role,
    ) -> None:
        """Entfernt eine Rolle von einem Mitglied."""
        try:
            await member.remove_roles(role, reason=f"Entfernt von {ctx.author}")
            await ctx.send(f"{role.mention} wurde {member.mention} entfernt.")
        except discord.Forbidden:
            await ctx.send("Die Rolle ist zu hoch oder ich habe keine Berechtigung.")

    @bot.hybrid_command(name="remind")
    @app_commands.describe(
        minutes="Zeit bis zur Erinnerung in Minuten",
        message="Der Text, an den du erinnert werden möchtest",
    )
    async def remind(
        ctx: commands.Context[commands.Bot],
        minutes: int,
        *,
        message: str,
    ) -> None:
        """Sendet eine persönliche Erinnerung."""
        if minutes < 1 or minutes > 10080:
            await ctx.send("Die Zeit muss zwischen 1 und 10080 Minuten liegen.")
            return
        await ctx.send(f"Erinnerung in **{minutes} Minuten** geplant.", ephemeral=True)
        await asyncio.sleep(minutes * 60)
        await ctx.channel.send(f"{ctx.author.mention} Erinnerung: {message[:1500]}")

    @bot.hybrid_command(name="suggest")
    @app_commands.describe(idea="Dein Vorschlag für den Server")
    @commands.guild_only()
    async def suggest(ctx: commands.Context[commands.Bot], *, idea: str) -> None:
        """Sendet einen Community-Vorschlag."""
        embed = discord.Embed(
            title="Neuer Vorschlag", description=idea[:4096], color=discord.Color.teal()
        )
        embed.set_footer(text=f"Vorgeschlagen von {ctx.author.display_name}")
        message = await ctx.send(embed=embed)
        if isinstance(message, discord.Message):
            await message.add_reaction("👍")
            await message.add_reaction("👎")

    @bot.hybrid_command(name="logkanal")
    @app_commands.describe(
        channel="Der Kanal, in dem Moderations- und Community-Logs erscheinen"
    )
    @commands.guild_only()
    @commands.has_guild_permissions(manage_guild=True)
    async def log_channel(
        ctx: commands.Context[commands.Bot],
        channel: discord.TextChannel,
    ) -> None:
        """Setzt den Kanal für Moderations- und Community-Logs."""
        welcome_config.setdefault(str(ctx.guild.id), {})["log_channel_id"] = channel.id
        save_welcome_config(welcome_config)
        await ctx.send(
            f"Logs werden ab jetzt in {channel.mention} gesendet.", ephemeral=True
        )

    @bot.hybrid_command(name="support")
    @commands.guild_only()
    async def support(ctx: commands.Context[commands.Bot]) -> None:
        """Erklärt, wie ein Support-Ticket erstellt wird."""
        await ctx.send(
            "Nutze das Ticket-Panel und klicke auf **Ticket erstellen**, um Hilfe zu bekommen."
        )

    @bot.hybrid_command(name="ticketpanel")
    @app_commands.describe(
        channel="Der Kanal, in dem das Ticket-Panel veröffentlicht wird"
    )
    @commands.guild_only()
    @commands.has_guild_permissions(manage_guild=True)
    async def ticket_panel(
        ctx: commands.Context[commands.Bot],
        channel: discord.TextChannel,
    ) -> None:
        """Sendet das Panel, über das Mitglieder Tickets erstellen."""
        embed = discord.Embed(
            title="Brauchst du Hilfe?",
            description=(
                "Unser Support-Team hilft dir gerne weiter.\n\n"
                "Klicke auf **Ticket erstellen**, um einen privaten Kanal zu öffnen."
            ),
            color=discord.Color.blurple(),
        )
        embed.set_footer(text="Support • Wir sind für dich da")
        panel_message = await channel.send(embed=embed, view=TicketPanelView())
        guild_config = welcome_config.setdefault(str(ctx.guild.id), {})
        guild_config["ticket_panel_channel_id"] = channel.id
        guild_config["ticket_panel_message_id"] = panel_message.id
        save_welcome_config(welcome_config)
        await ctx.send(f"Das Ticket-Panel wurde in {channel.mention} veröffentlicht.")

    @bot.hybrid_command(name="ticketrolle")
    @app_commands.describe(
        role="Die Support-Rolle, die alle Tickets sehen und schließen darf"
    )
    @commands.guild_only()
    @commands.has_guild_permissions(manage_guild=True)
    async def ticket_role(
        ctx: commands.Context[commands.Bot],
        role: discord.Role,
    ) -> None:
        """Setzt die Rolle, die alle Tickets sehen darf."""
        welcome_config.setdefault(str(ctx.guild.id), {})["ticket_support_role_id"] = (
            role.id
        )
        save_welcome_config(welcome_config)
        await ctx.send(f"Die Support-Rolle ist jetzt **{role.name}**.")

    @bot.hybrid_command(name="ticketkategorie")
    @app_commands.describe(
        category="Die Kategorie, unter der neue Ticketkanäle erstellt werden"
    )
    @commands.guild_only()
    @commands.has_guild_permissions(manage_guild=True)
    async def ticket_category(
        ctx: commands.Context[commands.Bot],
        category: discord.CategoryChannel,
    ) -> None:
        """Setzt die Kategorie für neue Tickets."""
        welcome_config.setdefault(str(ctx.guild.id), {})["ticket_category_id"] = (
            category.id
        )
        save_welcome_config(welcome_config)
        await ctx.send(f"Neue Tickets werden unter **{category.name}** erstellt.")

    @bot.hybrid_command(name="hilfe")
    async def help_command(ctx: commands.Context[commands.Bot]) -> None:
        """Zeigt die verfügbaren Befehle."""
        embed = discord.Embed(
            title="Bot-Hilfe",
            description="Alle Funktionen sind als Slash-Commands mit `/` verfügbar.",
            color=discord.Color.blurple(),
        )
        embed.add_field(
            name="Community",
            value=(
                "`/serverinfo` – Serverübersicht\n"
                "`/userinfo` – Infos über ein Mitglied\n"
                "`/avatar` – Avatar anzeigen\n"
                "`/membercount` – Mitgliederzahl\n"
                "`/poll` – Ja/Nein-Umfrage\n"
                "`/suggest` – Vorschlag einreichen\n"
                "`/remind` – Erinnerung planen"
            ),
            inline=False,
        )
        embed.add_field(
            name="Moderation",
            value=(
                "`/clear` – Nachrichten löschen\n"
                "`/slowmode` – Slowmode einstellen\n"
                "`/lock` / `/unlock` – Kanal sperren oder freigeben\n"
                "`/giverole` / `/removerole` – Rollen verwalten\n"
                "`/logkanal` – Ereignisprotokoll einrichten"
            ),
            inline=False,
        )
        embed.add_field(
            name="Server-Setup",
            value=(
                "`/welcomekanal` – Begrüßungskanal festlegen\n"
                "`/standardrolle` – automatische Rolle festlegen\n"
                "`/willkommen` – eigenen Begrüßungstext festlegen\n"
                "`/ticketpanel` – Ticket-Panel veröffentlichen\n"
                "`/ticketrolle` – Support-Rolle festlegen\n"
                "`/ticketkategorie` – Ticket-Kategorie festlegen"
            ),
            inline=False,
        )
        embed.add_field(
            name="Inhalte & Extras",
            value=(
                "`/giveaway` – Giveaway starten\n"
                "`/announce` – Ankündigung senden\n"
                "`/say` – Nachricht als Bot senden\n"
                "`/channelinfo` / `/roleinfo` – technische Infos\n"
                "`/support` – Ticket-Hinweise\n"
                "`/ping` – Bot-Latenz"
            ),
            inline=False,
        )
        embed.set_footer(
            text="Admin-Commands sind nur mit den nötigen Berechtigungen nutzbar."
        )
        await ctx.send(embed=embed)

    return bot


def main() -> None:
    token = os.getenv("DISCORD_TOKEN")
    if not token:
        raise RuntimeError("DISCORD_TOKEN fehlt. Lege das Token als Replit Secret an.")

    bot = create_bot()
    bot.run(token, log_handler=None)


if __name__ == "__main__":
    main()
