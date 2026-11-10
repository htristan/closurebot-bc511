import requests
import json
import time
import boto3
from boto3.dynamodb.conditions import Attr
from boto3.dynamodb.conditions import Key
from botocore.exceptions import NoCredentialsError, PartialCredentialsError
from shapely.geometry import Point, Polygon, LineString
from decimal import Decimal
from discord_webhook import DiscordWebhook, DiscordEmbed
import os
from datetime import datetime, timedelta, date
import calendar
from pytz import timezone
import logging
import random
import re
import logging

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler()  # Logs to the console
    ]
)

logger = logging.getLogger()
logger.setLevel(logging.INFO)


# Load the configuration file
with open('config.json', 'r') as f:
    config = json.load(f)

DISCORD_WEBHOOK_URL = os.environ['DISCORD_WEBHOOK']
AWS_ACCESS_KEY_ID = os.environ.get('AWS_DB_KEY', None)
AWS_SECRET_ACCESS_KEY = os.environ.get('AWS_DB_SECRET_ACCESS_KEY', None)

discordUsername = "DriveBC"
discordAvatarURL = "https://pbs.twimg.com/profile_images/961736998745600000/Zrqm1EiB_400x400.jpg"

# Fallback mechanism for credentials
try:
    # Use environment variables if they exist
    if AWS_ACCESS_KEY_ID and AWS_SECRET_ACCESS_KEY:
        dynamodb = boto3.resource(
            'dynamodb',
            region_name='us-east-1',
            aws_access_key_id=AWS_ACCESS_KEY_ID,
            aws_secret_access_key=AWS_SECRET_ACCESS_KEY
        )
    else:
        # Otherwise, use IAM role permissions (default behavior of boto3)
        dynamodb = boto3.resource('dynamodb', region_name='us-east-1')
except (NoCredentialsError, PartialCredentialsError):
    print("AWS credentials are not properly configured. Ensure IAM role or environment variables are set.")
    raise

# Specify the name of your DynamoDB table
table = dynamodb.Table(config['db_name'])

utc_timestamp = None

def update_utc_timestamp():
    global utc_timestamp
    utc_timestamp = calendar.timegm(datetime.utcnow().timetuple())

# set the current UTC timestamp for use in a few places
update_utc_timestamp()


def float_to_decimal(data):
    """
    Recursively converts all float values in the input (dict, list, or scalar) to Decimal,
    as DynamoDB does not support float.
    """
    if isinstance(data, float):
        return Decimal(str(data))  # Convert float to Decimal
    elif isinstance(data, dict):
        return {key: float_to_decimal(value) for key, value in data.items()}  # Recurse into dict
    elif isinstance(data, list):
        return [float_to_decimal(item) for item in data]  # Recurse into list
    else:
        return data  # Return data as-is if not float, dict, or list

def getThreadID(threadName):
    if threadName == 'LowerMainland':
        return config['Thread-LowerMainland']
    elif threadName == 'VancouverIsland':
        return config['Thread-VancouverIsland']
    else:
        return config['Thread-CatchAll'] #Other catch all thread

def unix_to_readable_with_timezone(iso_timestamp):
    """
    Converts an ISO 8601 timestamp with a timezone offset into a human-readable string.
    If no timezone is present, defaults to the configured timezone in `config['timezone']`.
    """
    try:
        # Parse the timestamp with dateutil to include timezone info
        parsed_time = datetime.fromisoformat(iso_timestamp)
    except ValueError:
        # Handle improperly formatted timestamps
        return "Invalid Timestamp"

    # Convert to local timezone if present; fallback to config['timezone']
    if parsed_time.tzinfo is not None:
        local_time = parsed_time
    else:
        # If no timezone info exists, use the configured timezone as fallback
        fallback_tz = timezone(config['timezone'])
        local_time = parsed_time.replace(tzinfo=timezone('UTC')).astimezone(fallback_tz)

    return local_time.strftime('%Y-%b-%d %I:%M %p')

def parse_geography(geography):
    """
    Parses the geography field to extract a representative Point.
    Handles Point and LineString geometries.
    """
    if geography['type'] == 'Point':
        # Return the Point directly
        coordinates = geography['coordinates']
        return Point(coordinates[0], coordinates[1])
    elif geography['type'] == 'LineString':
        # Handle LineString: pick the midpoint as the representative point
        line = LineString(geography['coordinates'])
        return line.interpolate(0.5, normalized=True)  # Midpoint
    else:
        raise ValueError(f"Unsupported geography type: {geography['type']}")

