"""Manage FalconBot's SQLite configuration without Discord slash commands."""
import argparse
import asyncio
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo, available_timezones

import utils.db as db
from utils.reminders import format_reminder_message, split_discord_message

DEFAULT_MESSAGE = "📣 **Meeting Name: {meeting}**\n📅 {date} · {time}–{end_time}\n\nAttendees (marked Y):\n{attendees}\n\nPotential (marked M):\n{potential}\n\nNot attending (marked N):\n{not_attending}\n\nUnknown (blank in spreadsheet):\n{unknown}\n\nIf you intend to come or change your status, please open the [spreadsheet here]({spreadsheet})"


def parser():
    p = argparse.ArgumentParser(description="Manage FalconBot's shared SQLite database.")
    default_db = Path(__file__).resolve().parent / "servers.db"
    p.add_argument("--db", default=str(default_db), help=f"database path (default: {default_db})")
    commands = p.add_subparsers(dest="command", required=True)

    help_parser = commands.add_parser("help", help="show CLI help")
    help_parser.add_argument("topic", nargs="?", choices=("servers", "setup", "setup-sheets", "attendees", "edit", "events", "preview", "attendance", "link", "unlink"))
    commands.add_parser("servers", help="list configured servers")

    setup = commands.add_parser("setup", help="configure reminders for a server")
    setup.add_argument("--server-id", required=True, type=int)
    setup.add_argument("--channel-id", required=True, type=int)
    setup.add_argument("--timezone", default="America/New_York")
    setup.add_argument("--time-before", type=int, default=180, help="reminder offset in minutes (default: 180, or 3 hours)")
    setup.add_argument("--time-before-2", type=int, help="optional second reminder offset in minutes")
    setup.add_argument("--message", default=DEFAULT_MESSAGE, help="reminder template")

    sheets = commands.add_parser("setup-sheets", help="save attendance spreadsheet settings")
    sheets.add_argument("--server-id", required=True, type=int)
    sheets.add_argument("--url", required=True)
    sheets.add_argument("--attendee-rows", required=True, help="row range, e.g. 42:58")
    sheets.add_argument("--start-col", required=True)
    sheets.add_argument("--date-row", required=True, type=int)
    sheets.add_argument("--time-row", required=True, type=int)
    sheets.add_argument("--user-col", required=True)
    sheets.add_argument("--sheet", default="Sheet1")
    sheets.add_argument("--meeting-name-row", type=int)
    sheets.add_argument("--user-col-last-name")

    attendees = commands.add_parser("attendees", help="list/search attendees and their IDs")
    attendees.add_argument("--server-id", required=True, type=int)
    attendees.add_argument("--query", default="", help="optional name filter")

    edit = commands.add_parser("edit", help="change one bot or spreadsheet setting")
    edit.add_argument("--server-id", required=True, type=int)
    edit.add_argument("--field", required=True, choices=("channel_id", "message", "timezone", "sheet_url", "sheet", "attendee_rows", "start_col", "date_row", "time_row", "user_col", "meeting_name_row", "user_col_last_name"))
    edit.add_argument("--value", required=True)

    events = commands.add_parser("events", help="list a server's events")
    events.add_argument("--server-id", required=True, type=int)

    preview = commands.add_parser("preview", help="preview the next scheduled reminder")
    preview.add_argument("--server-id", required=True, type=int)

    attendance = commands.add_parser("attendance", help="show attendance for an event")
    attendance.add_argument("--server-id", required=True, type=int)
    attendance.add_argument("--event", required=True, help="event name or event ID")

    for name in ("link", "unlink"):
        cmd = commands.add_parser(name, help=f"{name} a Discord member to an attendee")
        cmd.add_argument("--server-id", required=True, type=int)
        cmd.add_argument("--attendee-id", required=True, type=int)
        if name == "link":
            cmd.add_argument("--discord-id", required=True, type=int)
    return p


def validate_attendee_rows(value):
    match = re.fullmatch(r"\s*(\d+)\s*[:\-]\s*(\d+)\s*", value)
    if not match or int(match[1]) < 1 or int(match[2]) < int(match[1]):
        raise ValueError("Attendee rows must be a range like 42:59")


def validate_sheet_config(attendee_rows, start_col, date_row, time_row, user_col, meeting_name_row, last_name_col):
    validate_attendee_rows(attendee_rows)
    if date_row < 1 or time_row < 1 or (meeting_name_row is not None and meeting_name_row < 1):
        raise ValueError("Spreadsheet row numbers must be positive")
    for label, column in (("start", start_col), ("attendee", user_col), ("last name", last_name_col)):
        if column is not None and not re.fullmatch(r"[A-Za-z]+", column):
            raise ValueError(f"{label.title()} column must be letters, like A or AB")

