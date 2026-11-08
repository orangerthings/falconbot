import asyncio
import discord
from discord import app_commands
from datetime import datetime, timedelta, timezone as dt_timezone
from discord.ext import commands, tasks
from gspread.utils import a1_range_to_grid_range, a1_to_rowcol
import re
from zoneinfo import ZoneInfo, available_timezones

import utils.db as db
import utils.embed as embed_helpers
import utils.localization as l

def to_military_times(time_range) -> tuple[tuple[int, int], tuple[int, int]]:
    time_range = time_range.lower().replace(" ", "")
    pattern = r"(\d{1,2}(?::\d{2})?)(am|pm)?-(\d{1,2}(?::\d{2})?)(am|pm)?"
    match = re.fullmatch(pattern, time_range)
    if not match:
        raise ValueError("Invalid time range")
    start, start_period, end, end_period = match.groups()
    def parse_time(value, period):
        hour, minute = map(int, (value.split(":") + ["0"])[:2])
        if not 1 <= hour <= 12 or not 0 <= minute <= 59:
            raise ValueError("Invalid time")
        if period == "pm" and hour != 12:
            hour += 12
        elif period == "am" and hour == 12:
            hour = 0
        return hour, minute
    # infer missing am/pm
    if not start_period and end_period:
        start_period = end_period
        # case: 9-5pm means 9am-5pm
        if parse_time(start, start_period) >= parse_time(end, end_period):
            start_period = "am"
    elif start_period and not end_period:
        end_period = start_period
        # case: 9pm-5 means 9pm-5am, 9am-5 means 9am-5pm
        if parse_time(end, end_period) <= parse_time(start, start_period):
            end_period = "am" if start_period == "pm" else "pm"
    elif not start_period and not end_period:
        # case: 5-9 means 5pm-9pm
        if parse_time(end, end_period) >= parse_time(start, start_period):
            start_period = "pm"
            end_period = "pm"
        # case: 9-5 means 9am-5pm
        else:
            start_period = "am"
            end_period = "pm"
    return parse_time(start, start_period), parse_time(end, end_period)