def check_which_polygon(areaName):
    # Function to see which polygon a point is in, and returns the text. Returns "Other" if unknown.
    if areaName == 'Lower Mainland District':
        return 'LowerMainland'
    elif areaName == 'Vancouver Island District':
        return 'VancouverIsland'
    else:
        return 'Other'

def parse_time_with_fallback(iso_time_str, fallback_tz):
    """
    Parses an ISO 8601 timestamp, applying a fallback timezone if none exists in the input.
    """
    # Check if the string includes timezone information
    if iso_time_str.endswith("Z"):  # UTC timezone indicated by 'Z'
        dt = datetime.fromisoformat(iso_time_str.replace("Z", "+00:00"))
    elif "+" in iso_time_str or "-" in iso_time_str[10:]:  # Already has a timezone offset
        dt = datetime.fromisoformat(iso_time_str)
    else:  # No timezone information, apply the fallback timezone
        naive_dt = datetime.fromisoformat(iso_time_str)
        dt = naive_dt.replace(tzinfo=fallback_tz)

    return dt

# Path 2 only. These are closure phrases. "detour", "avalanche", "both lanes",
# and "hov lane blocked" are intentionally absent: they match open roads.
DEFAULT_CLOSURE_KEYWORDS = [
    "road closed",
    "road closure",
    "bridge closed",
    "route closed",
    "all lanes closed",
    "full closure",
]
_RAMP_RE = re.compile(r"\b(?:on-?ramp|off-?ramp|ramp)\b", re.IGNORECASE)

def is_road_closed(event):
    """True when any road on the event has Open511 state CLOSED."""
    return any(_road_state(road) == "CLOSED" for road in event.get("roads") or [])

def _road_state(road):
    state = road.get("state")
    if isinstance(state, str):
        return state.upper()
    return ""

def is_ramp(road):
    """A ramp closure is not a full road closure. Highway closures still alert."""
    text = " ".join(str(road.get(field) or "") for field in ("name", "from", "to"))
    return _RAMP_RE.search(text) is not None

def _closure_keywords():
    return config.get("closure_keywords", DEFAULT_CLOSURE_KEYWORDS)

def _fallback_tz():
    return timezone(config['timezone'])

def _parse_schedule_start(value, fallback_tz):
    value = value.strip()
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        # A recurring start_date is a local calendar day.
        return fallback_tz.localize(datetime.fromisoformat(value))
    if value.endswith("Z"):
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    if "+" in value or "-" in value[10:]:
        return datetime.fromisoformat(value)
    # Interval times omit the offset. 14:45 is 7:45 AM PDT, so they are UTC.
    return timezone("UTC").localize(datetime.fromisoformat(value))

def schedule_has_started(event, now=None):
    """
    True when the event has no schedule, or any recorded start is already past.
    This is the event start, not the daily closure window inside a long event.
    """
    fallback_tz = _fallback_tz()
    if now is None:
        now = datetime.now(timezone("UTC")).astimezone(fallback_tz)
    elif now.tzinfo is None:
        now = fallback_tz.localize(now)
    else:
        now = now.astimezone(fallback_tz)

    starts = []
    schedule = event.get("schedule") or {}
    for interval in schedule.get("intervals") or []:
        if not isinstance(interval, str) or "/" not in interval:
            continue
        start_str = interval.split("/", 1)[0].strip()
        if not start_str:
            continue
        try:
            starts.append(_parse_schedule_start(start_str, fallback_tz))
        except ValueError:
            continue
    for recurring in schedule.get("recurring_schedules") or []:
        start_date = recurring.get("start_date")
        if not start_date:
            continue
        try:
            starts.append(_parse_schedule_start(start_date, fallback_tz))
        except ValueError:
            continue

    if not starts:
        return True
    return any(start <= now for start in starts)

def alert_path(event, now=None):
    """
    closed_road: any severity, a non-ramp road is CLOSED, and the schedule has started.
    keyword: MAJOR/MODERATE only, the description matches a closure phrase, and no road is CLOSED.
    """
    if schedule_has_started(event, now) and any(
        _road_state(road) == "CLOSED" and not is_ramp(road)
        for road in event.get("roads") or []
    ):
        return "closed_road"
    if is_road_closed(event):
        return None
    severity = (event.get("severity") or "").upper()
    if severity not in ("MAJOR", "MODERATE"):
        return None
    description = (event.get("description") or "").lower()
    if any(keyword.lower() in description for keyword in _closure_keywords()):
        return "keyword"
    return None

