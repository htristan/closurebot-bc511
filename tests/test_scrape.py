import pytest
from unittest.mock import patch, Mock, mock_open
import json
from datetime import datetime, timedelta
from decimal import Decimal
from freezegun import freeze_time
from moto import mock_aws
import boto3
import os

# Add this before the scrape import
os.environ['DISCORD_WEBHOOK'] = 'https://mock-discord-webhook.com/test'

from scrape import (
    check_which_polygon, getThreadID, unix_to_readable_with_timezone,
    post_to_discord,
    close_recent_events, cleanup_old_events, float_to_decimal,
    check_and_post_events
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
    sample_event['id'] = sample_event['ID']  # Map ID to id
    sample_event['event_type'] = sample_event.get('EventType', 'roadwork')
    sample_event['severity'] = 'MODERATE'
    sample_event['description'] = sample_event.get('Description', 'Test description')
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
    sample_event['id'] = sample_event['ID']  # Map ID to id
    sample_event['event_type'] = sample_event.get('EventType', 'roadwork')
    sample_event['severity'] = 'MODERATE'
    sample_event['description'] = sample_event.get('Description', 'Test description')
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
    sample_event['id'] = sample_event['ID']  # Map ID to id
    sample_event['event_type'] = sample_event.get('EventType', 'roadwork')
    sample_event['severity'] = 'MODERATE'
    sample_event['description'] = sample_event.get('Description', 'Test description')
    sample_event['created'] = '2023-01-01T12:00:00Z'
    sample_event['updated'] = '2023-01-01T12:00:00Z'
    
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
def test_check_and_post_events(mock_post, mock_fetch, mock_dynamodb_table, sample_events):
    # Update sample events to match expected structure
    for event in sample_events:
        event['status'] = 'ACTIVE'
        event['id'] = event['ID']  # Map ID to id
        event['geography'] = {'type': 'Point', 'coordinates': [-75.69528, 45.40719], 'areas': [{'name': 'Lower Mainland District'}]}
        event['event_type'] = event.get('EventType', 'roadwork')
        event['severity'] = 'MODERATE'
        event['description'] = event.get('Description', 'Test description')
        event['created'] = '2023-01-01T12:00:00Z'
        event['updated'] = '2023-01-01T12:00:00Z'
    
    # Mock fetch_all_events to return proper structure
    mock_fetch.return_value = {'events': sample_events}
    
    # Mock the database query to return no existing items
    mock_dynamodb_table.query.return_value = {'Items': []}
    
    with patch('scrape.table', mock_dynamodb_table), \
         patch('scrape.config', mock_config):
        # Test the main function
        check_and_post_events()
        
        # Verify Discord post was called for new events
        assert mock_post.call_count > 0

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