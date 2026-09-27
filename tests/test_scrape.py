import pytest
from unittest.mock import patch, Mock, mock_open
import json
from datetime import datetime, timedelta
from pytz import timezone
from decimal import Decimal
from freezegun import freeze_time
from moto import mock_aws
import boto3
import os

# Add this before the scrape import
os.environ['DISCORD_WEBHOOK'] = 'https://mock-discord-webhook.com/test'

from scrape import (
    check_which_polygon, getThreadID, unix_to_readable_with_timezone,
    post_to_discord, is_road_closed, is_ramp, schedule_has_started, alert_path,
    close_recent_events, cleanup_old_events, float_to_decimal,
    check_and_post_events, fetch_all_events
)

# Load fixture data
@pytest.fixture
def sample_events():
    with open('tests/fixtures/sample_events.json', 'r') as f:
        return json.load(f)

@pytest.fixture
def sample_db_items():
    with open('tests/fixtures/sample_db_items.json', 'r') as f:
        items = json.load(f)
        # Convert float values to Decimal
        for item in items:
            for key, value in item.items():
                if isinstance(value, float):
                    item[key] = Decimal(str(value))
        return items

@pytest.fixture
def sample_event(sample_events):
    # Use the first event from the sample data
    return sample_events[0]

@pytest.fixture
def mock_dynamodb_table():
    with patch('boto3.resource') as mock_boto:
        mock_table = mock_boto.return_value.Table.return_value
        # Add common table operations
        mock_table.query.return_value = {'Items': []}
        mock_table.scan.return_value = {'Items': []}
        yield mock_table

@pytest.fixture
def mock_config():
    return {
        'Thread-LowerMainland': '123456',
        'Thread-VancouverIsland': '234567',
        'Thread-CatchAll': '567890',
        'timezone': 'US/Eastern',
        'license_notice': 'Test License Notice',
        'db_name': 'test-db'
    }

# Polygon Tests
@pytest.mark.parametrize("area_name,expected_region", [
    ('Lower Mainland District', 'LowerMainland'),
    ('Vancouver Island District', 'VancouverIsland'),
    ('Unknown District', 'Other'),
    ('Some Other Area', 'Other'),
])
def test_check_which_polygon(area_name, expected_region):
    assert check_which_polygon(area_name) == expected_region

# Thread ID Tests
@pytest.mark.parametrize("region,expected_thread", [
    ('LowerMainland', '123456'),
    ('VancouverIsland', '234567'),
    ('Other', '567890'),
    ('Invalid', '567890'),
])
def test_getThreadID(region, expected_thread, mock_config):
    with patch('scrape.config', mock_config):
        assert getThreadID(region) == expected_thread

# Time Conversion Tests
@pytest.mark.parametrize("iso_timestamp,expected_time", [
    ('2023-01-01T12:00:00Z', '2023-Jan-01 12:00 PM'),  # UTC to Eastern
    ('2022-12-31T00:00:00Z', '2022-Dec-31 12:00 AM'),  # UTC to Eastern
])
@freeze_time("2023-01-01 12:00:00", tz_offset=0)
def test_unix_to_readable_with_timezone(iso_timestamp, expected_time):
    assert unix_to_readable_with_timezone(iso_timestamp) == expected_time

# Discord Posting Tests
@patch('scrape.DiscordWebhook')
def test_post_to_discord_closure(mock_webhook, sample_event, mock_config):
    # Update sample event to have required fields
    sample_event['event_type'] = sample_event.get('eventType', 'roadwork')
    sample_event['severity'] = 'MODERATE'
    sample_event['description'] = sample_event.get('description', 'Test description')
    sample_event['created'] = '2023-01-01T12:00:00Z'
    sample_event['updated'] = '2023-01-01T12:00:00Z'
    
    with patch('scrape.config', mock_config):
        post_to_discord(sample_event, 'closure', 'LowerMainland')
        mock_webhook.assert_called_once()
        webhook_instance = mock_webhook.return_value
        webhook_instance.execute.assert_called_once()