def should_alert(event, now=None):
    return alert_path(event, now) is not None

def post_to_discord(event, post_type, threadName, point=None):
    """
    Posts a message to Discord based on the type of event (closure, update, or archived),
    including thread routing and geo information.
    """
    # Get the thread ID based on the thread name
    threadID = getThreadID(threadName)

    # Get the title and color based on the post type
    post_types = {
        "closure": {"title": "New Event", "color": 15548997},
        "update": {"title": "Event Updated", "color": 16753920},
        "archived": {"title": "Event Cleared", "color": 52224},
    }
    if post_type not in post_types:
        raise ValueError(f"Unknown post type: {post_type}")
    title = post_types[post_type]["title"]
    color = post_types[post_type]["color"]

    # Create the embed
    embed = DiscordEmbed(title=title, color=color)

    # Add Event Type and Severity
    event_type = event.get("event_type", "UNKNOWN")
    severity = event.get("severity", "UNKNOWN")
    embed.add_embed_field(name="Event Type", value=f"{event_type.capitalize()} - {severity.capitalize()}")

    # Add Timing Information
    schedule = event.get("schedule", {})
    intervals = schedule.get("intervals", [])
    recurring_schedules = schedule.get("recurring_schedules", [])
    start_time = None

    if intervals:
        # Handle intervals if provided
        interval_start_str = intervals[0].split("/")[0]  # Extract start time from interval
        start_time = unix_to_readable_with_timezone(interval_start_str)
        embed.add_embed_field(name="Start Time", value=start_time)
    elif recurring_schedules:
        # Handle recurring schedules if provided
        recurring = recurring_schedules[0]  # Assume the first recurring schedule
        start_date = unix_to_readable_with_timezone(recurring.get("start_date", "Unknown"))
        end_date = unix_to_readable_with_timezone(recurring.get("end_date", "Unknown"))
        daily_start = recurring.get("daily_start_time", "Unknown")
        daily_end = recurring.get("daily_end_time", "Unknown")
        embed.add_embed_field(
            name="Schedule",
            value=f"{start_date} to {end_date}\nDaily: {daily_start} - {daily_end}"
        )

    # Add Road Information
    roads = event.get("roads", [])
    if roads:
        road_info = "\n".join(
            [
                f"{road['name']} ({road['direction']})"
                + (f" from {road['from']}" if "from" in road else "")
                + (f" to {road['to']}" if "to" in road else "")
                for road in roads
            ]
        )
        embed.add_embed_field(name="Affected Roads", value=road_info, inline=False)
        # Append additional info to the title
        additional_info = roads[0].get("name", "")
        if additional_info == "Other Roads":
            # Use regex to extract the road name from the description
            description = event.get("description", "")
            match = re.match(r"^(.*?),", description)
            if match:
                additional_info = match.group(1)
        embed.title += f" - {additional_info}"

    # Add Description
    embed.add_embed_field(name="Description", value=event.get("description", "No description provided"), inline=False)

    # Add Metadata
    created_time = unix_to_readable_with_timezone(event.get("created", "Unknown"))
    updated_time = unix_to_readable_with_timezone(event.get("updated", "Unknown"))
    embed.add_embed_field(name="Created", value=created_time)
    embed.add_embed_field(name="Last Updated", value=updated_time)

    driveBCID = event['id'].split("/")[-1]
    url511 = f"https://www.drivebc.ca/?type=event&id={driveBCID}"
    # Add Geo Information and Map Links
    if point:
        latitude = point.y
        longitude = point.x
        url_wme = f"https://www.waze.com/en-GB/editor?env=usa&lon={longitude}&lat={latitude}&zoomLevel=15"
        url_livemap = f"https://www.waze.com/live-map/directions?dir_first=no&latlng={latitude},{longitude}&overlay=false&zoom=16"
        if post_type == 'archived':
            embed.add_embed_field(name="Map Links", value=f"[WME]({url_wme}) | [Livemap]({url_livemap})", inline=False)
        else:
            embed.add_embed_field(name="Map Links", value=f"[DriveBC]({url511}) | [WME]({url_wme}) | [Livemap]({url_livemap})", inline=False)

    # Set Footer
    embed.set_footer(text=config['license_notice'])

    # Set the timestamp
    if post_type == "closure":
        # Final fallback to the current UTC time
        embed.set_timestamp(datetime.utcnow())
    elif post_type == "update":
        embed.set_timestamp(datetime.utcnow())
    elif post_type == "archived":
        last_touched = event.get("lastTouched", None)
        if last_touched:
            # DynamoDB returns the stored unix time as a Decimal.
            embed.set_timestamp(datetime.utcfromtimestamp(int(last_touched)))
        else:
            # Fallback to the current UTC time if lastTouched is missing
            embed.set_timestamp(datetime.utcnow())

    # Send to Discord
    webhook = DiscordWebhook(
        url=DISCORD_WEBHOOK_URL,
        username=discordUsername,
        avatar_url=discordAvatarURL,
        thread_id=threadID  # Route to the correct thread
    )
    webhook.add_embed(embed)
    webhook.execute()
