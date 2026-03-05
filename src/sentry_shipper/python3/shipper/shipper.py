import json
import logging
import os
import time
import urllib
import urllib.request
import uuid
from datetime import datetime, timezone
from urllib.parse import urlparse

# set logger
logger = logging.getLogger()
logger.setLevel(logging.INFO)

# .NET LogLevel -> Sentry level + severity_number
LOG_LEVEL_MAP = {
    'trace':       ('trace', 1),
    'debug':       ('debug', 5),
    'information': ('info', 9),
    'warning':     ('warn', 13),
    'error':       ('error', 17),
    'critical':    ('fatal', 21),
}

SDK_NAME = 'sentry.cloudwatch.shipper'
SDK_VERSION = '1.0.0'
MAX_LOGS_PER_ENVELOPE = 100


class MaxRetriesException(Exception):
    pass


class UnauthorizedAccessException(Exception):
    pass


class BadRequestException(Exception):
    pass


class SentryShipper(object):

    def __init__(self):
        dsn = os.environ.get('SENTRY_DSN', '')
        if not dsn:
            raise ValueError("Missing SENTRY_DSN environment variable")

        parsed = urlparse(dsn)
        self._public_key = parsed.username
        self._host = parsed.hostname
        self._project_id = parsed.path.strip('/')
        self._scheme = parsed.scheme

        self._envelope_url = "{scheme}://{host}/api/{project_id}/envelope/".format(
            scheme=self._scheme,
            host=self._host,
            project_id=self._project_id
        )

        self._auth_header = (
            "Sentry sentry_version=7, "
            "sentry_key={key}, "
            "sentry_client={name}/{version}"
        ).format(key=self._public_key, name=SDK_NAME, version=SDK_VERSION)

        self._logs = []

    def _make_sentry_log(self, log):
        # type: (dict) -> dict

        # Extract timestamp (CloudWatch provides milliseconds since epoch)
        timestamp = log.get('@timestamp', log.get('timestamp', ''))
        try:
            ts_seconds = float(timestamp) / 1000.0
        except (ValueError, TypeError):
            ts_seconds = datetime.now(timezone.utc).timestamp()

        # Extract log level
        log_level_raw = log.get('LogLevel', log.get('log_level', 'error'))
        level_key = log_level_raw.lower()
        sentry_level, severity_number = LOG_LEVEL_MAP.get(level_key, ('error', 17))

        # Extract trace ID (from .NET scopes or generate random)
        trace_id = log.get('TraceId', '')
        if not trace_id:
            # Try nested Scopes field
            scopes = log.get('Scopes', [])
            if isinstance(scopes, list):
                for scope in scopes:
                    if isinstance(scope, dict) and 'TraceId' in scope:
                        trace_id = scope['TraceId']
                        break
        if not trace_id:
            trace_id = uuid.uuid4().hex

        # Extract message body
        body = log.get('Message', log.get('message', ''))

        # Build attributes
        attributes = {
            'sentry.sdk.name': {'value': SDK_NAME, 'type': 'string'},
            'sentry.sdk.version': {'value': SDK_VERSION, 'type': 'string'},
        }

        # Environment from ENRICH data
        environment = log.get('Environment', '')
        if environment:
            attributes['sentry.environment'] = {'value': environment, 'type': 'string'}

        # Log source metadata
        log_group = log.get('logGroup', '')
        if log_group:
            attributes['log.group'] = {'value': log_group, 'type': 'string'}

        category = log.get('Category', '')
        if category:
            attributes['log.category'] = {'value': category, 'type': 'string'}

        project = log.get('Project', '')
        if project:
            attributes['project'] = {'value': project, 'type': 'string'}

        # Exception (stack trace)
        exception = log.get('Exception', '')
        if exception:
            attributes['exception'] = {'value': exception, 'type': 'string'}

        # State (structured log parameters like TREATMENT, CUSTOMER, ERROR)
        state = log.get('State', {})
        if isinstance(state, dict):
            for key, value in state.items():
                if key in ('Message', '{OriginalFormat}'):
                    continue
                attributes['state.{}'.format(key)] = {'value': str(value), 'type': 'string'}

        # Scopes (SpanId, ParentId, RequestPath, ConnectionId)
        scopes = log.get('Scopes', [])
        if isinstance(scopes, list):
            for scope in scopes:
                if isinstance(scope, dict):
                    for key, value in scope.items():
                        if key == 'Message' or key == 'TraceId':
                            continue
                        attributes['scope.{}'.format(key)] = {'value': str(value), 'type': 'string'}

        event_id = log.get('EventId', '')
        if event_id:
            if isinstance(event_id, dict):
                event_id = json.dumps(event_id)
            attributes['event.id'] = {'value': str(event_id), 'type': 'string'}

        return {
            'timestamp': ts_seconds,
            'level': sentry_level,
            'body': body,
            'trace_id': trace_id,
            'severity_number': severity_number,
            'attributes': attributes,
        }

    def add(self, log):
        # type: (dict) -> None
        sentry_log = self._make_sentry_log(log)
        self._logs.append(sentry_log)

        if len(self._logs) >= MAX_LOGS_PER_ENVELOPE:
            self._send_to_sentry()
            self._logs = []

    def flush(self):
        if self._logs:
            self._send_to_sentry()
            self._logs = []

    def _build_envelope(self):
        # type: () -> bytes
        event_id = uuid.uuid4().hex
        now = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%S.%fZ')

        header = json.dumps({
            'event_id': event_id,
            'dsn': '{scheme}://{key}@{host}/{project_id}'.format(
                scheme=self._scheme,
                key=self._public_key,
                host=self._host,
                project_id=self._project_id
            ),
            'sent_at': now,
        })

        item_header = json.dumps({
            'type': 'log',
            'item_count': len(self._logs),
            'content_type': 'application/vnd.sentry.items.log+json',
        })

        item_payload = json.dumps({'items': self._logs})

        envelope = '{}\n{}\n{}'.format(header, item_header, item_payload)
        return envelope.encode('utf-8')

    def _send_to_sentry(self):

        @SentryShipper._retry
        def do_request():
            data = self._build_envelope()
            headers = {
                'Content-Type': 'application/x-sentry-envelope',
                'X-Sentry-Auth': self._auth_header,
            }
            request = urllib.request.Request(
                self._envelope_url, data=data, headers=headers
            )
            return urllib.request.urlopen(request)

        try:
            do_request()
            logger.info(
                "Successfully sent {} logs to Sentry!".format(len(self._logs)))
        except MaxRetriesException:
            logger.error('Retry limit reached. Failed to send logs to Sentry.')
            raise
        except UnauthorizedAccessException:
            logger.error(
                "Unauthorized access to Sentry. Check your DSN.")
            raise
        except BadRequestException as e:
            logger.error(
                "Bad request to Sentry (400). Logs may be malformed: {}".format(e))
            logger.warning("Dropping malformed logs...")
        except urllib.error.HTTPError as e:
            logger.error(
                "Unexpected HTTP error while sending to Sentry: {}".format(e))
            raise
        except Exception as e:
            logger.error(
                "Unexpected error while sending to Sentry: {}".format(e))
            raise

    @staticmethod
    def _retry(func):
        def retry_func():
            max_retries = 4
            sleep_between_retries = 2

            for retries in range(max_retries):
                if retries:
                    sleep_between_retries *= 2
                    logger.info("Failure in sending logs - Trying again in {} seconds"
                                .format(sleep_between_retries))
                    time.sleep(sleep_between_retries)
                try:
                    res = func()
                except urllib.error.HTTPError as e:
                    status_code = e.getcode()
                    if status_code == 400:
                        raise BadRequestException(e.reason)
                    elif status_code == 401 or status_code == 403:
                        raise UnauthorizedAccessException()
                    elif status_code == 429:
                        logger.warning("Rate limited by Sentry (429). Will retry...")
                        continue
                    else:
                        logger.error("HTTP error {}: {}".format(status_code, e))
                        continue
                except urllib.error.URLError:
                    raise
                return res

            raise MaxRetriesException()

        return retry_func