@patch('scrape.DiscordWebhook')
def test_post_to_discord_updated(mock_webhook, sample_event, mock_config):
    # Update sample event to have required fields
    sample_event['event_type'] = sample_event.get('eventType', 'roadwork')
    sample_event['severity'] = 'MODERATE'
    sample_event['description'] = sample_event.get('description', 'Test description')
    sample_event['created'] = '2023-01-01T12:00:00Z'
    sample_event['updated'] = '2023-01-01T12:00:00Z'
    
    with patch('scrape.config', mock_config):
        post_to_discord(sample_event, 'update', 'LowerMainland')
        mock_webhook.assert_called_once()
        webhook_instance = mock_webhook.return_value
        webhook_instance.execute.assert_called_once()

@patch('scrape.DiscordWebhook')
def test_post_to_discord_completed(mock_webhook, sample_event, mock_config):
    # Update sample event to have required fields
    sample_event['event_type'] = sample_event.get('eventType', 'roadwork')
    sample_event['severity'] = 'MODERATE'
    sample_event['description'] = sample_event.get('description', 'Test description')
    sample_event['created'] = '2023-01-01T12:00:00Z'
    sample_event['updated'] = '2023-01-01T12:00:00Z'
    sample_event['lastTouched'] = Decimal('1759000000')
    
    with patch('scrape.config', mock_config):
        post_to_discord(sample_event, 'archived', 'LowerMainland')
        mock_webhook.assert_called_once()
        webhook_instance = mock_webhook.return_value
        webhook_instance.execute.assert_called_once()

# Database Operation Tests
@mock_aws
def test_cleanup_old_events(sample_db_items):
    # Create mock DynamoDB table
    dynamodb = boto3.resource('dynamodb', region_name='us-east-1')
    table = dynamodb.create_table(
        TableName='test-db',
        KeySchema=[{'AttributeName': 'EventID', 'KeyType': 'HASH'}],
        AttributeDefinitions=[{'AttributeName': 'EventID', 'AttributeType': 'S'}],
        BillingMode='PAY_PER_REQUEST'
    )
    
    # Modify items to ensure they're old enough
    old_timestamp = int((datetime.now() - timedelta(days=8)).timestamp())
    for item in sample_db_items:
        if item.get('isActive') == 0:
            item['LastUpdated'] = Decimal(str(old_timestamp))
    
    # Add test items from fixture
    for item in sample_db_items:
        table.put_item(Item=item)
    
    with patch('scrape.table', table):
        cleanup_old_events()
    
    # Verify old items were deleted
    for item in sample_db_items:
        if item.get('isActive') == 0:
            response = table.get_item(Key={'EventID': item['EventID']})
            assert 'Item' not in response

@mock_aws
def test_close_recent_events(sample_db_items):
    # Setup mock DynamoDB
    dynamodb = boto3.resource('dynamodb', region_name='us-east-1')
    table = dynamodb.create_table(
        TableName='test-db',
        KeySchema=[{'AttributeName': 'EventID', 'KeyType': 'HASH'}],
        AttributeDefinitions=[{'AttributeName': 'EventID', 'AttributeType': 'S'}],
        BillingMode='PAY_PER_REQUEST'
    )

    # Ensure we have an active item
    active_item = sample_db_items[0].copy()
    active_item['isActive'] = 1
    from decimal import Decimal
    active_item['geography'] = {'type': 'Point', 'coordinates': [Decimal('-75.69528'), Decimal('45.40719')]}
    table.put_item(Item=active_item)

    # Mock API response with empty list (no active events)
    mock_data = {'events': []}  # Proper data structure

    with patch('scrape.table', table), \
         patch('scrape.post_to_discord') as mock_post:
        close_recent_events(mock_data)
        mock_post.assert_called_once()

# Utility Function Tests
def test_float_to_decimal(sample_event):
    result = float_to_decimal(sample_event)
    # Check that numeric values are converted to Decimal
    for key, value in result.items():
        if isinstance(value, float):
            assert isinstance(result[key], Decimal)
        elif isinstance(value, dict):
            # Check nested dictionaries
            for nested_key, nested_value in value.items():
                if isinstance(nested_value, float):
                    assert isinstance(result[key][nested_key], Decimal)