def fetch_all_events():
    """
    Fetch every active event. Severity is not a closure signal: DriveBC marks
    many real closures as MINOR, and the CLOSED road state is what we keep.
    """
    base_url = "https://api.open511.gov.bc.ca/events"
    limit = 300
    all_events = []
    offset = 0

    while True:
        response = requests.get(
            base_url,
            params={"status": "ACTIVE", "limit": limit, "offset": offset},
        )

        if not response.ok:
            raise Exception(f"Error connecting to BC511 API: {response.status_code} - {response.text}")

        data = response.json()
        events = data.get('events', [])
        if not events:
            break

        all_events.extend(events)
        offset += limit

        if len(events) < limit:
            break

    return {"events": all_events}

def _scan_pages(scan_params):
    """Yield every page of a scan. DynamoDB stops a page at 1 MB."""
    while True:
        response = table.scan(**scan_params)
        yield response
        if 'LastEvaluatedKey' in response:
            scan_params['ExclusiveStartKey'] = response['LastEvaluatedKey']
        else:
            break

def _coerce_unix(value):
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float, Decimal)):
        return int(value)
    if isinstance(value, str):
        try:
            return int(parse_time_with_fallback(value, timezone('UTC')).timestamp())
        except ValueError:
            return None
    return None

def _item_updated_unix(item):
    """When this row was last updated by the feed.

    Older rows stored a numeric LastUpdated. BC rows store the Open511 `updated`
    ISO string. Prefer LastUpdated when both are present.
    """
    for key in ('LastUpdated', 'updated'):
        unix = _coerce_unix(item.get(key))
        if unix is not None:
            return unix
    return None

def check_and_post_events():
    #check if we need to clean old events
    last_execution_day = get_last_execution_day()
    today = date.today().isoformat()
    if last_execution_day is None or last_execution_day < today:
        # Perform cleanup of old events
        cleanup_old_events()

        # Update last execution day to current date
        update_last_execution_day()

    # Fetch Events from BC API
    data = fetch_all_events()

    # One scan of active rows. The feed has hundreds of non-closures; querying
    # Dynamo once per event is what pushed this Lambda toward its timeout.
    still_active = close_recent_events(data)

    for event in data['events']:
        if event.get('status') != 'ACTIVE':
            continue
        stored_event = still_active.get(event['id'])
        # Not tracked, and not a closure we would alert on. Skip the rest.
        path = None if stored_event is not None else alert_path(event)
        if stored_event is None and path is None:
            continue

        geography = event.get('geography') or {}
        if not geography:
            continue

        point = parse_geography(geography)

        if 'areas' in event and isinstance(event['areas'], list) and len(event['areas']) > 0:
            area_name = event['areas'][0].get('name', 'Unknown')
        else:
            area_name = None
        detectedPolygon = check_which_polygon(area_name)

        update_utc_timestamp()
        if stored_event is None:
            event['EventID'] = event['id']
            event['isActive'] = 1
            event['lastTouched'] = utc_timestamp
            event['DetectedPolygon'] = detectedPolygon
            event = float_to_decimal(event)
            logging.info(f"Posting New EventID: {event['EventID']} via {path}")
            post_to_discord(event,'closure',detectedPolygon,point)
            table.put_item(Item=event)
        else:
            event = float_to_decimal(event)
            lastUpdated = stored_event.get('updated')
            if lastUpdated != None:
                if lastUpdated != event['updated']:
                    event['EventID'] = event['id']
                    event['isActive'] = 1
                    event['lastTouched'] = utc_timestamp
                    event['DetectedPolygon'] = detectedPolygon
                    logging.info(f"Posting Updated EventID: {event['EventID']}")
                    post_to_discord(event,'update',detectedPolygon,point)
                    table.put_item(Item=event)
            lastTouched = stored_event.get('lastTouched')
            now = datetime.fromtimestamp(utc_timestamp)
            if lastTouched is None:
                logging.warning(f"EventID: {event['id']} - Missing lastTouched. Setting it now.")
                lastTouched_datetime = now
            else:
                lastTouched_datetime = datetime.fromtimestamp(int(lastTouched))
            time_diff_min = (now - lastTouched_datetime).total_seconds() / 60
            variability = random.uniform(-2, 2)
            time_diff_min += variability
            logging.info(
                f"EventID: {event['id']}, TimeDiff: {time_diff_min:.2f} minutes (Variability: {variability:.2f}), LastTouched: {lastTouched_datetime}, Now: {now}"
            )
            if abs(time_diff_min) > 5:
                table.update_item(
                    Key={'EventID': event['id']},
                    UpdateExpression="SET lastTouched = :val",
                    ExpressionAttributeValues={':val': utc_timestamp}
                )
                logging.info(f"EventID: {event['id']} - lastTouched updated successfully.")