async def run(args):
    db.DB_PATH = args.db
    await db.init()
    try:
        if args.command == "servers":
            for server in await db.get_servers():
                print(f"{server['server_id']}  channel={server['reminder_channel_id']}  timezone={server['timezone']}  reminder_minutes={server['times_before']}")
        elif args.command == "setup":
            if args.timezone not in available_timezones():
                raise ValueError(f"Unknown timezone: {args.timezone}")
            if not 0 <= args.time_before <= 1440 or (args.time_before_2 is not None and not 0 <= args.time_before_2 <= 1440):
                raise ValueError("Reminder offsets must be between 0 and 1440 minutes")
            if args.channel_id <= 0:
                raise ValueError("Channel ID must be positive")
            if await db.get_server(args.server_id):
                raise ValueError("This server is already configured")
            try:
                args.message.format(**{name: "x" for name in ("users", "meeting", "attendees", "maybe", "potential", "not_attending", "unknown", "date", "time", "end_time", "spreadsheet")})
            except (KeyError, IndexError, ValueError) as exc:
                raise ValueError(f"Invalid message template: {exc}") from exc
            offsets = list(dict.fromkeys([args.time_before] + ([args.time_before_2] if args.time_before_2 is not None else [])))
            await db.create_server(args.server_id, args.channel_id, args.message, args.timezone, offsets)
            print(f"Configured server {args.server_id}.")
        elif args.command == "setup-sheets":
            if not await db.get_server(args.server_id):
                raise ValueError("Run setup for this server first")
            if await db.get_sheet_config(args.server_id):
                raise ValueError("Sheet already configured; use `edit --field ...` to change it")
            validate_sheet_config(args.attendee_rows, args.start_col, args.date_row, args.time_row, args.user_col, args.meeting_name_row, args.user_col_last_name)
            await db.save_gsheet_config(
                args.server_id, url=args.url, sheet=args.sheet,
                attendee_rows=args.attendee_rows, start_col=args.start_col,
                date_row=args.date_row, time_row=args.time_row, user_col=args.user_col,
                meeting_name_row=args.meeting_name_row, user_col_last_name=args.user_col_last_name,
            )
            print("Saved sheet configuration. The bot will sync it on its next refresh.")
        elif args.command == "attendees":
            for attendee in await db.search_attendees(args.server_id, args.query):
                print(f"{attendee['attendee_id']}  {attendee['name']}")
        elif args.command == "edit":
            server = await db.get_server(args.server_id)
            if server is None:
                raise ValueError("Server is not configured")
            server_fields = {"channel_id": "reminder_channel_id", "message": "message", "timezone": "timezone"}
            sheet_fields = {"sheet_url": "url", "sheet": "sheet", "attendee_rows": "attendee_rows", "start_col": "start_col", "date_row": "date_row", "time_row": "time_row", "user_col": "user_col", "meeting_name_row": "meeting_name_row", "user_col_last_name": "user_col_last_name"}
            if args.field == "timezone":
                if args.value not in available_timezones():
                    raise ValueError(f"Unknown timezone: {args.value}")
                await db.update_server_setting(args.server_id, "timezone", args.value)
            elif args.field == "channel_id":
                await db.update_server_setting(args.server_id, "reminder_channel_id", int(args.value))
            elif args.field == "message":
                try:
                    args.value.format(**{name: "x" for name in ("users", "meeting", "attendees", "maybe", "potential", "not_attending", "unknown", "date", "time", "end_time", "spreadsheet")})
                except (KeyError, IndexError, ValueError) as exc:
                    raise ValueError(f"Invalid message template: {exc}") from exc
                await db.update_server_setting(args.server_id, "message", args.value)
            else:
                config = await db.get_sheet_config(args.server_id)
                if config is None:
                    raise ValueError("Set up the spreadsheet before editing its settings")
                key = sheet_fields[args.field]
                value = args.value
                if key in {"meeting_name_row", "user_col_last_name"} and value.strip().lower() in {"none", "null", "clear"}:
                    value = None
                elif key in {"date_row", "time_row", "meeting_name_row"}:
                    value = int(value)
                    if value < 1:
                        raise ValueError("Row numbers must be positive integers")
                elif key == "attendee_rows":
                    validate_attendee_rows(value)
                elif key in {"start_col", "user_col", "user_col_last_name"}:
                    if not re.fullmatch(r"[A-Za-z]+", value):
                        raise ValueError("Column must be letters, like A or AB")
                    value = value.upper()
                config[key] = value
                config.pop("server_id", None)
                await db.save_gsheet_config(args.server_id, **config)
            print(f"Updated {args.field}.")
        elif args.command == "events":
            server = await db.get_server(args.server_id)
            if server is None:
                raise ValueError("Server is not configured")
            for event in await db.get_events(args.server_id):
                start = datetime.fromtimestamp(event["start_time"], timezone.utc).astimezone(ZoneInfo(server["timezone"]))
                end = (datetime.fromtimestamp(event["end_time"], timezone.utc).astimezone(ZoneInfo(server["timezone"]))
                       if event["end_time"] else None)
                when = f"{start:%Y-%m-%d %H:%M %Z}–{end:%H:%M %Z}" if end else f"{start:%Y-%m-%d %H:%M %Z}–end unavailable"
                print(f"{event['event_id']}  {when}  {event['name']}")
        elif args.command == "preview":
            server = await db.get_server(args.server_id)
            if server is None:
                raise ValueError("Server is not configured")
            now = datetime.now(timezone.utc)
            upcoming = [
                (datetime.fromtimestamp(event["start_time"] - minutes * 60, timezone.utc), event, minutes)
                for event in await db.get_events(args.server_id)
                for minutes in server["times_before"]
                if datetime.fromtimestamp(event["start_time"] - minutes * 60, timezone.utc) > now
            ]
            if not upcoming:
                print("No upcoming reminders are scheduled.")
            else:
                due, event, minutes = min(upcoming, key=lambda item: item[0])
                attendees = await db.get_attendees(args.server_id, event["event_id"])
                sheet_config = await db.get_sheet_config(args.server_id) or {}
                try:
                    message = format_reminder_message(
                        server["message"], event, attendees, server["timezone"],
                        sheet_config.get("url", ""),
                    )
                except (KeyError, IndexError, ValueError) as exc:
                    raise ValueError(f"Invalid message template: {exc}") from exc
                chunks = split_discord_message(message)
                local_due = due.astimezone(ZoneInfo(server["timezone"]))
                print(f"Next reminder: {local_due:%A, %B %-d, %Y at %-I:%M %p %Z}")
                print(f"Meeting: {event['name']}")
                print(f"Reminder offset: {minutes} minutes")
                print(f"Channel ID: {server['reminder_channel_id']}")
                print(f"Discord messages: {len(chunks)}")
                for index, chunk in enumerate(chunks, 1):
                    print(f"\n--- Message {index}/{len(chunks)} ---\n{chunk}")
        elif args.command == "attendance":
            if not await db.get_server(args.server_id):
                raise ValueError("Server is not configured")
            events = await db.get_events(args.server_id)
            matches = [e for e in events if args.event in (e["event_id"], e["name"])]
            if not matches:
                raise ValueError("No matching event; use `events` to list event names and IDs")
            if len(matches) > 1:
                choices = "\n".join(f"{e['event_id']}  {e['name']}" for e in matches)
                raise ValueError(f"Event name is ambiguous; use an event ID:\n{choices}")
            event = matches[0]
            records = await db.get_attendees(args.server_id, event["event_id"])
            if not records:
                print(f"No attendance records for {event['name']} ({event['event_id']}).")
            for attendee in records:
                status = {"Y": "Attending", "M": "Potential", "N": "No", "": "Unknown"}.get(attendee["status"], attendee["status"])
                member = f"discord:{attendee['discord_id']}" if attendee["discord_id"] else "unlinked"
                print(f"{attendee['attendee_id']}  {status:10}  {attendee['name']}  ({member})")
        elif args.command == "link":
            if not await db.link_discord(args.server_id, args.attendee_id, args.discord_id):
                raise ValueError("Attendee not found for that server")
            print("Discord member linked.")
        elif args.command == "unlink":
            if not await db.unlink_discord(args.server_id, args.attendee_id):
                raise ValueError("Attendee not found for that server")
            print("Discord member unlinked.")
    finally:
        await db.close()


def main():
    p = parser()
    args = p.parse_args()
    if args.command == "help":
        if args.topic:
            p.parse_args([args.topic, "--help"])
        p.print_help()
        return
    try:
        asyncio.run(run(args))
    except (ValueError, OSError, sqlite3.Error, TypeError) as exc:
        raise SystemExit(f"error: {exc}") from exc


if __name__ == "__main__":
    main()