# Main Function Test
@patch('scrape.fetch_all_events')
@patch('scrape.post_to_discord')
def test_check_and_post_events(mock_post, mock_fetch, mock_dynamodb_table, sample_events, mock_config):
    # Update sample events to match expected structure
    for event in sample_events:
        # Real BC 511 data already has these fields, just ensure they exist
        if 'status' not in event:
            event['status'] = 'ACTIVE'
        if 'geography' not in event:
            event['geography'] = {'type': 'Point', 'coordinates': [-123.1207, 49.2827]}
        if 'areas' not in event:
            event['areas'] = [{'name': 'Lower Mainland District'}]
        if 'event_type' not in event:
            event['event_type'] = event.get('eventType', 'CONSTRUCTION')
        if 'severity' not in event:
            event['severity'] = event.get('severity', 'MODERATE')
        if 'description' not in event:
            event['description'] = event.get('description', 'Test description')
        if 'created' not in event:
            event['created'] = '2023-01-01T12:00:00Z'
        if 'updated' not in event:
            event['updated'] = '2023-01-01T12:00:00Z'
        if 'roads' not in event:
            event['roads'] = [{'name': 'Highway 1', 'direction': 'BOTH'}]
        for road in event['roads']:
            road['state'] = 'CLOSED'

    # Mock fetch_all_events to return proper structure
    mock_fetch.return_value = {'events': sample_events}
    
    # Mock the database query to return no existing items
    mock_dynamodb_table.query.return_value = {'Items': []}
    
    with patch('scrape.table', mock_dynamodb_table), \
         patch('scrape.config', mock_config):
        # Test the main function
        check_and_post_events()
        
        # Verify Discord post was called for new events
        assert mock_post.call_count == len(sample_events)

def test_is_road_closed():
    assert is_road_closed({"roads": [{"state": "CLOSED"}]})
    assert is_road_closed({"roads": [{"state": "closed"}]})
    assert is_road_closed({
        "roads": [
            {"state": "ALL_LANES_OPEN"},
            {"state": "CLOSED"},
        ]
    })
    assert not is_road_closed({"roads": [{"state": "ALL_LANES_OPEN"}]})
    assert not is_road_closed({"roads": [{"name": "Highway 1"}]})
    assert not is_road_closed({})
    assert not is_road_closed({
        "description": "Vehicle incident. Closed. Expect major delays.",
        "roads": [{"state": "ALL_LANES_OPEN"}],
    })

def _event(**overrides):
    event = {
        "id": "drivebc.ca/RIDE-1",
        "status": "ACTIVE",
        "event_type": "INCIDENT",
        "severity": "MINOR",
        "description": "Vehicle incident. Closed. Expect major delays.",
        "created": "2026-09-24T18:00:00-07:00",
        "updated": "2026-09-24T18:00:00-07:00",
        "geography": {"type": "Point", "coordinates": [-121.44, 49.38]},
        "areas": [{"name": "Lower Mainland District"}],
        "roads": [{"name": "Highway 3", "direction": "E", "state": "CLOSED"}],
    }
    event.update(overrides)
    return event