def close_recent_events(data):
    # Ensure the expected structure is present
    if 'events' not in data or not isinstance(data['events'], list):
        raise KeyError("The API response does not contain a valid 'events' list.")

    events_by_id = {event['id']: event for event in data['events']}
    still_active = {}

    # Scan is capped at 1 MB per call. Follow LastEvaluatedKey until every active row is seen.
    # The filter does not reduce the read: inactive history is still scanned.
    scan_params = {
        'FilterExpression': Attr('isActive').eq(1)
    }
    for response in _scan_pages(scan_params):
        for item in response['Items']:
            event_id = item['EventID']
            point = parse_geography(item['geography'])
            event_data = events_by_id.get(event_id)
            mark_completed = (
                event_data is None
                or event_data.get('status', '').upper() != 'ACTIVE'
                or not should_alert(event_data)
            )

            if mark_completed:
                item = float_to_decimal(item)
                table.update_item(
                    Key={'EventID': event_id},
                    UpdateExpression="SET isActive = :val",
                    ExpressionAttributeValues={':val': 0}
                )
                if 'DetectedPolygon' in item and item['DetectedPolygon'] is not None:
                    post_to_discord(item,'archived',item['DetectedPolygon'],point)
                else:
                    post_to_discord(item,'archived',None,point)
            else:
                still_active[event_id] = item

    return still_active

def cleanup_old_events():
    # Inactive rows older than 5 days. BC stores `updated` as an ISO string, so the
    # cutoff is applied in Python. LastUpdated is still honored for older numeric rows.
    cutoff_unix = int((datetime.now(timezone('UTC')) - timedelta(days=5)).timestamp())
    scan_params = {
        'FilterExpression': Attr('isActive').eq(0)
    }
    with table.batch_writer() as batch:
        for response in _scan_pages(scan_params):
            for item in response['Items']:
                if item.get('isActive') != 0:
                    continue
                updated_unix = _item_updated_unix(item)
                if updated_unix is None or updated_unix >= cutoff_unix:
                    continue
                batch.delete_item(Key={'EventID': str(item['EventID'])})

def get_last_execution_day():
    response = table.query(
        KeyConditionExpression=Key('EventID').eq('LastCleanup')
    )

    items = response.get('Items')
    if items:
        item = items[0]
        last_execution_day = item.get('LastExecutionDay')
        return last_execution_day

    return None

def update_last_execution_day():
    today = datetime.now().date().isoformat()
    table.put_item(
        Item={
            'EventID': 'LastCleanup',
            'LastExecutionDay': today
        }
    )


def lambda_handler(event, context):
    check_and_post_events()

if __name__ == "__main__":
    # Simulate the Lambda environment by passing an empty event and context
    event = {}
    context = None
    lambda_handler(event, context)