class ExtensionCog(commands.Cog):
    extension_commands = app_commands.Group(name="attendance", description="Commands related to attendance.", guild_only=True)

    async def schedule_message(self, dt: datetime, message: str, channel: discord.TextChannel):
        delay = (dt - datetime.now(dt_timezone.utc)).total_seconds()
        if delay > 0:
            await asyncio.sleep(delay)
        chunks = []
        while len(message) > 2000:
            split_at = message.rfind("\n", 0, 2000)
            if split_at <= 0:
                split_at = 2000
            chunks.append(message[:split_at])
            message = message[split_at:].lstrip("\n")
        if message:
            chunks.append(message)
        for chunk in chunks:
            try:
                await channel.send(chunk)
            except discord.DiscordException as e:
                print(f"Failed to send attendance reminder: {e}")
                break

    def __init__(self, bot: commands.Bot):
        self.reminders = {}
        self.bot = bot
        self.update_all_sheets_attendance.start()

    async def schedule_reminders(self):
        desired = {}
        unavailable_servers = set()
        now = datetime.now(dt_timezone.utc)
        sheet_configs = {}
        for reminder_time in await db.get_reminder_times():
            server_id = reminder_time["server_id"]
            server = await db.get_server(server_id)
            if server is None:
                continue
            try:
                channel = self.bot.get_channel(server["reminder_channel_id"]) or await self.bot.fetch_channel(server["reminder_channel_id"])
            except discord.HTTPException as e:
                print(f"Unable to fetch reminder channel for server {server_id}: {e}")
                unavailable_servers.add(server_id)
                continue
            if server_id not in sheet_configs:
                sheet_configs[server_id] = await db.get_sheet_config(server_id) or {}
            for event in await db.get_events(server_id):
                attendees = await db.get_attendees(server_id, event["event_id"])

                def roster(status, ping=True):
                    names = [
                        f"{a['name']} ||<@{a['discord_id']}>||" if ping and a["discord_id"] else a["name"]
                        for a in attendees if a["status"] == status
                    ]
                    return "\n".join(f"> {name}" for name in names) if names else "None"

                pings = " ".join(
                    f"<@{a['discord_id']}>" if a["discord_id"] else a["name"]
                    for a in attendees if a["status"] in {"Y", "M"}
                )
                start_time = event["start_time"]
                minutes = reminder_time["minutes"]
                due = datetime.fromtimestamp(start_time - minutes * 60, tz=dt_timezone.utc)
                key = (server_id, str(event["event_id"]), str(minutes))
                try:
                    local_start = datetime.fromtimestamp(start_time, tz=dt_timezone.utc).astimezone(ZoneInfo(server["timezone"]))
                    message = server["message"].format(
                        users=f"||{pings}||" if pings else "None",
                        meeting=event["name"],
                        attendees=roster("Y"),
                        maybe=roster("M"),
                        potential=roster("M"),
                        not_attending=roster("N", ping=False),
                        unknown=roster(""),
                        date=local_start.strftime("%B %-d, %Y"),
                        time=local_start.strftime("%-I:%M %p"),
                        end_time=(datetime.fromtimestamp(event["end_time"], tz=dt_timezone.utc)
                                  .astimezone(ZoneInfo(server["timezone"])).strftime("%-I:%M %p")
                                  if event["end_time"] else "unavailable"),
                        spreadsheet=sheet_configs[server_id].get("url", ""),
                    )
                except (KeyError, IndexError, ValueError) as e:
                    print(f"Invalid reminder template for server {server_id}: {e}")
                    continue
                signature = (due.timestamp(), message, channel.id)
                old = self.reminders.get(key)
                if due > now or (old and not old[1].done()):
                    desired[key] = (signature, due, message, channel)

        for key, (signature, task) in list(self.reminders.items()):
            if key[0] in unavailable_servers:
                continue
            if key not in desired or signature != desired[key][0]:
                task.cancel()
                del self.reminders[key]

        for key, (signature, due, message, channel) in desired.items():
            old = self.reminders.get(key)
            if old and not old[1].done() and old[0] == signature:
                continue
            if old:
                old[1].cancel()
            self.reminders[key] = (
                signature,
                asyncio.create_task(self.schedule_message(due, message, channel)),
            )

    @extension_commands.command(name="edit", description="edit one attendance bot setting")
    @app_commands.describe(field="Setting to change", value="New value for that setting")
    @app_commands.choices(field=[
        app_commands.Choice(name="reminder channel ID", value="channel_id"),
        app_commands.Choice(name="message template", value="message"),
        app_commands.Choice(name="timezone", value="timezone"),
        app_commands.Choice(name="spreadsheet URL", value="sheet_url"),
        app_commands.Choice(name="worksheet name", value="sheet"),
        app_commands.Choice(name="attendee rows", value="attendee_rows"),
        app_commands.Choice(name="first event column", value="start_col"),
        app_commands.Choice(name="date row", value="date_row"),
        app_commands.Choice(name="time row", value="time_row"),
        app_commands.Choice(name="attendee name column", value="user_col"),
        app_commands.Choice(name="meeting name row", value="meeting_name_row"),
        app_commands.Choice(name="last name column", value="user_col_last_name"),
    ])
    @app_commands.guild_only()
    @app_commands.checks.has_permissions(manage_guild=True)
    async def edit(self, interaction: discord.Interaction, field: app_commands.Choice[str], value: str):
        server_id = interaction.guild.id
        server_fields = {"channel_id": "reminder_channel_id", "message": "message", "timezone": "timezone"}
        sheet_fields = {"sheet_url": "url", "sheet": "sheet", "attendee_rows": "attendee_rows", "start_col": "start_col", "date_row": "date_row", "time_row": "time_row", "user_col": "user_col", "meeting_name_row": "meeting_name_row", "user_col_last_name": "user_col_last_name"}
        if field.value in server_fields:
            if await db.get_server(server_id) is None:
                await interaction.response.send_message(l.get("attendance.no_setup"), ephemeral=True)
                return
            if field.value == "timezone" and value not in available_timezones():
                await interaction.response.send_message(l.get("attendance.unknown_timezone"), ephemeral=True)
                return
            if field.value == "channel_id":
                try:
                    parsed = int(value)
                    if parsed <= 0:
                        raise ValueError
                except ValueError:
                    await interaction.response.send_message("Channel ID must be a positive number.", ephemeral=True)
                    return
            elif field.value == "message":
                try:
                    value.format(**{name: "x" for name in ("users", "meeting", "attendees", "maybe", "potential", "not_attending", "unknown", "date", "time", "end_time", "spreadsheet")})
                except (KeyError, IndexError, ValueError) as e:
                    await interaction.response.send_message(f"Invalid message template: {e}", ephemeral=True)
                    return
                parsed = value
            else:
                parsed = value
            await db.update_server_setting(server_id, server_fields[field.value], parsed)
        else:
            config = await db.get_sheet_config(server_id)
            if config is None:
                await interaction.response.send_message("Set up the spreadsheet before editing its settings.", ephemeral=True)
                return
            key = sheet_fields[field.value]
            if key in {"meeting_name_row", "user_col_last_name"} and value.strip().lower() in {"none", "null", "clear"}:
                parsed = None
            elif key in {"date_row", "time_row", "meeting_name_row"}:
                try:
                    parsed = int(value)
                    if parsed < 1:
                        raise ValueError
                except ValueError:
                    await interaction.response.send_message("Row numbers must be positive integers (or 'none' for optional rows).", ephemeral=True)
                    return
            elif key == "attendee_rows":
                match = re.fullmatch(r"\s*(\d+)\s*[:\-]\s*(\d+)\s*", value)
                if not match or int(match[1]) < 1 or int(match[2]) < int(match[1]):
                    await interaction.response.send_message("Attendee rows must be a range like 42:59.", ephemeral=True)
                    return
                parsed = value
            elif key in {"start_col", "user_col", "user_col_last_name"}:
                try:
                    a1_to_rowcol(f"{value.upper()}1")
                except Exception:
                    await interaction.response.send_message("Column must be letters, like A or AB.", ephemeral=True)
                    return
                parsed = value.upper()
            else:
                parsed = value
            config[key] = parsed
            config.pop("server_id", None)
            await db.save_gsheet_config(server_id, **config)
        await interaction.response.send_message(f"Updated {field.name}.", ephemeral=True)

    async def cog_app_command_error(self, interaction: discord.Interaction, error: app_commands.AppCommandError):
        if isinstance(error, app_commands.MissingPermissions):
            message = "You need Manage Server permission to use this command."
            if interaction.response.is_done():
                await interaction.followup.send(message, ephemeral=True)
            else:
                await interaction.response.send_message(message, ephemeral=True)
        else:
            raise error

    @extension_commands.command(name="help", description="show attendance bot command help")
    @app_commands.guild_only()
    async def help(self, interaction: discord.Interaction):
        await interaction.response.send_message(
            "**Attendance bot commands**\n"
            "`/attendance setup` — choose the reminder channel, message template, timezone, and reminder times (default reminder: 3 hours before).\n"
            "`/attendance setup_sheets` — connect an attendance spreadsheet.\n"
            "`/attendance edit` — change one bot or spreadsheet setting.\n"
            "`/attendance get_attendance` — view attendance for a meeting.\n"
            "`/attendance link_discord` and `/attendance unlink_discord` — link attendee names to Discord members.\n\n"
            "Message template fields: `{meeting}`, `{date}`, `{time}`, `{end_time}`, `{users}`, `{attendees}`, `{maybe}`, `{potential}`, `{not_attending}`, `{unknown}`, `{spreadsheet}`.",
            ephemeral=True,
        )

    @extension_commands.command(name="setup", description="quickly set up attendance tracking")
    @app_commands.guild_only()
    @app_commands.describe(
        channel="The channel where attendance messages will be sent",
        message="Template fields: {meeting}, {date}, {time}, {end_time}, {users}, {attendees}, {maybe}, {potential}, {not_attending}, {unknown}, {spreadsheet}.",
        time_before="minutes before the meeting to send the reminder (default: 180, or 3 hours)"
    )
    @app_commands.checks.has_permissions(manage_guild=True)
    async def setup(self, interaction: discord.Interaction,
        channel: discord.TextChannel,
        message: str = "📣 **Meeting Name: {meeting}**\n📅 {date} · {time}–{end_time}\n\nAttendees (marked Y):\n{attendees}\n\nPotential (marked M):\n{potential}\n\nNot attending (marked N):\n{not_attending}\n\nUnknown (blank in spreadsheet):\n{unknown}\n\nIf you intend to come or change your status, please open the [spreadsheet here]({spreadsheet})",
        time_before: app_commands.Range[int, 0, 1440] = 180,
        time_before_2: app_commands.Range[int, -1, 1440] = -1,
        timezone: str = "America/New_York"
    ):
        if timezone not in available_timezones():
            await interaction.response.send_message(
                l.get("attendance.unknown_timezone"), ephemeral=True
            )
            return
        if await db.get_server(interaction.guild.id) is not None:
            await interaction.response.send_message(
                l.get("attendance.setup_already"), ephemeral=True
            )
            return
        offsets = [time_before] + ([time_before_2] if time_before_2 != -1 else [])
        await db.create_server(interaction.guild.id, channel.id, message, timezone, offsets)
        await interaction.response.send_message(
            l.get("attendance.setup_success"), ephemeral=True
        )
    @setup.autocomplete("timezone")
    async def timezone_autocomplete(self, interaction: discord.Interaction, current: str):
        matches = [tz for tz in sorted(available_timezones()) if current.lower() in tz.lower()]
        print(matches)
        return [app_commands.Choice(name=tz, value=tz) for tz in matches[:25]]

    async def update_sheets_attendance(self, server_id: int) -> tuple[int, int]:
        server = await db.get_server(server_id)
        config = await db.get_sheet_config(server_id)
        if server is None or config is None:
            raise RuntimeError("Attendance or sheet config is not set up for this server")

        timezone = server["timezone"]
        url = config["url"]
        sheet = config["sheet"]
        attendee_rows = config["attendee_rows"]
        start_col = config["start_col"]
        date_row = config["date_row"]
        time_row = config["time_row"]
        user_col = config["user_col"]
        meeting_name_row = config["meeting_name_row"]
        user_col_last_name = config["user_col_last_name"]

        try:
            m = re.compile(r"\s*(\d+)\s*[:\-]\s*(\d+)\s*").fullmatch(attendee_rows)
            if not m:
                raise ValueError("Rows must look like 42:58")
            start, end = int(m[1]), int(m[2])
            if start < 1 or end < start:
                raise ValueError("Row range is invalid")
            row_start, row_end = start - 1, end

            first_col = a1_to_rowcol(f"{start_col}1")[1] - 1
            name_col = a1_to_rowcol(f"{user_col}1")[1] - 1
            last_col = a1_to_rowcol(f"{user_col_last_name}1")[1] - 1 if user_col_last_name else None
        except Exception as e:
            raise e

        try:
            values = await asyncio.to_thread(
                lambda: self.bot.gspread.open_by_url(url).worksheet(sheet).get_all_values()
            )
        except Exception as e:
            raise e

        def cell(r: int, c: int) -> str:
            row = values[r] if 0 <= r < len(values) else []
            return row[c].strip() if 0 <= c < len(row) else ""

        tz = ZoneInfo(timezone)
        now = datetime.now(tz)
        row_end = min(row_end, len(values))

        events = {}
        attendance = []

        j = first_col
        max_columns = max((len(row) for row in values), default=0)
        last_date = None
        prev_month = None
        running_year = now.year
        # iterate until the time row is empty to recognize all attendances since first_col
        while j < max_columns:
            time_cell = cell(time_row - 1, j)
            if not time_cell:
                j += 1
                continue
            date_cell = cell(date_row - 1, j)
            if date_cell:
                last_date = date_cell
            elif last_date is None:
                raise ValueError(f"No date found at or before column {j + 1}")
            parts = [p for p in last_date.split("/") if p]
            if len(parts) < 2:
                raise ValueError(f"Can't read date {last_date!r} in column {j + 1}")
            month, day = int(parts[0]), int(parts[1])
            if len(parts) > 2:
                year = int(parts[2])
                if year < 100:
                    year += 2000
                running_year = year
            else:
                if prev_month is not None and month < prev_month:
                    running_year += 1
                year = running_year
            prev_month = month

            (start_hour, start_minute), (end_hour, end_minute) = to_military_times(time_cell)
            local_start = datetime(year, month, day, start_hour, start_minute, tzinfo=tz)
            local_end = datetime(year, month, day, end_hour, end_minute, tzinfo=tz)
            if local_end <= local_start:
                local_end += timedelta(days=1)
            start_utc = local_start.astimezone(dt_timezone.utc)
            end_utc = local_end.astimezone(dt_timezone.utc)

            event_id = f"gs-{server_id}-{j}"
            meeting_name = cell(meeting_name_row - 1, j) if meeting_name_row else ""
            meeting_name = meeting_name or f"Event at {local_start.strftime('%B %d')}, {local_start.strftime('%I:%M %p')}"
            events[event_id] = (meeting_name, start_utc, end_utc)

            for i in range(row_start, row_end):
                attendee = cell(i, name_col)
                if not attendee:
                    continue
                if last_col is not None:
                    last_name = cell(i, last_col)
                    if last_name:
                        attendee += f" {last_name[0]}."
                status = cell(i, j)[:1].upper()
                if status not in {"", "Y", "M", "N"}:
                    status = ""
                attendance.append((event_id, attendee, status))
            j += 1

        if not events:
            raise ValueError(f"No events found starting at column {start_col.upper()} (is the time row correct?)")

        await db.save_events(server_id, events, attendance)
        return len(events), len(attendance)

    
    @extension_commands.command(name="setup_sheets", description="set up googlel sheets attendance sheet")
    @app_commands.guild_only()
    @app_commands.describe(
        url="link to the google sheet",
        attendee_rows="rows where the attendees are i.e. 42:58",
        start_col="first column with an event i.e. H",
        date_row="row where the days are located i.e. 1",
        time_row="row where the times are located i.e. 3",
        user_col="column where the users are located i.e. A"
    )
    @app_commands.checks.has_permissions(manage_guild=True)
    async def setup_sheets(self, interaction: discord.Interaction,
        url: str,
        attendee_rows: str,
        start_col: str,
        date_row: int,
        time_row: int,
        user_col: str,
        meeting_name_row: int = None,
        user_col_last_name: str = None,
        sheet: str = "Sheet1"
    ):
        # so no timing out if reqtss takes too long
        await interaction.response.defer(ephemeral=True)

        server = await db.get_server(interaction.guild.id)
        if server is None:
            await interaction.followup.send(
                l.get("attendance.no_setup"), ephemeral=True
            )
            return
        existing = await db.get_sheet_config(interaction.guild.id)
        if existing is not None:
            await interaction.followup.send(
                l.get("attendance.setup_spreadsheet_already"), ephemeral=True,
            )
            return

        try:
            await db.save_gsheet_config(
                interaction.guild.id,
                url=url,
                sheet=sheet,
                attendee_rows=attendee_rows,
                start_col=start_col,
                date_row=date_row,
                time_row=time_row,
                meeting_name_row=meeting_name_row,
                user_col=user_col,
                user_col_last_name=user_col_last_name,
            )
        except Exception as e:
            await interaction.followup.send(
                l.get("attendance.error_accessing_cells", e=e), ephemeral=True
            )
            return

        try:
            num_events, num_attendances = await self.update_sheets_attendance(interaction.guild.id)
        except Exception as e:
            await interaction.followup.send(
                l.get("attendance.error_accessing_cells", e=e), ephemeral=True
            )
            return

        await interaction.followup.send(
            l.get("attendance.setup_spreadsheet_success", events=num_events, attendances=num_attendances),
            ephemeral=True,
        )

    @extension_commands.command(name="get_attendance", description="see who is signed up for each meeting")
    @app_commands.guild_only()
    @app_commands.describe(name="the name of the meeting to look up")
    async def get_attendance(self, interaction: discord.Interaction, name: str):
        await interaction.response.defer(ephemeral=True)

        server = await db.get_server(interaction.guild.id)
        if server is None:
            await interaction.followup.send(l.get("attendance.no_setup"), ephemeral=True)
            return
    
        event_id = name
        event = await db.get_event(interaction.guild.id, event_id)
        if event is None:
            await interaction.followup.send(l.get("attendance.no_meeting_found", name=name), ephemeral=True)
            return

        attendees = await db.get_attendees(interaction.guild.id, event_id)
        attending, maybe_attending, not_attending = [], [], []
        for attendee in attendees:
            if attendee["status"] == "Y":
                attending.append(attendee)
            elif attendee["status"] == "M":
                maybe_attending.append(attendee)
            elif attendee["status"] == "N":
                not_attending.append(attendee)
        lines = []
        lines.append(
            l.get(
                "attendance.meeting_attendance",
                name=event["name"],
                attending=len(attending),
                not_attending=len(not_attending)
            )
        )
        lines.append(":green_circle: `Attending: `")
        for attendee in attending:
            lines.append(f"`- {attendee['name']}`")
        lines.append(":yellow_circle: `Maybe Attending: `")
        for attendee in maybe_attending:
            lines.append(f"`- {attendee['name']}`")
        lines.append(":red_circle: `Not Attending: `")
        for attendee in not_attending:
            lines.append(f"`- {attendee['name']}`")
        embed = embed_helpers.list_to_embed(
            lines, 
            title=l.get("attendance.attendance_embed_title", name=event["name"])
        )
        await interaction.followup.send(embed=embed)
    @get_attendance.autocomplete("name")
    async def event_name_autocomplete(self, interaction: discord.Interaction, current: str):
        try:
            server = await db.get_server(interaction.guild.id)
            tz = ZoneInfo(server["timezone"])
            events = await db.search_events(interaction.guild.id, current)
        except Exception:
            return []
        return [
            app_commands.Choice(
                name=f"{e['name']} ({datetime.fromtimestamp(e['start_time'], tz=dt_timezone.utc).astimezone(tz).strftime('%m/%d/%Y')})",
                value=e["event_id"],
            )
            for e in events
        ]

    async def attendee_name_autocomplete(self, interaction: discord.Interaction, current: str):
        try:
            attendees = await db.search_attendees(interaction.guild.id, current)
        except Exception:
            return []
        return [
            app_commands.Choice(
                name=a["name"],
                value=a["attendee_id"],
            )
            for a in attendees
        ]
    
    @extension_commands.command(name="unlink_discord")
    @app_commands.guild_only()
    @app_commands.checks.has_permissions(manage_guild=True)
    @app_commands.autocomplete(name=attendee_name_autocomplete)
    async def unlink_discord(self, interaction: discord.Interaction, name: int):
        if await db.unlink_discord(interaction.guild.id, name):
            await interaction.response.send_message(l.get("attendance.unlink_discord_message"))
        else:
            await interaction.response.send_message(l.get("attendance.unlink_discord_failed_message"))

    @extension_commands.command(name="link_discord")
    @app_commands.guild_only()
    @app_commands.checks.has_permissions(manage_guild=True)
    @app_commands.autocomplete(name=attendee_name_autocomplete)
    async def link_discord(self, interaction: discord.Interaction, name: int, member: discord.Member):
        if await db.link_discord(interaction.guild.id, name, member.id):
            await interaction.response.send_message(l.get("attendance.link_discord_message"))
        else:
            await interaction.response.send_message(l.get("attendance.link_discord_failed_message"))
    
    @tasks.loop(seconds=10)
    async def update_all_sheets_attendance(self):
        for server in self.bot.guilds:
            try:
                existing = await db.get_sheet_config(server.id)
                if existing is None:
                    continue
                await self.update_sheets_attendance(server.id)
            except Exception as e:
                print(f"Attendance sync failed for server {server.id}: {e}")
                continue
        await self.schedule_reminders()
    @update_all_sheets_attendance.before_loop
    async def before_update(self):
        await self.bot.wait_until_ready()

async def setup(bot: commands.Bot):
    await bot.add_cog(ExtensionCog(bot))
    print("Attendance extension loaded")