def test_alert_path_rules():
    pacific = timezone("US/Pacific")
    now = pacific.localize(datetime(2026, 9, 27, 12, 0))
    assert is_ramp({"name": "Sumas Way Onramp", "from": "Highway 11"})
    assert is_ramp({"name": "Highway 1A", "from": "Trans-Canada Highway Onramp"})
    assert not is_ramp({"name": "Highway 3", "from": "5km East of Hope"})

    # Path 1: any severity, closed non-ramp, schedule already started or absent.
    assert alert_path(_event()) == "closed_road"
    assert alert_path(_event(severity="MAJOR", description="Road closed.")) == "closed_road"
    assert alert_path(_event(
        roads=[
            {"name": "Highway 1", "direction": "E", "state": "CLOSED"},
            {"name": "Sumas Way Onramp", "direction": "W", "state": "CLOSED"},
        ]
    )) == "closed_road"

    # A ramp-only closure stays quiet, even when the text says the ramp is closed.
    assert alert_path(_event(
        severity="MAJOR",
        description="Entrance ramp closed. Detour via McCallum Rd.",
        roads=[{"name": "Sumas Way Onramp", "direction": "W", "state": "CLOSED"}],
    )) is None

    # Future closures wait until the schedule start.
    assert not schedule_has_started(_event(schedule={"intervals": ["2099-01-01T00:00/"]}), now)
    assert alert_path(_event(schedule={"intervals": ["2099-01-01T00:00/"]}), now) is None
    assert alert_path(_event(schedule={"intervals": ["2020-01-01T00:00/"]}), now) == "closed_road"
    assert schedule_has_started(_event(), now)
    # 14:45 with no offset is 7:45 AM Pacific, not 2:45 PM.
    garibaldi = _event(schedule={"intervals": ["2026-09-27T14:45/2026-09-27T16:00"]})
    before_open = pacific.localize(datetime(2026, 9, 27, 7, 0))
    during = pacific.localize(datetime(2026, 9, 27, 8, 30))
    assert not schedule_has_started(garibaldi, before_open)
    assert schedule_has_started(garibaldi, during)
    assert schedule_has_started(_event(schedule={"recurring_schedules": [{"start_date": "2020-01-01"}]}), now)
    assert not schedule_has_started(_event(schedule={"recurring_schedules": [{"start_date": "2099-01-01"}]}), now)

    # Path 2: MAJOR/MODERATE prose, and only when no road is marked CLOSED.
    assert alert_path(_event(
        severity="MAJOR",
        description="Bridge closed at Johnston Bridge Loop. Industrial traffic must detour.",
        roads=[{"name": "Other Roads", "direction": "BOTH", "state": "ALL_LANES_OPEN"}],
    )) == "keyword"
    assert alert_path(_event(
        severity="MODERATE",
        description="Road Closed from 9AM-3PM PT on weekdays.",
        roads=[{"name": "Highway 1", "direction": "BOTH", "state": "ALL_LANES_OPEN"}],
    )) == "keyword"
    assert alert_path(_event(
        severity="MINOR",
        description="Road closed. Detour in effect.",
        roads=[{"name": "Allenby Road", "direction": "BOTH", "state": "ALL_LANES_OPEN"}],
    )) is None
    assert alert_path(_event(
        severity="MAJOR",
        description="Road maintenance at Second Avalanche Gate. Detour in effect. Both lanes open.",
        roads=[{"name": "Highway 3", "direction": "BOTH", "state": "ALL_LANES_OPEN"}],
    )) is None

@patch('scrape.fetch_all_events')
@patch('scrape.post_to_discord')
def test_new_events_use_both_alert_paths(mock_post, mock_fetch, mock_dynamodb_table):
    events = [
        _event(id="drivebc.ca/RIDE-closed"),
        _event(
            id="drivebc.ca/RIDE-keyword",
            severity="MAJOR",
            description="Bridge closed at Johnston Bridge Loop.",
            roads=[{"name": "Other Roads", "direction": "BOTH", "state": "ALL_LANES_OPEN"}],
        ),
        _event(
            id="drivebc.ca/RIDE-open",
            severity="MAJOR",
            description="Detour in effect at Second Avalanche Gate. Both lanes open.",
            roads=[{"name": "Highway 3", "direction": "BOTH", "state": "ALL_LANES_OPEN"}],
        ),
        _event(
            id="drivebc.ca/RIDE-ramp",
            severity="MAJOR",
            description="Entrance ramp closed.",
            roads=[{"name": "Sumas Way Onramp", "direction": "W", "state": "CLOSED"}],
        ),
        _event(
            id="drivebc.ca/RIDE-future",
            schedule={"intervals": ["2099-01-01T00:00/"]},
        ),
    ]
    mock_fetch.return_value = {"events": events}
    mock_dynamodb_table.query.return_value = {"Items": []}
    mock_dynamodb_table.scan.return_value = {"Items": []}

    with patch('scrape.table', mock_dynamodb_table):
        check_and_post_events()

    posted_ids = [call.args[0]["id"] for call in mock_post.call_args_list]
    assert posted_ids == ["drivebc.ca/RIDE-closed", "drivebc.ca/RIDE-keyword"]
    assert all(call.args[1] == "closure" for call in mock_post.call_args_list)

