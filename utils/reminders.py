from datetime import datetime, timezone
from zoneinfo import ZoneInfo


def format_reminder_message(template, event, attendees, timezone_name, spreadsheet_url):
    def roster(status, ping=True):
        names = [
            f"{attendee['name']} ||<@{attendee['discord_id']}>||"
            if ping and attendee["discord_id"] else attendee["name"]
            for attendee in attendees if attendee["status"] == status
        ]
        return "\n".join(f"> {name}" for name in names) if names else "None"

    pings = " ".join(
        f"<@{attendee['discord_id']}>" if attendee["discord_id"] else attendee["name"]
        for attendee in attendees if attendee["status"] in {"Y", "M"}
    )
    zone = ZoneInfo(timezone_name)
    local_start = datetime.fromtimestamp(event["start_time"], tz=timezone.utc).astimezone(zone)
    end_time = (
        datetime.fromtimestamp(event["end_time"], tz=timezone.utc).astimezone(zone).strftime("%-I:%M %p")
        if event["end_time"] else "unavailable"
    )
    return template.format(
        users=f"||{pings}||" if pings else "None",
        meeting=event["name"],
        attendees=roster("Y"),
        maybe=roster("M"),
        potential=roster("M"),
        not_attending=roster("N", ping=False),
        unknown=roster(""),
        date=local_start.strftime("%B %-d, %Y"),
        time=local_start.strftime("%-I:%M %p"),
        end_time=end_time,
        spreadsheet=spreadsheet_url,
    )


def split_discord_message(message):
    chunks = []
    while len(message) > 2000:
        split_at = message.rfind("\n", 0, 2000)
        if split_at <= 0:
            split_at = 2000
        chunks.append(message[:split_at])
        message = message[split_at:].lstrip("\n")
    if message:
        chunks.append(message)
    return chunks
