import demistomock as demisto
from CommonServerPython import *  # noqa # pylint: disable=unused-wildcard-import
from CommonServerUserPython import *  # noqa

import urllib3
from typing import Dict, Any, Tuple

# Disable insecure warnings
urllib3.disable_warnings()  # pylint: disable=no-member


''' CONSTANTS '''

ALL_SUPPORTED_EVENT_TYPES = ['audit', 'page', 'network', 'application', 'alert']
MAX_EVENTS_PAGE_SIZE = 10000
MAX_SKIP = 50000

''' CLIENT CLASS '''


class Client(BaseClient):
    """
    Client for Netskope RESTful API.

    Args:
        base_url (str): The base URL of Netskope.
        token (str): The token to authenticate against Netskope API.
        validate_certificate (bool): Specifies whether to verify the SSL certificate or not.
        proxy (bool): Specifies if to use XSOAR proxy settings.
    """

    def __init__(self, base_url: str, token: str, api_version: str, validate_certificate: bool, proxy: bool):
        super().__init__(base_url, verify=validate_certificate, proxy=proxy)
        if api_version == 'v1':
            self._session.params['token'] = token  # type: ignore
        else:
            self.headers = {'Netskope-Api-Token': token}

    def get_events_request_v1(self, event_type: str, last_run: dict, skip: int = None,
                              limit: int = None, is_command: bool = False) -> Dict:  # pragma: no cover
        body = {
            'starttime': last_run.get(event_type),
            'endtime': int(datetime.now().timestamp()),
            'limit': limit if is_command else MAX_EVENTS_PAGE_SIZE,
            'type': event_type,
            'skip': skip
        }
        demisto.debug(f'Get event request body - {body}')
        response = self._http_request(method='GET', url_suffix='events', json_data=body, retries=3)
        return response

    def get_alerts_request_v1(self, last_run: dict, skip: int = None, limit: int = None,
                              is_command: bool = False) -> list[Any] | Any:  # pragma: no cover
        """
        Get alerts generated by Netskope, including policy, DLP, and watch list alerts.

        Args:
            last_run (dict): Get alerts from certain time period.
            skip (int): Skip over some events (useful for pagination in combination with limit).
            limit (int): Limit the number of events to return.
            is_command (bool): True when running any command besides the automatically triggered fetch mechanism.

        Returns:
            List[str, Any]: Netskope alerts.
        """

        url_suffix = 'alerts'
        body = {
            'starttime': last_run.get('alert'),
            'endtime': int(datetime.now().timestamp()),
            'limit': limit if is_command else MAX_EVENTS_PAGE_SIZE,
            'skip': skip
        }
        response = self._http_request(method='GET', url_suffix=url_suffix, json_data=body, retries=3)
        return response

    def get_events_request_v2(self, event_type: str, last_run: dict, skip: int = None,
                              limit: int = None, is_command: bool = False) -> Dict:  # pragma: no cover

        url_suffix = f'events/data/{event_type}'
        params = {
            'starttime': last_run.get(event_type),
            'endtime': int(datetime.now().timestamp()),
            'limit': limit if is_command else MAX_EVENTS_PAGE_SIZE,
            'skip': skip
        }
        response = self._http_request(method='GET', url_suffix=url_suffix, headers=self.headers,
                                      params=params, retries=3)
        return response


''' HELPER FUNCTIONS '''


def populate_parsing_rule_fields(event: dict, event_type: str):
    event['source_log_event'] = event_type
    try:
        event['_time'] = timestamp_to_datestring(event['timestamp'] * 1000)
    except TypeError:
        # modeling rule will default on ingestion time if _time is missing
        pass


def dedup_by_id(last_run: dict, results: list, event_type: str, limit: int):
    """
    Dedup mechanism for the fetch to check both event id and timestamp (since timestamp can be duplicate)
    Args:
        last_run: Last run.
        results: List of the events from the api.
        event_type: the event type.
        limit: the number of events to return.

    Returns:
        - list of events to send to XSIAM.
        - The new last_run (dictionary with the relevant timestamps and the events ids)

    """
    last_run_ids = set(last_run.get(f'{event_type}-ids', []))
    new_events = []
    new_events_ids = []
    new_last_run = {}

    # Sorting the list to Ascending order according to the timestamp (old one first)
    sorted_list = list(reversed(results))
    for event in sorted_list[:limit]:
        event_timestamp = event.get('timestamp')
        event_id = event.get('_id')
        event['event_id'] = event_id

        # The event we are looking at has the same timestamp as previously fetched events
        if event_timestamp == last_run[event_type]:
            if event_id not in last_run_ids:
                new_events.append(event)
                last_run_ids.add(event_id)

        # The event has a timestamp we have not yet fetched meaning it is a new event
        else:
            new_events.append(event)
            new_events_ids.append(event_id)
            # Since the event has a timestamp newer than the saved one, we will update the last run to the
            # current event time
            new_last_run[event_type] = event_timestamp

        # If we have received events with a newer time (new_event_ids list) we save them,
        # otherwise we save the list that include the old ids together with the new event ids.
        new_last_run[f'{event_type}-ids'] = new_events_ids or last_run_ids

    demisto.debug(f'Setting new last run - {new_last_run}')
    return new_events, new_last_run


''' COMMAND FUNCTIONS '''


def test_module(client: Client, api_version: str, last_run: dict, max_fetch: int) -> str:

    fetch_events_command(client, api_version, last_run, max_fetch=max_fetch, is_command=True)
    return 'ok'