@mock_aws
@patch('scrape.post_to_discord')
def test_reopened_road_is_archived(mock_post):
    dynamodb = boto3.resource('dynamodb', region_name='us-east-1')
    table = dynamodb.create_table(
        TableName='test-db',
        KeySchema=[{'AttributeName': 'EventID', 'KeyType': 'HASH'}],
        AttributeDefinitions=[{'AttributeName': 'EventID', 'AttributeType': 'S'}],
        BillingMode='PAY_PER_REQUEST'
    )
    table.put_item(Item={
        "EventID": "drivebc.ca/RIDE-closed",
        "isActive": 1,
        "DetectedPolygon": "LowerMainland",
        "geography": {"type": "Point", "coordinates": [Decimal("-121.44"), Decimal("49.38")]},
        "id": "drivebc.ca/RIDE-closed",
        "description": "Vehicle incident. Closed.",
        "event_type": "INCIDENT",
        "severity": "MAJOR",
        "created": "2026-09-24T18:00:00-07:00",
        "updated": "2026-09-24T18:00:00-07:00",
    })

    reopened = {
        "id": "drivebc.ca/RIDE-closed",
        "status": "ACTIVE",
        "roads": [{"name": "Highway 3", "direction": "E", "state": "ALL_LANES_OPEN"}],
    }

    with patch('scrape.table', table):
        close_recent_events({"events": [reopened]})

    mock_post.assert_called_once()
    assert mock_post.call_args.args[1] == "archived"
    stored = table.get_item(Key={"EventID": "drivebc.ca/RIDE-closed"})["Item"]
    assert stored["isActive"] == 0

@patch('scrape.post_to_discord')
def test_close_recent_events_scans_every_page(mock_post):
    page_one_item = {
        "EventID": "drivebc.ca/RIDE-page1",
        "isActive": 1,
        "geography": {"type": "Point", "coordinates": [Decimal("-123.1"), Decimal("49.2")]},
    }
    page_two_item = {
        "EventID": "drivebc.ca/RIDE-page2",
        "isActive": 1,
        "geography": {"type": "Point", "coordinates": [Decimal("-123.2"), Decimal("49.3")]},
    }
    mock_table = Mock()
    mock_table.scan.side_effect = [
        {"Items": [page_one_item], "LastEvaluatedKey": {"EventID": "drivebc.ca/RIDE-page1"}},
        {"Items": [page_two_item]},
    ]

    with patch('scrape.table', mock_table):
        close_recent_events({"events": []})

    assert mock_table.scan.call_count == 2
    assert mock_table.scan.call_args_list[1].kwargs["ExclusiveStartKey"] == {"EventID": "drivebc.ca/RIDE-page1"}
    assert [call.args[0]["EventID"] for call in mock_post.call_args_list] == [
        "drivebc.ca/RIDE-page1",
        "drivebc.ca/RIDE-page2",
    ]

@patch('scrape.requests.get')
def test_fetch_all_events_ignores_severity(mock_get):
    first = Mock()
    first.ok = True
    first.json.return_value = {"events": [{"id": f"drivebc.ca/RIDE-{i}"} for i in range(300)]}
    second = Mock()
    second.ok = True
    second.json.return_value = {"events": [{"id": "drivebc.ca/RIDE-last"}]}
    mock_get.side_effect = [first, second]

    result = fetch_all_events()

    assert len(result["events"]) == 301
    assert mock_get.call_count == 2
    for call in mock_get.call_args_list:
        assert call.kwargs["params"]["status"] == "ACTIVE"
        assert "severity" not in call.kwargs["params"]

# Error Handling Tests
def test_check_which_polygon_invalid_input():
    # Test with invalid area name
    assert check_which_polygon('Invalid Area') == 'Other'

@mock_aws
@patch('scrape.requests.get')
def test_check_and_post_events_api_error(mock_get):
    # Set up mock DynamoDB table
    dynamodb = boto3.resource('dynamodb', region_name='us-east-1')
    table = dynamodb.create_table(
        TableName='test-db',
        KeySchema=[{'AttributeName': 'EventID', 'KeyType': 'HASH'}],
        AttributeDefinitions=[{'AttributeName': 'EventID', 'AttributeType': 'S'}],
        BillingMode='PAY_PER_REQUEST'
    )
    
    with patch('scrape.table', table):
        mock_get.return_value.ok = False
        with pytest.raises(Exception, match='Error connecting to BC511 API'):
            check_and_post_events()