def get_all_events(client: Client, last_run: dict, limit: int, api_version: str, is_command: bool) -> Tuple[list, dict]:
    """
    This Function is doing a pagination to get all events within the given start and end time.
    Maximum events to get per a fetch call is 50,000 (MAX_SKIP)
    Args:
        client: Netskope Client
        last_run (dict): the last run
        limit (int): the number of events to return
        api_version (str): The API version: v1 or v2
        is_command (bool): Are we running the commands or test_module or not

    Returns (list): list of all the events from a start time.
    """
    events_result = []
    new_last_run = {}
    if limit is None:
        limit = MAX_EVENTS_PAGE_SIZE
    for event_type in ALL_SUPPORTED_EVENT_TYPES:
        events = []
        skip = 0
        while True:
            if api_version == 'v1':
                if event_type == 'alert':
                    response = client.get_alerts_request_v1(last_run, skip, limit, is_command)
                else:
                    response = client.get_events_request_v1(event_type, last_run, skip, limit, is_command)

                if response.get('status') != 'success':  # type: ignore
                    break

                results = response.get('data', [])  # type: ignore

            else:  # API version == v2
                response = client.get_events_request_v2(event_type, last_run, skip, limit, is_command)
                if response.get('ok') != 1:
                    break

                results = response.get('result', [])

            demisto.debug(f'The number of received events - {len(results)}')
            events.extend(results)
            if len(results) == MAX_EVENTS_PAGE_SIZE:
                skip += MAX_EVENTS_PAGE_SIZE

            if len(results) < MAX_EVENTS_PAGE_SIZE or not results or len(events) == MAX_SKIP:
                # This means that we either finished going over all results or that we have reached the
                # limit of accumulated events.
                break

        if events:
            final_events, partial_last_run = dedup_by_id(last_run, events, event_type, limit)
            # prepare for the next iteration
            new_last_run.update(partial_last_run)
            demisto.debug(f'Initialize last run after fetch - {event_type} - {new_last_run[event_type]} \n '
                          f'Events IDs to send to XSIAM - {new_last_run[f"{event_type}-ids"]}')

            for event in final_events:
                populate_parsing_rule_fields(event, event_type)
            events_result.extend(final_events)

    return events_result, new_last_run


def get_events_command(client: Client, args: Dict[str, Any], last_run: dict, api_version: str,
                       is_command: bool) -> Tuple[CommandResults, list]:

    limit = arg_to_number(args.get('limit')) or 50
    events, _ = get_all_events(client, last_run, api_version=api_version, limit=limit, is_command=is_command)

    for event in events:
        event['timestamp'] = timestamp_to_datestring(event['timestamp'] * 1000)

    readable_output = tableToMarkdown('Events List:', events,
                                      removeNull=True,
                                      headers=['_id', 'timestamp', 'type', 'access_method', 'app', 'traffic_type'],
                                      headerTransform=string_to_table_header)

    results = CommandResults(outputs_prefix='Netskope.Event',
                             outputs_key_field='_id',
                             outputs=events,
                             readable_output=readable_output,
                             raw_response=events)

    return results, events


def fetch_events_command(client, api_version, last_run, max_fetch, is_command):  # pragma: no cover
    events, new_last_run = get_all_events(client, last_run=last_run, limit=max_fetch, api_version=api_version,
                                          is_command=is_command)

    return events, new_last_run


''' MAIN FUNCTION '''


def main() -> None:  # pragma: no cover
    params = demisto.params()

    url = params.get('url')
    api_version = params.get('api_version')
    token = params.get('credentials', {}).get('password')
    base_url = urljoin(url, f'/api/{api_version}/')
    verify_certificate = not params.get('insecure', False)
    proxy = params.get('proxy', False)
    first_fetch = params.get('first_fetch')
    max_fetch = arg_to_number(params.get('max_fetch', 1000))
    vendor, product = params.get('vendor', 'netskope'), params.get('product', 'netskope')

    demisto.debug(f'Command being called is {demisto.command()}')
    try:
        client = Client(base_url, token, api_version, verify_certificate, proxy)

        last_run = demisto.getLastRun()
        demisto.debug(f'Running with the following last_run - {last_run}')
        for event_type in ALL_SUPPORTED_EVENT_TYPES:
            # First Fetch
            if not last_run.get(event_type):
                first_fetch = int(arg_to_datetime(first_fetch).timestamp())  # type: ignore[union-attr]
                last_run_id_key = f'{event_type}-ids'
                last_run[event_type] = first_fetch
                last_run[last_run_id_key] = last_run.get(last_run_id_key, [])
                demisto.debug(f'First Fetch - Initialize last run - {last_run}')

        if demisto.command() == 'test-module':
            # This is the call made when pressing the integration Test button.
            result = test_module(client, api_version, last_run, max_fetch)   # type: ignore[arg-type]
            return_results(result)

        elif demisto.command() == 'netskope-get-events':
            results, events = get_events_command(client, demisto.args(), last_run, api_version, is_command=True)

            if argToBoolean(demisto.args().get('should_push_events', 'true')):
                send_events_to_xsiam(events=events, vendor=vendor, product=product)  # type: ignore
            return_results(results)

        elif demisto.command() == 'fetch-events':
            demisto.debug(f'Sending request with last run {last_run}')
            events, new_last_run = fetch_events_command(client, api_version, last_run, max_fetch, is_command=False)
            send_events_to_xsiam(events=events, vendor=vendor, product=product)
            demisto.debug(f'Setting the last_run to: {new_last_run}')
            demisto.setLastRun(new_last_run)

    # Log exceptions and return errors
    except Exception as e:
        return_error(f'Failed to execute {demisto.command()} command.\nError:\n{str(e)}')


''' ENTRY POINT '''


if __name__ in ('__main__', '__builtin__', 'builtins'):
    main()
